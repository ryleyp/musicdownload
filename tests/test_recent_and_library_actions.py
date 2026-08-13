from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import liked_songs_playlist
import music_album_artist_cleanup
import music_delete_queue
import recent_additions
from common import connect_db, utc_now
from download_mp3 import select_download_tracks


def insert_track(connection, spotify_id: str, **values) -> None:
    defaults = {
        "title": f"Song {spotify_id}",
        "artists": "Artist",
        "primary_artist": "Artist",
        "album": "Album",
        "duration_ms": 180000,
        "is_liked": 1,
        "is_saved_album": 0,
        "user_deleted": 0,
        "added_at": datetime.now(timezone.utc).isoformat(),
        "youtube_url": f"https://youtube.test/{spotify_id}",
        "youtube_title": f"Song {spotify_id}",
        "youtube_channel": "Artist - Topic",
        "youtube_duration_seconds": 180,
        "youtube_score": 99,
        "match_status": "approved_automatic",
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


class RecentAdditionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = self.root / "library.sqlite"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_recent_report_includes_likes_and_saved_albums_but_not_blocked(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        with connect_db(self.db) as connection:
            insert_track(connection, "liked")
            insert_track(connection, "album", is_liked=0, is_saved_album=1)
            insert_track(connection, "both", is_saved_album=1)
            insert_track(connection, "blocked", user_deleted=1)
            insert_track(connection, "old", added_at=old)
            rows = recent_additions.recent_rows(
                connection,
                (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
            )
        by_id = {row["spotify_id"]: row for row in rows}
        self.assertEqual(set(by_id), {"liked", "album", "both"})
        self.assertEqual(by_id["liked"]["library_source"], "liked_song")
        self.assertEqual(by_id["album"]["library_source"], "saved_album")
        self.assertEqual(
            by_id["both"]["library_source"], "liked_song + saved_album"
        )
        self.assertEqual(by_id["liked"]["runtime_difference_seconds"], 0.0)

    def test_recent_main_is_report_only_by_default(self) -> None:
        report = self.root / "recent.csv"
        xlsx = self.root / "recent.xlsx"
        with connect_db(self.db) as connection:
            insert_track(connection, "liked")
        with (
            patch.object(recent_additions, "run_stage") as runner,
            redirect_stdout(io.StringIO()),
        ):
            result = recent_additions.main(
                ["--db", str(self.db), "--report", str(report), "--xlsx", str(xlsx)]
            )
        self.assertEqual(result, 0)
        runner.assert_not_called()
        self.assertTrue(report.exists())
        self.assertTrue(xlsx.exists())

    def test_download_selection_can_be_limited_to_recent_ids_and_blocklist(self) -> None:
        with connect_db(self.db) as connection:
            insert_track(connection, "wanted")
            insert_track(connection, "other")
            insert_track(connection, "blocked", user_deleted=1)
            rows, _ = select_download_tracks(
                connection,
                min_score=None,
                retry_errors=False,
                redownload=False,
                batch_size=100,
                process_all=True,
                spotify_ids=["wanted", "blocked"],
            )
        self.assertEqual([row["spotify_id"] for row in rows], ["wanted"])


class LikedPlaylistTests(unittest.TestCase):
    def test_liked_rows_are_newest_first_and_exclude_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "library.sqlite"
            with connect_db(db) as connection:
                insert_track(connection, "older", added_at="2026-01-01T00:00:00Z")
                insert_track(connection, "newer", added_at="2026-02-01T00:00:00Z")
                insert_track(
                    connection,
                    "blocked",
                    added_at="2026-03-01T00:00:00Z",
                    user_deleted=1,
                )
                rows = liked_songs_playlist.liked_rows(connection)
        self.assertEqual([row["spotify_id"] for row in rows], ["newer", "older"])

    def test_build_report_preserves_spotify_order_and_only_ready_music(self) -> None:
        spotify = [
            {
                "spotify_id": "new", "added_at": "2", "title": "New",
                "artists": "Artist", "album": "Album", "spotify_url": "url",
            },
            {
                "spotify_id": "old", "added_at": "1", "title": "Old",
                "artists": "Artist", "album": "Album", "spotify_url": "url",
            },
        ]
        music = {
            "new": {"persistent_id": "PIDNEW", "location": "/new.mp3"}
        }
        report, desired = liked_songs_playlist.build_report(spotify, music)
        self.assertEqual(desired, ["PIDNEW"])
        self.assertEqual(report[0]["position"], 1)
        self.assertEqual(report[1]["status"], "not_downloaded_or_not_in_music")


class DeleteQueueTests(unittest.TestCase):
    def test_delete_plan_protects_vinyl_shared_files_and_nonlocal_items(self) -> None:
        music = [
            {
                "persistent_id": "NORMAL", "title": "Song", "artist": "Artist",
                "album": "Album", "location": "/music/song.mp3",
                "comment": "SPOTIFY_ARCHIVE_ID=spotify1",
            },
            {
                "persistent_id": "SHARED", "title": "Shared", "artist": "Artist",
                "album": "Album", "location": "/music/shared.mp3", "comment": "",
            },
            {
                "persistent_id": "SHARED2", "title": "Shared 2", "artist": "Artist",
                "album": "Album", "location": "/music/shared.mp3", "comment": "",
            },
            {
                "persistent_id": "VINYL", "title": "Vinyl", "artist": "Artist",
                "album": "Album (VINYL)", "location": "/music/vinyl.mp3", "comment": "",
            },
        ]
        rows = music_delete_queue.build_rows(
            ["NORMAL", "SHARED", "VINYL", "CLOUD"],
            music,
            {"spotify1": "/downloads/song.mp3"},
        )
        by_id = {row["music_persistent_id"]: row for row in rows}
        self.assertEqual(by_id["NORMAL"]["action"], "would_delete")
        self.assertEqual(by_id["SHARED"]["shared_music_file"], "yes")
        self.assertEqual(by_id["VINYL"]["action"], "protected_vinyl")
        self.assertEqual(by_id["CLOUD"]["action"], "not_local_file_track")
        self.assertEqual(
            music_delete_queue.unique_trash_paths(by_id["SHARED"]), []
        )

    def test_apply_requires_exact_playlist_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "library.sqlite"
            with (
                patch.object(music_delete_queue, "require_mac"),
                redirect_stderr(io.StringIO()),
            ):
                result = music_delete_queue.main(
                    ["--db", str(db), "--apply", "--confirm", "wrong"]
                )
        self.assertEqual(result, 1)

    def test_schema_and_unblock_preserve_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "library.sqlite"
            with connect_db(db) as connection:
                insert_track(
                    connection,
                    "blocked",
                    user_deleted=1,
                    download_status="deleted_by_user",
                )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    music_delete_queue.main(
                        ["--db", str(db), "--unblock", "blocked"]
                    ),
                    0,
                )
            with connect_db(db) as connection:
                row = connection.execute(
                    "SELECT user_deleted, download_status FROM tracks "
                    "WHERE spotify_id='blocked'"
                ).fetchone()
                tables = {
                    item[0]
                    for item in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
        self.assertEqual(row["user_deleted"], 0)
        self.assertEqual(row["download_status"], "not_downloaded")
        self.assertIn("music_delete_runs", tables)
        self.assertIn("music_delete_items", tables)

    def test_trash_failure_marks_apply_run_partial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "library.sqlite"
            args = music_delete_queue.parse_args(
                ["--db", str(db), "--apply", "--confirm", "delete me pls"]
            )
            rows = [{
                "music_persistent_id": "PID",
                "spotify_id": "spotify1",
                "title": "Song",
                "artist": "Artist",
                "album": "Album",
                "music_location": "/music/song.mp3",
                "download_path": "",
                "shared_music_file": "no",
                "shared_download_file": "no",
                "action": "would_delete",
                "reason": "eligible",
            }]
            with connect_db(db) as connection:
                insert_track(connection, "spotify1")
                with (
                    patch.object(
                        music_delete_queue,
                        "run_bridge",
                        side_effect=["deleted", "trash_error"],
                    ),
                    redirect_stdout(io.StringIO()),
                ):
                    _run_id, deleted, status = music_delete_queue.apply_rows(
                        connection, args, rows
                    )
                run = connection.execute(
                    "SELECT status FROM music_delete_runs"
                ).fetchone()
        self.assertEqual(deleted, 1)
        self.assertEqual(status, "partial")
        self.assertEqual(run["status"], "partial")


class AlbumArtistCleanupTests(unittest.TestCase):
    def test_hadestown_cleanup_preserves_track_artist_and_normalizes_release(self) -> None:
        rows = music_album_artist_cleanup.build_rows([{
            "persistent_id": "PID",
            "title": "Road to Hell",
            "artist": "André De Shields; Hadestown Original Broadway Company",
            "album_artist": "Hadestown",
            "album": "Hadestown (Original Broadway Cast Recording)",
            "compilation": False,
        }])
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["track_artist"],
            "André De Shields; Hadestown Original Broadway Company",
        )
        self.assertEqual(
            rows[0]["new_album_artist"],
            "Original Broadway Cast of Hadestown",
        )
        self.assertTrue(rows[0]["new_compilation"])
        self.assertEqual(rows[0]["action"], "would_update")

    def test_unrelated_and_vinyl_albums_are_not_changed(self) -> None:
        rows = music_album_artist_cleanup.build_rows([
            {
                "persistent_id": "OTHER", "title": "Song", "artist": "Artist",
                "album_artist": "Artist", "album": "Other", "compilation": False,
            },
            {
                "persistent_id": "VINYL", "title": "Song", "artist": "Cast",
                "album_artist": "Wrong",
                "album": "Hadestown (Original Broadway Cast Recording) (VINYL)",
                "compilation": False,
            },
        ])
        self.assertEqual(rows, [])

    def test_noop_apply_does_not_create_empty_audit_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "library.sqlite"
            report = Path(directory) / "report.csv"
            normalized = {
                "persistent_id": "PID", "title": "Road to Hell",
                "artist": "André De Shields",
                "album_artist": "Original Broadway Cast of Hadestown",
                "album": "Hadestown (Original Broadway Cast Recording)",
                "compilation": True,
            }
            with (
                patch.object(music_album_artist_cleanup, "require_mac"),
                patch.object(
                    music_album_artist_cleanup,
                    "scan_music_metadata",
                    return_value=[normalized],
                ),
                redirect_stdout(io.StringIO()),
            ):
                result = music_album_artist_cleanup.main([
                    "--db", str(db), "--report", str(report), "--apply",
                ])
            with connect_db(db) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM music_album_artist_runs"
                ).fetchone()[0]
        self.assertEqual(result, 0)
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
