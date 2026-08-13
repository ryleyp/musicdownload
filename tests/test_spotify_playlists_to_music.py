from __future__ import annotations

import csv
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import spotify_playlists_to_music as playlists
from common import connect_db, utc_now


class FakeSpotifyClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, url: str, params=None):
        self.calls.append(url)
        if url == "/me/playlists":
            return {
                "items": [
                    {
                        "id": "playlist1",
                        "name": "Road Trip",
                        "description": "Test",
                        "owner": {"display_name": "Owner"},
                        "external_urls": {"spotify": "https://spotify.test/p1"},
                        "snapshot_id": "snapshot1",
                    }
                ],
                "total": 1,
                "next": None,
            }
        if url == "/playlists/playlist1/items":
            return {
                "items": [
                    {
                        "added_at": "2026-01-01T00:00:00Z",
                        "track": {
                            "id": "spotify1",
                            "type": "track",
                            "name": "Song",
                            "artists": [{"name": "Artist"}],
                            "album": {"name": "Album"},
                            "is_local": False,
                        },
                    }
                ],
                "next": None,
            }
        raise AssertionError(url)


class SpotifyPlaylistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = self.root / "music.sqlite"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_playlist_snapshot_is_immutable_and_migrated(self) -> None:
        client = FakeSpotifyClient()
        with connect_db(self.db) as connection, redirect_stdout(io.StringIO()):
            first, _, _ = playlists.sync_snapshot(connection, client)
            second, _, _ = playlists.sync_snapshot(connection, client)
            runs = connection.execute(
                "SELECT count(*) FROM spotify_playlist_sync_runs"
            ).fetchone()[0]
            tracks = connection.execute(
                "SELECT count(*) FROM spotify_playlist_tracks"
            ).fetchone()[0]
        self.assertNotEqual(first, second)
        self.assertEqual(runs, 2)
        self.assertEqual(tracks, 2)

    def test_duplicate_playlist_ids_from_spotify_are_deduplicated(self) -> None:
        class DuplicateClient:
            def get(self, _url: str, params=None):
                return {
                    "items": [
                        {"id": "same", "name": "First"},
                        {"id": "same", "name": "Duplicate"},
                    ],
                    "total": 2,
                    "next": None,
                }

        with redirect_stdout(io.StringIO()):
            result = playlists.fetch_all_playlists(DuplicateClient())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "First")

    def test_safe_resume_allows_ordered_music_omissions(self) -> None:
        self.assertEqual(
            playlists.safe_append_tail(["A", "B"], ["A", "B", "C"]),
            ["C"],
        )
        self.assertEqual(
            playlists.safe_append_tail(["A", "C"], ["A", "B", "C", "D"]),
            ["D"],
        )
        with self.assertRaisesRegex(Exception, "left unchanged"):
            playlists.safe_append_tail(["B", "A"], ["A", "B"])

    def test_duplicate_spotify_playlist_names_get_stable_suffixes(self) -> None:
        source = [
            {"spotify_playlist_id": "abcdef123", "name": "Mix"},
            {"spotify_playlist_id": "uvwxyz987", "name": "mix"},
        ]
        names = playlists.target_playlist_names(source, "Spotify - ")
        self.assertEqual(names["abcdef123"], "Spotify - Mix [abcdef]")
        self.assertEqual(names["uvwxyz987"], "Spotify - mix [uvwxyz]")

    def test_apply_uses_downloaded_marker_and_preserves_order(self) -> None:
        with connect_db(self.db) as connection:
            run_id = "run1"
            connection.execute(
                "INSERT INTO spotify_playlist_sync_runs VALUES (?, ?, 1, 2)",
                (run_id, utc_now()),
            )
            connection.execute(
                """
                INSERT INTO spotify_playlists (
                    run_id, spotify_playlist_id, name, item_count
                ) VALUES (?, 'playlist1', 'Mix', 2)
                """,
                (run_id,),
            )
            connection.executemany(
                """
                INSERT INTO spotify_playlist_tracks (
                    run_id, spotify_playlist_id, position, spotify_track_id,
                    title, artist, album, item_type, is_local
                ) VALUES (?, 'playlist1', ?, ?, ?, 'Artist', 'Album', 'track', 0)
                """,
                [
                    (run_id, 0, "spotify1", "First"),
                    (run_id, 1, "spotify2", "Second"),
                ],
            )
        music = [
            {
                "persistent_id": "MUSIC1",
                "title": "First",
                "artist": "Artist",
                "album": "Album",
                "duration": 180.0,
                "enabled": True,
                "location": "/Music/first.mp3",
                "comment": "SPOTIFY_ARCHIVE_ID=spotify1",
            },
            {
                "persistent_id": "MUSIC2",
                "title": "Second",
                "artist": "Artist",
                "album": "Album",
                "duration": 181.0,
                "enabled": True,
                "location": "/Music/second.mp3",
                "comment": "SPOTIFY_ARCHIVE_ID=spotify2",
            },
        ]
        report = self.root / "report.csv"
        with (
            patch.object(playlists, "require_mac"),
            patch.object(playlists, "scan_music_library", return_value=music),
            patch.object(playlists, "playlist_state", return_value=(True, ["MUSIC1"])),
            patch.object(
                playlists,
                "append_playlist",
                return_value={"requested": 1, "added": 1, "missing": 0, "final": 2},
            ) as append,
            redirect_stdout(io.StringIO()),
        ):
            result = playlists.main(
                ["--db", str(self.db), "--report", str(report), "--apply"]
            )
        self.assertEqual(result, 0)
        append.assert_called_once_with("Spotify - Mix", ["MUSIC2"])
        with report.open(encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["status"] for row in rows], ["ready", "ready"])


if __name__ == "__main__":
    unittest.main()
