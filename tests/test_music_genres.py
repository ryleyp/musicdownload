from __future__ import annotations

import csv
import io
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import music_genres
from common import connect_db


def track(path: Path, **values):
    defaults = {
        "persistent_id": "PID1",
        "title": "Song",
        "artist": "Artist",
        "album_artist": "Artist",
        "album": "Album",
        "duration": 180.0,
        "enabled": True,
        "location": str(path),
        "comment": "",
        "genre": "",
    }
    defaults.update(values)
    return defaults


class GenreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = self.root / "library.sqlite"
        self.audio = self.root / "song.mp3"
        self.audio.touch()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_migration_adds_genre_tables_without_replacing_data(self) -> None:
        with connect_db(self.db) as connection:
            connection.execute(
                "INSERT INTO tracks (spotify_id, title, artists, album, duration_ms, updated_at) "
                "VALUES ('keep', 'Song', 'Artist', 'Album', 180000, 'now')"
            )
        with connect_db(self.db) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertTrue(
                {"music_genre_cache", "music_genre_runs", "music_genre_changes"}
                <= tables
            )
            self.assertEqual(
                connection.execute("SELECT spotify_id FROM tracks").fetchone()[0],
                "keep",
            )

    def test_album_consensus_requires_one_unambiguous_genre(self) -> None:
        rows = [
            track(self.audio, persistent_id="A", genre="Rock"),
            track(self.audio, persistent_id="B", genre="Rock"),
        ]
        with patch.object(music_genres, "embedded_genre", return_value=""):
            consensus = music_genres.group_consensus(rows)
        self.assertEqual(
            consensus[music_genres.album_key(rows[0])],
            ("Rock", "music_album_consensus"),
        )
        rows[1]["genre"] = "Pop"
        with patch.object(music_genres, "embedded_genre", return_value=""):
            self.assertNotIn(
                music_genres.album_key(rows[0]),
                music_genres.group_consensus(rows),
            )

    def test_catalog_requires_exact_normalized_album_and_artist(self) -> None:
        session = Mock()
        response = session.get.return_value
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "results": [
                {
                    "collectionName": "The Album",
                    "artistName": "The Artist",
                    "primaryGenreName": "Alternative",
                    "collectionViewUrl": "https://example.test/exact",
                },
                {
                    "collectionName": "The Album (Live)",
                    "artistName": "The Artist",
                    "primaryGenreName": "Rock",
                },
                {
                    "collectionName": "The Album",
                    "artistName": "Another Artist",
                    "primaryGenreName": "Pop",
                },
            ]
        }
        self.assertEqual(
            music_genres.catalog_result(session, "The Artist", "The Album", "US"),
            (
                "Alternative",
                "apple_catalog",
                "matched",
                "https://example.test/exact",
            ),
        )
        self.assertEqual(
            music_genres.catalog_result(session, "Missing", "The Album", "US")[2],
            "no_match",
        )

    def test_candidates_preserve_existing_and_protect_vinyl(self) -> None:
        normal = track(self.audio, persistent_id="NORMAL", genre="Jazz")
        vinyl = track(
            self.audio,
            persistent_id="VINYL",
            album="Album (VINYL)",
        )
        consensus = {
            music_genres.album_key(normal): ("Rock", "music_album_consensus"),
            music_genres.album_key(vinyl): ("Rock", "music_album_consensus"),
        }
        rows = music_genres.candidate_rows(
            [normal, vinyl], consensus, {}, overwrite=False
        )
        self.assertEqual(rows[0]["action"], "keep_existing")
        self.assertEqual(rows[1]["action"], "protected_vinyl")

    def test_report_only_never_calls_music_apply(self) -> None:
        report = self.root / "report.csv"
        argv = ["--db", str(self.db), "--report", str(report)]
        rows = [track(self.audio)]
        with (
            patch.object(music_genres, "require_mac"),
            patch.object(music_genres, "scan_music_genres", return_value=rows),
            patch.object(
                music_genres,
                "group_consensus",
                return_value={music_genres.album_key(rows[0]): ("Rock", "test")},
            ),
            patch.object(music_genres, "set_genres") as apply_mock,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(music_genres.main(argv), 0)
        apply_mock.assert_not_called()
        with report.open(encoding="utf-8-sig") as handle:
            written = list(csv.DictReader(handle))
        self.assertEqual(written[0]["action"], "would_fill_missing")

    def test_apply_is_recorded_and_verified_for_restore(self) -> None:
        report = self.root / "report.csv"
        before = [track(self.audio)]
        after = [track(self.audio, genre="Rock")]
        argv = ["--db", str(self.db), "--report", str(report), "--apply"]
        with (
            patch.object(music_genres, "require_mac"),
            patch.object(
                music_genres,
                "scan_music_genres",
                side_effect=[before, after],
            ),
            patch.object(
                music_genres,
                "group_consensus",
                return_value={music_genres.album_key(before[0]): ("Rock", "test")},
            ),
            patch.object(
                music_genres,
                "set_genres",
                return_value={"applied": 1, "missing": 0, "protected_vinyl": 0},
            ) as apply_mock,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(music_genres.main(argv), 0)
        apply_mock.assert_called_once_with([("PID1", "Rock")])
        with connect_db(self.db) as connection:
            run = connection.execute("SELECT * FROM music_genre_runs").fetchone()
            change = connection.execute("SELECT * FROM music_genre_changes").fetchone()
        self.assertEqual(run["status"], "applied")
        self.assertEqual(run["applied_count"], 1)
        self.assertEqual(change["old_genre"], "")
        self.assertEqual(change["new_genre"], "Rock")
        self.assertEqual(change["status"], "applied")

    def test_lookup_batch_selection_is_resumable(self) -> None:
        rows = [
            track(self.audio, persistent_id="A", album="First"),
            track(self.audio, persistent_id="B", album="Second"),
        ]
        args = Namespace(
            batch_size=1,
            delay_seconds=3.0,
            all=False,
            refresh_cache=False,
            retry_errors=False,
            country="US",
        )
        fake_session = Mock()
        with (
            connect_db(self.db) as connection,
            patch.dict(
                "sys.modules",
                {"requests": SimpleNamespace(Session=Mock(return_value=fake_session))},
            ),
            patch.object(
                music_genres,
                "catalog_result",
                return_value=("Rock", "apple_catalog", "matched", "url"),
            ) as lookup,
            redirect_stdout(io.StringIO()),
        ):
            music_genres.perform_lookups(connection, rows, {}, args)
            music_genres.perform_lookups(connection, rows, {}, args)
            cached = connection.execute(
                "SELECT count(*) FROM music_genre_cache"
            ).fetchone()[0]
        self.assertEqual(lookup.call_count, 2)
        self.assertEqual(cached, 2)


if __name__ == "__main__":
    unittest.main()
