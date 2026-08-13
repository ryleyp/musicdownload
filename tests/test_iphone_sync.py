from __future__ import annotations

import csv
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import iphone_sync


class IPhoneSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.audio = self.root / "song.mp3"
        self.audio.write_bytes(b"audio")
        self.tracks = [
            {
                "persistent_id": "READY1",
                "title": "Ready Song",
                "artist": "Artist",
                "album": "Album",
                "duration": 180.0,
                "enabled": True,
                "location": str(self.audio),
                "comment": "SPOTIFY_ARCHIVE_ID=spotify1",
            },
            {
                "persistent_id": "DISABLED1",
                "title": "Disabled Song",
                "artist": "Artist",
                "album": "Album",
                "duration": 200.0,
                "enabled": False,
                "location": str(self.audio),
                "comment": "",
            },
        ]

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_manifest_marks_only_enabled_existing_files_ready(self) -> None:
        rows, ready, total = iphone_sync.build_manifest(
            self.tracks, ["READY1", "DISABLED1", "MISSING1"]
        )
        self.assertEqual(ready, ["READY1"])
        self.assertEqual(total, 5)
        self.assertEqual(rows[1]["reason"], "disabled_in_music")
        self.assertEqual(rows[2]["reason"], "playlist_entry_not_local_or_missing")

    def test_report_only_never_changes_music_playlist(self) -> None:
        report = self.root / "manifest.csv"
        db = self.root / "music.sqlite"
        argv = ["--db", str(db), "--report", str(report)]
        with (
            patch.object(iphone_sync, "require_mac"),
            patch.object(iphone_sync, "scan_music_library", return_value=self.tracks),
            patch.object(iphone_sync, "playlist_persistent_ids", return_value=["READY1"]),
            patch.object(iphone_sync, "add_to_sync_playlist") as add,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(iphone_sync.main(argv), 0)
        add.assert_not_called()
        with report.open(encoding="utf-8-sig") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["persistent_id"], "READY1")
        self.assertEqual(row["ready"], "yes")

    def test_apply_is_additive_and_uses_only_ready_ids(self) -> None:
        report = self.root / "manifest.csv"
        db = self.root / "music.sqlite"
        argv = ["--db", str(db), "--report", str(report), "--apply"]
        with (
            patch.object(iphone_sync, "require_mac"),
            patch.object(iphone_sync, "scan_music_library", return_value=self.tracks),
            patch.object(
                iphone_sync,
                "playlist_persistent_ids",
                return_value=["READY1", "DISABLED1"],
            ),
            patch.object(
                iphone_sync,
                "add_to_sync_playlist",
                return_value={
                    "requested": 1,
                    "previous": 4,
                    "added": 1,
                    "missing": 0,
                    "final": 5,
                },
            ) as add,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(iphone_sync.main(argv), 0)
        add.assert_called_once_with(iphone_sync.DEFAULT_TARGET_PLAYLIST, ["READY1"])

    def test_all_local_does_not_require_source_playlist(self) -> None:
        rows, ready, _total = iphone_sync.build_manifest(self.tracks, None)
        self.assertEqual(len(rows), 2)
        self.assertEqual(ready, ["READY1"])


if __name__ == "__main__":
    unittest.main()
