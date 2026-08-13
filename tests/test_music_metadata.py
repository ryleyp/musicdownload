from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import music_metadata
from common import connect_db, utc_now


def music_track(path: Path, **values):
    defaults = {
        "persistent_id": "PID1",
        "title": "Old Song",
        "artist": "Old Artist",
        "album_artist": "",
        "album": "Old Album",
        "genre": "Rock",
        "year": 0,
        "track_number": 0,
        "track_count": 0,
        "disc_number": 0,
        "compilation": False,
        "comment": "SPOTIFY_ARCHIVE_ID=spotify1\nPersonal note",
        "enabled": True,
        "location": str(path),
    }
    defaults.update(values)
    return defaults


def insert_spotify(connection) -> None:
    connection.execute(
        """
        INSERT INTO tracks (
            spotify_id, title, artists, primary_artist, album, album_artist,
            release_date, release_year, disc_number, track_number, total_tracks,
            explicit, isrc, spotify_url, album_url, youtube_url, genres,
            added_at, download_status, download_path, updated_at
        ) VALUES (
            'spotify1', 'Song', 'Artist & Guest', 'Artist', 'Album',
            'Various Artists', '2025-03-04', 2025, 1, 2, 12, 1, 'USABC123',
            'https://spotify.test/track', 'https://spotify.test/album',
            'https://youtube.test/watch', '', '2025-04-01', 'downloaded',
            '/tmp/song.mp3', ?
        )
        """,
        (utc_now(),),
    )


class MusicMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = self.root / "library.sqlite"
        self.audio = self.root / "song.mp3"
        self.audio.touch()
        with connect_db(self.db) as connection:
            insert_spotify(connection)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_schema_adds_reversible_metadata_tables(self) -> None:
        with connect_db(self.db) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertIn("music_metadata_runs", tables)
        self.assertIn("music_metadata_changes", tables)

    def test_build_rows_uses_spotify_fields_and_preserves_genre_and_notes(self) -> None:
        current = music_track(self.audio)
        with connect_db(self.db) as connection:
            spotify = connection.execute(
                "SELECT * FROM tracks WHERE spotify_id='spotify1'"
            ).fetchone()
        row = music_metadata.build_rows([current], {"spotify1": spotify})[0]
        desired = row["new_values"]
        self.assertEqual(row["action"], "would_update")
        self.assertEqual(desired["title"], "Song")
        self.assertEqual(desired["artist"], "Artist & Guest")
        self.assertEqual(desired["album_artist"], "Various Artists")
        self.assertEqual(desired["genre"], "Rock")
        self.assertEqual(desired["year"], 2025)
        self.assertEqual(desired["track_number"], 2)
        self.assertEqual(desired["track_count"], 12)
        self.assertTrue(desired["compilation"])
        self.assertIn("ISRC=USABC123", desired["comment"])
        self.assertIn("EXPLICIT=1", desired["comment"])
        self.assertIn("Personal note", desired["comment"])
        self.assertLessEqual(len(desired["comment"]), 255)

    def test_music_text_fields_respect_music_limit(self) -> None:
        self.assertEqual(len(music_metadata.music_text("x" * 300)), 255)

    def test_non_project_tracks_are_ignored_and_vinyl_is_protected(self) -> None:
        unrelated = music_track(self.audio, persistent_id="OTHER", comment="")
        vinyl = music_track(self.audio, album="Old Album (VINYL)")
        with connect_db(self.db) as connection:
            spotify = connection.execute("SELECT * FROM tracks").fetchone()
        rows = music_metadata.build_rows(
            [unrelated, vinyl], {"spotify1": spotify}
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "protected_vinyl")

    def test_report_only_never_calls_metadata_set(self) -> None:
        report = self.root / "report.csv"
        current = [music_track(self.audio)]
        with (
            patch.object(music_metadata, "require_mac"),
            patch.object(music_metadata, "scan_music_metadata", return_value=current),
            patch.object(music_metadata, "set_metadata") as setter,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                music_metadata.main(
                    ["--db", str(self.db), "--report", str(report)]
                ),
                0,
            )
        setter.assert_not_called()
        with report.open(encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["action"], "would_update")

    def test_apply_records_before_state_and_verifies(self) -> None:
        report = self.root / "report.csv"
        before = music_track(self.audio)
        with connect_db(self.db) as connection:
            spotify = connection.execute("SELECT * FROM tracks").fetchone()
        desired = music_metadata.desired_values(before, spotify)
        after = dict(before, **desired)
        with (
            patch.object(music_metadata, "require_mac"),
            patch.object(
                music_metadata, "scan_music_metadata", side_effect=[[before], [after]]
            ),
            patch.object(
                music_metadata,
                "set_metadata",
                return_value={"applied": 1, "missing": 0, "protected_vinyl": 0},
            ) as setter,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                music_metadata.main(
                    ["--db", str(self.db), "--report", str(report), "--apply"]
                ),
                0,
            )
        setter.assert_called_once()
        with connect_db(self.db) as connection:
            run = connection.execute("SELECT * FROM music_metadata_runs").fetchone()
            change = connection.execute(
                "SELECT * FROM music_metadata_changes"
            ).fetchone()
        self.assertEqual(run["status"], "applied")
        self.assertEqual(run["applied_count"], 1)
        self.assertEqual(change["status"], "applied")
        self.assertIn('"title": "Old Song"', change["old_values_json"])
        self.assertIn('"title": "Song"', change["new_values_json"])

    def test_batch_result_reports_skips_without_aborting_later_tracks(self) -> None:
        row = {
            "music_persistent_id": "PID1",
            "new_values": music_metadata.current_values(music_track(self.audio)),
        }
        with patch.object(
            music_metadata, "run_bridge", return_value="1\x1f0\x1f0"
        ) as bridge:
            result = music_metadata.set_metadata([row], 25)
        self.assertEqual(result["applied"], 1)
        self.assertEqual(bridge.call_args.args[0][0], "metadata-set")
        self.assertEqual(len(bridge.call_args.args[0]), 13)

    def test_restore_includes_verification_failed_before_states(self) -> None:
        old = music_metadata.current_values(music_track(self.audio))
        new = dict(old, title="New Song")
        with connect_db(self.db) as connection:
            connection.execute(
                "INSERT INTO music_metadata_runs "
                "(run_id, status, planned_count, created_at) "
                "VALUES ('run1', 'partial', 1, ?)",
                (utc_now(),),
            )
            connection.execute(
                "INSERT INTO music_metadata_changes "
                "(run_id, music_persistent_id, spotify_id, old_values_json, "
                "new_values_json, status) VALUES "
                "('run1', 'PID1', 'spotify1', ?, ?, 'verification_failed')",
                (json.dumps(old), json.dumps(new)),
            )
            with (
                patch.object(music_metadata, "set_metadata") as setter,
                patch.object(music_metadata, "verify_run", return_value=1),
                redirect_stdout(io.StringIO()),
            ):
                music_metadata.restore_run(connection, "run1", 25)
        self.assertEqual(setter.call_args.args[0][0]["new_values"], old)


if __name__ == "__main__":
    unittest.main()
