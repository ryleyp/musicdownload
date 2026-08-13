from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from common import connect_db, utc_now
from download_mp3 import (
    final_path,
    select_download_tracks,
    yt_dlp_download_command,
)
from library_status import progress_counts
from matching_rules import SCORING_VERSION
from reset_matches import reset_current_matches, reset_preview


def insert_track(connection: sqlite3.Connection, spotify_id: str, **values) -> None:
    defaults = {
        "title": f"Song {spotify_id}",
        "artists": "Test Artist",
        "primary_artist": "Test Artist",
        "album": "Test Album",
        "duration_ms": 180000,
        "is_liked": 1,
        "youtube_url": f"https://www.youtube.com/watch?v={spotify_id}",
        "youtube_video_id": spotify_id,
        "youtube_title": f"Song {spotify_id} (Official Audio)",
        "youtube_channel": "Test Artist - Topic",
        "youtube_duration_seconds": 180,
        "match_status": "suggested",
        "download_status": "not_downloaded",
        "updated_at": utc_now(),
    }
    defaults.update(values)
    columns = ", ".join(("spotify_id", *defaults))
    placeholders = ", ".join("?" for _ in range(len(defaults) + 1))
    connection.execute(
        f"INSERT INTO tracks ({columns}) VALUES ({placeholders})",
        (spotify_id, *defaults.values()),
    )


