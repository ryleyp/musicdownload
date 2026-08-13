from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
import io
from pathlib import Path
from unittest.mock import MagicMock, patch

from activity_log import record_event
import download_mp3
import repair_artwork
from common import connect_db, export_catalog, utc_now
from download_mp3 import DownloadRuntimeMismatch, validate_downloaded_runtime
from failures import classify_failure, next_retry_time, retry_delay_seconds
from import_review import record_review_decision
from matching_rules import SCORING_VERSION
from youtube_match import (
    candidate_score,
    candidate_record,
    hydrate_candidate,
    save_candidates,
    source_tier,
)


def add_track(connection: sqlite3.Connection, spotify_id: str = "spotify1") -> None:
    connection.execute(
        """
        INSERT INTO tracks (
            spotify_id, title, artists, primary_artist, album, duration_ms,
            explicit, is_liked, match_status, download_status, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'not_searched', 'not_downloaded', ?)
        """,
        (
            spotify_id,
            "Test Song",
            "Test Artist",
            "Test Artist",
            "Anniversary Remastered Album",
            180000,
            0,
            utc_now(),
        ),
    )


class CandidateHydrationTests(unittest.TestCase):
    def test_full_metadata_replaces_flat_duration_and_verification(self) -> None:
        class FakeYDL:
            def extract_info(self, _url: str, download: bool = False):
                return {
                    "id": "video1",
                    "webpage_url": "https://youtube.test/video1",
                    "title": "Test Song (Official Audio)",
                    "channel": "TestArtistMusic",
                    "duration": 181.4,
                    "channel_is_verified": True,
                }

        flat = {
            "id": "video1",
            "webpage_url": "https://youtube.test/video1",
            "title": "Test Song",
            "channel": "Unknown",
            "duration": None,
        }
        hydrated, error = hydrate_candidate(FakeYDL(), flat)
        self.assertIsNone(error)
        self.assertEqual(hydrated["duration"], 181.4)
        self.assertTrue(hydrated["channel_is_verified"])
        record = candidate_record(
            {
                "title": "Test Song",
                "artists": "Test Artist",
                "primary_artist": "Test Artist",
                "album": "Test Album",
                "duration_ms": 181000,
                "explicit": 0,
            },
            hydrated,
        )
        self.assertIn("youtube_channel_verified", record)
        self.assertTrue(record["youtube_channel_verified"])

    def test_topic_and_verified_sources_outrank_unrelated_uploaders(self) -> None:
        track = {
            "title": "Test Song",
            "artists": "Test Artist",
            "primary_artist": "Test Artist",
            "album": "Test Album",
            "duration_ms": 180000,
            "explicit": 0,
        }
        topic = {
            "title": "Test Song",
            "channel": "Test Artist - Topic",
            "duration": 180,
        }
        unrelated = {
            "title": "Test Artist - Test Song (Official Audio)",
            "channel": "Upload Archive",
            "duration": 180,
        }
        self.assertGreater(source_tier(track, topic), source_tier(track, unrelated))


class VersionPolicyTests(unittest.TestCase):
    def test_album_can_confirm_exact_remastered_version(self) -> None:
        track = {
            "title": "Test Song",
            "artists": "Test Artist",
            "primary_artist": "Test Artist",
            "album": "Anniversary Remastered Album",
            "duration_ms": 180000,
            "explicit": 0,
        }
        candidate = {
            "title": "Test Song (Remastered)",
            "channel": "Test Artist - Topic",
            "duration": 180,
        }
        _score, notes, rejected = candidate_score(track, candidate)
        self.assertFalse(rejected)
        self.assertIn("Spotify confirms version: remastered", notes)

    def test_explicit_spotify_track_rejects_unexpected_clean_upload(self) -> None:
        track = {
            "title": "Test Song",
            "artists": "Test Artist",
            "primary_artist": "Test Artist",
            "album": "Test Album",
            "duration_ms": 180000,
            "explicit": 1,
        }
        candidate = {
            "title": "Test Song (Clean)",
            "channel": "Test Artist - Topic",
            "duration": 180,
        }
        _score, _notes, rejected = candidate_score(track, candidate)
        self.assertTrue(rejected)

    def test_unexpected_featured_artist_is_rejected(self) -> None:
        track = {
            "title": "Test Song",
            "artists": "Test Artist; Known Guest",
            "primary_artist": "Test Artist",
            "album": "Test Album",
            "duration_ms": 180000,
            "explicit": 0,
        }
        unexpected = {
            "title": "Test Song feat. Different Guest (Official Audio)",
            "channel": "Test Artist",
            "duration": 180,
        }
        _score, notes, rejected = candidate_score(track, unexpected)
        self.assertTrue(rejected)
        self.assertTrue(
            any("unexpected featured artist" in note for note in notes)
        )

        confirmed = dict(
            unexpected,
            title="Test Song feat. Known Guest (Official Audio)",
        )
        _score, notes, rejected = candidate_score(track, confirmed)
        self.assertFalse(rejected)
        self.assertTrue(
            any("confirms featured artist" in note for note in notes)
        )


