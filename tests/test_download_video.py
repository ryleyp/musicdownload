from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import download_video


class VideoDownloadTests(unittest.TestCase):
    def test_default_command_outputs_quicktime_compatible_mov(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args = download_video.parse_args(
                ["https://youtube.test/watch?v=abc", "--output", directory]
            )
            command = download_video.yt_dlp_video_command(args, args.urls[0])
        self.assertIn("--recode-video", command)
        self.assertEqual(command[command.index("--recode-video") + 1], "mov")
        post_args = command[command.index("--postprocessor-args") + 1]
        self.assertIn("-c:v libx264", post_args)
        self.assertIn("-pix_fmt yuv420p", post_args)
        self.assertIn("-c:a aac", post_args)
        self.assertIn("-movflags +faststart", post_args)
        self.assertIn("--continue", command)
        self.assertIn("--no-overwrites", command)
        self.assertIn("--download-archive", command)
        self.assertIn("--no-playlist", command)
        self.assertTrue(any("%(title).180B" in item for item in command))

    def test_quality_limit_and_best_available_format(self) -> None:
        self.assertEqual(
            download_video.format_selector(720),
            "bv*[height<=720]+ba/b[height<=720]/best[height<=720]",
        )
        self.assertEqual(download_video.format_selector(0), "bv*+ba/b/best")

    def test_playlist_redownload_and_provider_flags_are_explicit(self) -> None:
        args = download_video.parse_args(
            [
                "https://youtube.test/playlist?list=abc",
                "--playlist",
                "--redownload",
                "--keep-source",
                "--po-token-provider",
            ]
        )
        command = download_video.yt_dlp_video_command(args, args.urls[0])
        self.assertIn("--yes-playlist", command)
        self.assertIn("--force-overwrites", command)
        self.assertNotIn("--download-archive", command)
        self.assertIn("--keep-video", command)
        self.assertIn("youtube:player_client=mweb", command)

    def test_dry_run_does_not_create_output_or_run_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "not-created"
            with (
                patch.object(download_video, "check_dependencies"),
                patch.object(download_video.subprocess, "run") as run,
                redirect_stdout(io.StringIO()),
            ):
                result = download_video.main(
                    [
                        "https://youtube.test/watch?v=abc",
                        "--output",
                        str(output),
                        "--dry-run",
                    ]
                )
        self.assertEqual(result, 0)
        self.assertFalse(output.exists())
        run.assert_not_called()

    def test_failed_url_does_not_block_later_video(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(download_video, "check_dependencies"),
                patch.object(
                    download_video.subprocess,
                    "run",
                    side_effect=[
                        SimpleNamespace(returncode=1),
                        SimpleNamespace(returncode=0),
                    ],
                ) as run,
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                result = download_video.main(
                    [
                        "https://youtube.test/watch?v=bad",
                        "https://youtube.test/watch?v=good",
                        "--output",
                        directory,
                    ]
                )
        self.assertEqual(result, 1)
        self.assertEqual(run.call_count, 2)

    def test_rejects_non_url_and_invalid_encoding_values(self) -> None:
        with self.assertRaisesRegex(Exception, "full http"):
            args = download_video.parse_args(["not-a-url"])
            download_video.validate_args(args)
        with self.assertRaisesRegex(Exception, "between 0 and 51"):
            args = download_video.parse_args(
                ["https://youtube.test/video", "--crf", "52"]
            )
            download_video.validate_args(args)


if __name__ == "__main__":
    unittest.main()