class MigrationTests(unittest.TestCase):
    def test_new_columns_are_added_without_replacing_legacy_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "legacy.sqlite"
            connection = sqlite3.connect(db)
            connection.executescript(
                """
                CREATE TABLE tracks (
                    spotify_id TEXT PRIMARY KEY,
                    match_status TEXT NOT NULL DEFAULT 'not_searched',
                    is_liked INTEGER NOT NULL DEFAULT 1
                );
                INSERT INTO tracks (spotify_id) VALUES ('keep-me');
                CREATE TABLE youtube_candidates (
                    spotify_id TEXT NOT NULL,
                    youtube_video_id TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    PRIMARY KEY (spotify_id, youtube_video_id)
                );
                """
            )
            connection.close()
            with connect_db(db) as migrated:
                columns = {
                    row[1] for row in migrated.execute("PRAGMA table_info(tracks)")
                }
                candidate_columns = {
                    row[1]
                    for row in migrated.execute(
                        "PRAGMA table_info(youtube_candidates)"
                    )
                }
                self.assertIn("youtube_score_version", columns)
                self.assertIn("is_saved_album", columns)
                self.assertIn("match_attempts", columns)
                self.assertIn("match_error", columns)
                self.assertIn("match_error_code", columns)
                self.assertIn("match_next_retry_at", columns)
                self.assertIn("download_error_code", columns)
                self.assertIn("download_next_retry_at", columns)
                self.assertIn("downloaded_duration_seconds", columns)
                self.assertIn("youtube_channel_verified", candidate_columns)
                self.assertIn("source_tier", candidate_columns)
                tables = {
                    row[0]
                    for row in migrated.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertIn("match_assessments", tables)
                self.assertIn("workflow_events", tables)
                self.assertIn("apple_music_restore_plans", tables)
                self.assertEqual(
                    migrated.execute(
                        "SELECT spotify_id FROM tracks"
                    ).fetchone()["spotify_id"],
                    "keep-me",
                )


class ResetMatchesTests(unittest.TestCase):
    def test_below_score_resets_only_strictly_lower_scores(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "music.sqlite"
            with connect_db(db) as connection:
                insert_track(connection, "low", youtube_score=94.99)
                insert_track(connection, "boundary", youtube_score=95)
                insert_track(connection, "high", youtube_score=100)
                preview = reset_preview(
                    connection,
                    include_downloaded=False,
                    below_score=95,
                )
                self.assertEqual(preview["resettable"], 1)
                reset_current_matches(
                    connection,
                    include_downloaded=False,
                    below_score=95,
                )
                statuses = {
                    row["spotify_id"]: row["match_status"]
                    for row in connection.execute(
                        "SELECT spotify_id, match_status FROM tracks"
                    )
                }
                self.assertEqual(statuses["low"], "not_searched")
                self.assertEqual(statuses["boundary"], "suggested")
                self.assertEqual(statuses["high"], "suggested")

    def test_preview_is_read_only_and_apply_protects_downloaded_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "music.sqlite"
            with connect_db(db) as connection:
                insert_track(connection, "reset-me")
                insert_track(
                    connection,
                    "keep-downloaded",
                    match_status="approved_auto",
                    download_status="downloaded",
                    download_path="/Music/keep-downloaded.mp3",
                )
                for spotify_id in ("reset-me", "keep-downloaded"):
                    connection.execute(
                        """
                        INSERT INTO youtube_candidates (
                            spotify_id, youtube_video_id, rank, youtube_url,
                            score
                        ) VALUES (?, ?, 1, ?, 100)
                        """,
                        (
                            spotify_id,
                            spotify_id,
                            f"https://www.youtube.com/watch?v={spotify_id}",
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO match_assessments (
                        run_id, spotify_id, decision, scoring_version, created_at
                    ) VALUES ('old-run', 'reset-me', 'suggested', 1, ?)
                    """,
                    (utc_now(),),
                )

                preview = reset_preview(connection, include_downloaded=False)
                self.assertEqual(preview["resettable"], 1)
                self.assertEqual(preview["protected_downloaded"], 1)
                self.assertEqual(
                    connection.execute(
                        "SELECT match_status FROM tracks WHERE spotify_id = ?",
                        ("reset-me",),
                    ).fetchone()["match_status"],
                    "suggested",
                )

                reset_current_matches(
                    connection,
                    include_downloaded=False,
                )
                reset_row = connection.execute(
                    "SELECT * FROM tracks WHERE spotify_id = ?",
                    ("reset-me",),
                ).fetchone()
                protected_row = connection.execute(
                    "SELECT * FROM tracks WHERE spotify_id = ?",
                    ("keep-downloaded",),
                ).fetchone()
                self.assertEqual(reset_row["match_status"], "not_searched")
                self.assertIsNone(reset_row["youtube_url"])
                self.assertEqual(reset_row["download_status"], "not_downloaded")
                self.assertEqual(
                    protected_row["match_status"], "approved_auto"
                )
                self.assertEqual(
                    protected_row["download_path"],
                    "/Music/keep-downloaded.mp3",
                )
                remaining_candidates = connection.execute(
                    """
                    SELECT spotify_id FROM youtube_candidates
                    ORDER BY spotify_id
                    """
                ).fetchall()
                self.assertEqual(
                    [row["spotify_id"] for row in remaining_candidates],
                    ["keep-downloaded"],
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM match_assessments"
                    ).fetchone()[0],
                    1,
                )


class DownloadSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Path(self.temp_dir.name) / "music.sqlite"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_min_score_uses_existing_suggestion_without_refresh(self) -> None:
        with connect_db(self.db) as connection:
            insert_track(connection, "existing")
            rows, assessments = select_download_tracks(
                connection,
                min_score=95,
                retry_errors=False,
                redownload=False,
                batch_size=100,
                process_all=False,
            )
        self.assertEqual([row["spotify_id"] for row in rows], ["existing"])
        self.assertGreaterEqual(assessments["existing"][0], 95)

    def test_po_token_mode_selects_mweb_without_browser_cookies(self) -> None:
        command = yt_dlp_download_command(
            {
                "spotify_id": "spotify1",
                "youtube_url": "https://www.youtube.com/watch?v=video1",
            },
            Path("/tmp/partial"),
            use_po_token_provider=True,
        )
        self.assertIn("youtube:player_client=mweb", command)
        self.assertNotIn("--cookies", command)
        self.assertNotIn("--cookies-from-browser", command)

    def test_saved_album_only_track_enters_download_queue(self) -> None:
        with connect_db(self.db) as connection:
            insert_track(
                connection,
                "album-track",
                is_liked=0,
                is_saved_album=1,
                match_status="approved_manual",
            )
            rows, _assessments = select_download_tracks(
                connection,
                min_score=None,
                retry_errors=False,
                redownload=False,
                batch_size=100,
                process_all=False,
            )
        self.assertEqual([row["spotify_id"] for row in rows], ["album-track"])

    def test_errors_only_excludes_pending_tracks(self) -> None:
        with connect_db(self.db) as connection:
            insert_track(connection, "pending")
            insert_track(
                connection,
                "failed",
                download_status="error",
                download_error="temporary failure",
            )
            rows, _assessments = select_download_tracks(
                connection,
                min_score=95,
                retry_errors=True,
                redownload=False,
                batch_size=100,
                process_all=True,
                errors_only=True,
            )
        self.assertEqual([row["spotify_id"] for row in rows], ["failed"])

    def test_runtime_gate_blocks_high_scoring_existing_match(self) -> None:
        with connect_db(self.db) as connection:
            insert_track(connection, "wrong-runtime", youtube_duration_seconds=186)
            rows, _assessments = select_download_tracks(
                connection,
                min_score=95,
                retry_errors=False,
                redownload=False,
                batch_size=100,
                process_all=False,
            )
        self.assertEqual(rows, [])

    def test_current_policy_hydrated_score_is_reused_after_safety_gates(self) -> None:
        with connect_db(self.db) as connection:
            insert_track(
                connection,
                "hydrated",
                title="I Won't Say (I'm In Love)",
                artists="Susan Egan",
                primary_artist="Susan Egan",
                youtube_title=(
                    "I Won't Say (I'm in Love) "
                    "[From Disney's Hercules]"
                ),
                youtube_channel="Susan Egan",
                youtube_channel_verified=1,
                youtube_score=98,
                youtube_score_version=SCORING_VERSION,
                match_status="approved_auto",
            )
            rows, assessments = select_download_tracks(
                connection,
                min_score=95,
                retry_errors=False,
                redownload=False,
                batch_size=100,
                process_all=False,
            )
        self.assertEqual([row["spotify_id"] for row in rows], ["hydrated"])
        self.assertEqual(assessments["hydrated"][0], 98)

    def test_old_incompatible_hydrated_score_is_not_reused(self) -> None:
        with connect_db(self.db) as connection:
            insert_track(
                connection,
                "old-score",
                title="I Won't Say (I'm In Love)",
                artists="Susan Egan",
                primary_artist="Susan Egan",
                youtube_title=(
                    "I Won't Say (I'm in Love) "
                    "[From Disney's Hercules]"
                ),
                youtube_channel="Susan Egan",
                youtube_channel_verified=1,
                youtube_score=100,
                youtube_score_version=5,
                match_status="approved_auto",
            )
            rows, _assessments = select_download_tracks(
                connection,
                min_score=95,
                retry_errors=False,
                redownload=False,
                batch_size=100,
                process_all=False,
            )
        self.assertEqual(rows, [])

    def test_pending_tracks_precede_retried_errors_and_batch_resumes(self) -> None:
        with connect_db(self.db) as connection:
            insert_track(
                connection,
                "done",
                match_status="approved_manual",
                download_status="downloaded",
            )
            insert_track(
                connection,
                "failed",
                match_status="approved_manual",
                download_status="error",
            )
            insert_track(
                connection,
                "pending",
                match_status="approved_manual",
                download_status="not_downloaded",
            )
            first, _ = select_download_tracks(
                connection,
                min_score=None,
                retry_errors=True,
                redownload=False,
                batch_size=1,
                process_all=False,
            )
            later, _ = select_download_tracks(
                connection,
                min_score=None,
                retry_errors=True,
                redownload=False,
                batch_size=100,
                process_all=False,
            )
        self.assertEqual([row["spotify_id"] for row in first], ["pending"])
        self.assertEqual(
            [row["spotify_id"] for row in later], ["pending", "failed"]
        )


class FileNamingTests(unittest.TestCase):
    def test_collision_gets_stable_spotify_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            track = {
                "spotify_id": "abcdefgh1234",
                "primary_artist": "Artist/Name",
                "album": "Album: Name",
                "release_year": 2024,
                "disc_number": 1,
                "track_number": 3,
                "title": "Title?",
            }
            first = final_path(output, track)
            self.assertEqual(
                first.relative_to(output).as_posix(),
                "Artist-Name/2024 - Album- Name/03 - Title.mp3",
            )
            first.parent.mkdir(parents=True)
            first.write_bytes(b"not an mp3")
            second = final_path(output, track)
            self.assertEqual(second.name, "03 - Title [abcdefgh].mp3")


class StatusTests(unittest.TestCase):
    def test_progress_totals_cover_requested_stages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "music.sqlite"
            with connect_db(db) as connection:
                insert_track(
                    connection,
                    "complete",
                    match_status="approved_manual",
                    download_status="downloaded",
                    apple_music_status="preferred_download",
                )
                insert_track(
                    connection,
                    "failed",
                    match_status="match_error",
                    download_status="error",
                )
                counts = progress_counts(connection)
        self.assertEqual(counts["synced"], 2)
        self.assertEqual(counts["matched"], 2)
        self.assertEqual(counts["approved"], 1)
        self.assertEqual(counts["downloaded"], 1)
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["added_to_apple_music"], 1)


if __name__ == "__main__":
    unittest.main()