class AuditHistoryTests(unittest.TestCase):
    def test_selection_writes_immutable_assessment_and_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "music.sqlite"
            with connect_db(db) as connection:
                add_track(connection)
                track = connection.execute(
                    "SELECT * FROM tracks WHERE spotify_id = 'spotify1'"
                ).fetchone()
                save_candidates(
                    connection,
                    track,
                    [
                        {
                            "youtube_video_id": "video1",
                            "youtube_url": "https://youtube.test/video1",
                            "youtube_title": "Test Song",
                            "youtube_channel": "Test Artist - Topic",
                            "youtube_channel_verified": 0,
                            "youtube_duration_seconds": 180,
                            "score": 100.0,
                            "score_notes": "safe",
                            "hard_reject": 0,
                            "rank": 1,
                        }
                    ],
                    95,
                    root / "activity.jsonl",
                )
                assessment = connection.execute(
                    "SELECT * FROM match_assessments"
                ).fetchone()
                event = connection.execute(
                    "SELECT * FROM workflow_events"
                ).fetchone()
            self.assertEqual(assessment["decision"], "approved_auto")
            self.assertEqual(assessment["scoring_version"], SCORING_VERSION)
            self.assertEqual(event["event"], "candidate_selected")
            payload = json.loads(
                (root / "activity.jsonl").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["spotify_id"], "spotify1")

    def test_manual_review_decision_is_audited(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "music.sqlite"
            with connect_db(db) as connection:
                add_track(connection)
                connection.execute(
                    """
                    UPDATE tracks
                    SET match_status = 'approved_manual',
                        youtube_url = 'https://youtube.test/video1',
                        youtube_video_id = 'video1',
                        youtube_title = 'Test Song'
                    WHERE spotify_id = 'spotify1'
                    """
                )
                record_review_decision(
                    connection,
                    "spotify1",
                    "approved_manual",
                    root / "activity.jsonl",
                )
                row = connection.execute(
                    "SELECT * FROM match_assessments"
                ).fetchone()
            self.assertEqual(row["decision"], "approved_manual")


class RuntimeVerificationTests(unittest.TestCase):
    def test_tagger_reuses_existing_empty_id3_container(self) -> None:
        from mutagen.id3 import ID3

        audio = MagicMock()
        audio.tags = ID3()
        track = {
            "title": "Test Song",
            "artists": "Test Artist",
            "album_artist": "Test Artist",
            "primary_artist": "Test Artist",
            "album": "Test Album",
            "release_date": "2026-01-01",
            "genres": "Pop",
            "track_number": 1,
            "total_tracks": 10,
            "disc_number": 1,
            "isrc": "TEST123",
            "spotify_url": "https://open.spotify.com/track/spotify1",
            "youtube_url": "https://youtube.test/video1",
            "spotify_id": "spotify1",
            "youtube_video_id": "video1",
            "explicit": 0,
        }
        with patch("mutagen.mp3.MP3", return_value=audio):
            download_mp3.tag_mp3(Path("/tmp/not-opened.mp3"), track, None)
        audio.add_tags.assert_not_called()
        audio.save.assert_called_once_with(v2_version=3)
        self.assertEqual(audio.tags["TIT2"].text, ["Test Song"])

    def test_downloaded_mp3_runtime_must_match_spotify(self) -> None:
        track = {"duration_ms": 180000}
        with patch("mutagen.mp3.MP3") as mp3:
            mp3.return_value.info.length = 182.25
            actual, difference = validate_downloaded_runtime(
                Path("/tmp/not-opened.mp3"), track
            )
        self.assertEqual(actual, 182.25)
        self.assertEqual(difference, 2.25)

    def test_downloaded_runtime_mismatch_is_rejected(self) -> None:
        track = {"duration_ms": 180000}
        with (
            patch("mutagen.mp3.MP3") as mp3,
            self.assertRaises(DownloadRuntimeMismatch),
        ):
            mp3.return_value.info.length = 190.0
            validate_downloaded_runtime(Path("/tmp/not-opened.mp3"), track)

    def test_successful_download_persists_verified_runtime_and_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "music.sqlite"
            source = root / "source.mp3"
            source.write_bytes(b"mock mp3")
            with connect_db(db) as connection:
                add_track(connection)
                connection.execute(
                    """
                    UPDATE tracks
                    SET match_status = 'approved_manual',
                        youtube_url = 'https://youtube.test/video1',
                        youtube_video_id = 'video1',
                        youtube_title = 'Test Song',
                        youtube_channel = 'Test Artist - Topic',
                        youtube_duration_seconds = 180
                    WHERE spotify_id = 'spotify1'
                    """
                )
            argv = [
                "download_mp3.py",
                "--db",
                str(db),
                "--output",
                str(root / "downloads"),
                "--sleep-min-seconds",
                "0",
                "--sleep-max-seconds",
                "0",
            ]
            with (
                patch.object(download_mp3, "check_dependencies"),
                patch.object(
                    download_mp3, "download_audio", return_value=source
                ),
                patch.object(
                    download_mp3,
                    "validate_downloaded_runtime",
                    return_value=(180.25, 0.25),
                ),
                patch.object(download_mp3, "fetch_artwork", return_value=None),
                patch.object(download_mp3, "tag_mp3"),
                patch.object(sys, "argv", argv),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(download_mp3.main(), 0)
            with connect_db(db) as connection:
                track = connection.execute(
                    "SELECT * FROM tracks WHERE spotify_id = 'spotify1'"
                ).fetchone()
                event = connection.execute(
                    """
                    SELECT * FROM workflow_events
                    WHERE event = 'attempt_completed'
                    """
                ).fetchone()
            self.assertEqual(track["download_status"], "downloaded")
            self.assertEqual(track["downloaded_duration_seconds"], 180.25)
            self.assertEqual(
                track["downloaded_duration_difference_seconds"], 0.25
            )
            self.assertEqual(event["status"], "downloaded")

    def test_consecutive_authentication_errors_stop_the_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "music.sqlite"
            with connect_db(db) as connection:
                for index in range(3):
                    spotify_id = f"spotify{index}"
                    add_track(connection, spotify_id)
                    connection.execute(
                        """
                        UPDATE tracks
                        SET match_status = 'approved_manual',
                            youtube_url = ?,
                            youtube_video_id = ?,
                            youtube_title = 'Test Song',
                            youtube_channel = 'Test Artist - Topic',
                            youtube_duration_seconds = 180
                        WHERE spotify_id = ?
                        """,
                        (
                            f"https://youtube.test/{spotify_id}",
                            spotify_id,
                            spotify_id,
                        ),
                    )
            argv = [
                "download_mp3.py",
                "--db",
                str(db),
                "--output",
                str(root / "downloads"),
                "--all",
                "--sleep-min-seconds",
                "0",
                "--sleep-max-seconds",
                "0",
                "--max-consecutive-auth-errors",
                "2",
            ]
            with (
                patch.object(download_mp3, "check_dependencies"),
                patch.object(
                    download_mp3,
                    "download_audio",
                    side_effect=Exception("Sign in to confirm you're not a bot"),
                ) as download,
                patch.object(sys, "argv", argv),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(download_mp3.main(), 1)
            self.assertEqual(download.call_count, 2)

    def test_authentication_cooldown_is_persisted_and_track_is_requeued(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "music.sqlite"
            source = root / "source.mp3"
            source.write_bytes(b"mock mp3")
            with connect_db(db) as connection:
                add_track(connection)
                connection.execute(
                    """
                    UPDATE tracks
                    SET match_status = 'approved_manual',
                        youtube_url = 'https://youtube.test/video1',
                        youtube_video_id = 'video1',
                        youtube_title = 'Test Song',
                        youtube_channel = 'Test Artist - Topic',
                        youtube_duration_seconds = 180
                    WHERE spotify_id = 'spotify1'
                    """
                )
            argv = [
                "download_mp3.py", "--db", str(db),
                "--output", str(root / "downloads"),
                "--sleep-min-seconds", "0", "--sleep-max-seconds", "0",
                "--auth-cooldown-min-seconds", "10",
                "--auth-cooldown-max-seconds", "20",
                "--max-consecutive-auth-errors", "4",
            ]
            with (
                patch.object(download_mp3, "check_dependencies"),
                patch.object(
                    download_mp3,
                    "download_audio",
                    side_effect=[
                        Exception("Sign in to confirm you're not a bot"),
                        source,
                    ],
                ) as download,
                patch.object(
                    download_mp3,
                    "validate_downloaded_runtime",
                    return_value=(180.0, 0.0),
                ),
                patch.object(download_mp3, "fetch_artwork", return_value=None),
                patch.object(download_mp3, "tag_mp3"),
                patch.object(download_mp3.time, "sleep") as sleep,
                patch.object(sys, "argv", argv),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(download_mp3.main(), 0)
            self.assertEqual(download.call_count, 2)
            sleep.assert_called_once()
            with connect_db(db) as connection:
                track = connection.execute(
                    "SELECT download_status FROM tracks WHERE spotify_id = 'spotify1'"
                ).fetchone()
                state = connection.execute(
                    "SELECT value FROM workflow_state WHERE key = ?",
                    (download_mp3.YOUTUBE_COOLDOWN_KEY,),
                ).fetchone()
            self.assertEqual(track["download_status"], "downloaded")
            self.assertIsNone(state)

    def test_artwork_repair_embeds_only_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "music.sqlite"
            song = root / "song.mp3"
            song.write_bytes(b"mock mp3")
            with connect_db(db) as connection:
                add_track(connection)
                connection.execute(
                    """
                    UPDATE tracks
                    SET download_status = 'downloaded', download_path = ?,
                        cover_url = 'https://spotify.test/cover.jpg'
                    WHERE spotify_id = 'spotify1'
                    """,
                    (str(song),),
                )
            argv = ["repair_artwork.py", "--db", str(db)]
            with (
                patch.object(
                    repair_artwork, "has_embedded_artwork", return_value=False
                ),
                patch.object(
                    repair_artwork,
                    "fetch_artwork",
                    return_value=(b"image", "image/jpeg"),
                ),
                patch.object(repair_artwork, "tag_mp3") as tag,
                patch.object(sys, "argv", argv),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(repair_artwork.main(), 0)
            tag.assert_called_once()


class FailureSchedulingTests(unittest.TestCase):
    def test_failures_are_classified_and_backoff_is_bounded(self) -> None:
        code, retryable = classify_failure("HTTP Error 429", "download")
        self.assertEqual(code, "download_rate_limited")
        self.assertTrue(retryable)
        self.assertEqual(retry_delay_seconds(1, code), 900)
        self.assertLessEqual(retry_delay_seconds(20, code), 86400)
        self.assertIsNotNone(next_retry_time(1, code, retryable))

    def test_runtime_mismatch_is_not_automatically_retried(self) -> None:
        code, retryable = classify_failure("runtime mismatch", "download")
        self.assertFalse(retryable)
        self.assertIsNone(next_retry_time(1, code, retryable))


class AtomicExportTests(unittest.TestCase):
    def test_existing_exports_are_backed_up_before_atomic_replace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "music.sqlite"
            csv_path = root / "review.csv"
            xlsx_path = root / "review.xlsx"
            csv_path.write_bytes(b"old csv")
            xlsx_path.write_bytes(b"old workbook")
            with connect_db(db) as connection:
                add_track(connection)
                export_catalog(connection, csv_path, xlsx_path)
            backups = list((root / "backups").glob("*/*"))
            contents = {path.read_bytes() for path in backups}
            self.assertIn(b"old csv", contents)
            self.assertIn(b"old workbook", contents)
            self.assertTrue(csv_path.read_text(encoding="utf-8-sig").startswith(
                "decision,"
            ))
            self.assertGreater(xlsx_path.stat().st_size, 1000)


class StructuredLogTests(unittest.TestCase):
    def test_event_is_written_to_database_and_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "music.sqlite"
            log = root / "activity.jsonl"
            with connect_db(db) as connection:
                add_track(connection)
                record_event(
                    connection,
                    stage="test",
                    event="checked",
                    status="ok",
                    spotify_id="spotify1",
                    details={"value": 1},
                    log_path=log,
                )
                row = connection.execute(
                    "SELECT * FROM workflow_events"
                ).fetchone()
            self.assertEqual(row["status"], "ok")
            self.assertEqual(json.loads(log.read_text())["details"]["value"], 1)


class PackagingTests(unittest.TestCase):
    def test_console_entry_point_is_declared_and_importable(self) -> None:
        from music_library_app.cli import main

        self.assertTrue(callable(main))
        project = Path(__file__).resolve().parents[1]
        configuration = tomllib.loads(
            (project / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            configuration["project"]["scripts"]["music-library"],
            "music_library_app.cli:main",
        )


if __name__ == "__main__":
    unittest.main()
