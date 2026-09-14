from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from download_mp3 import (
    probe_duration_seconds,
    trailing_silence_start,
    trim_trailing_silence,
)


def build_mp3(path: Path, filter_spec: str) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-filter_complex", filter_spec, "-map", "[a]",
         "-c:a", "libmp3lame", str(path)],
        check=True, capture_output=True,
    )


@unittest.skipIf(shutil.which("ffmpeg") is None, "ffmpeg is required")
class SilenceTrimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="silence-trim-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def test_trailing_pad_is_removed_and_a_short_tail_is_kept(self) -> None:
        path = self.directory / "padded.mp3"
        # Five seconds of tone followed by four seconds of digital silence,
        # the shape of a YouTube "Topic" upload.
        build_mp3(path, "sine=frequency=440:duration=5,apad=pad_dur=4[a]")
        trimmed = trim_trailing_silence(path)
        self.assertIsNotNone(trimmed)
        # Tone plus the deliberate tail, nowhere near the original nine seconds.
        self.assertGreater(trimmed, 5.0)
        self.assertLess(trimmed, 6.0)

    def test_a_file_without_padding_is_left_alone(self) -> None:
        path = self.directory / "clean.mp3"
        build_mp3(path, "sine=frequency=440:duration=5[a]")
        before = probe_duration_seconds(path)
        self.assertIsNone(trim_trailing_silence(path))
        self.assertAlmostEqual(before, probe_duration_seconds(path), places=2)

    def test_an_interior_gap_is_not_mistaken_for_padding(self) -> None:
        path = self.directory / "gap.mp3"
        # Silence in the middle: a quiet bridge must never truncate the track.
        build_mp3(
            path,
            "sine=frequency=440:duration=3[x];"
            "anullsrc=r=44100:cl=mono,atrim=duration=2[y];"
            "sine=frequency=440:duration=3[z];"
            "[x][y][z]concat=n=3:v=0:a=1[a]",
        )
        total = probe_duration_seconds(path)
        self.assertIsNone(trailing_silence_start(path, total))
        self.assertIsNone(trim_trailing_silence(path))

    def test_a_failed_trim_leaves_the_original_intact(self) -> None:
        missing = self.directory / "does-not-exist.mp3"
        self.assertIsNone(trim_trailing_silence(missing))


if __name__ == "__main__":
    unittest.main()
