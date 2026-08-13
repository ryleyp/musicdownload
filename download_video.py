#!/usr/bin/env python3
"""Download videos with yt-dlp and convert them to QuickTime-compatible MOV."""
from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from common import AppError, PROJECT_DIR


DEFAULT_OUTPUT = PROJECT_DIR / "videos"
DEFAULT_ARCHIVE = PROJECT_DIR / "data" / "video_download_archive.txt"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download one or more video URLs with yt-dlp and convert each one "
            "to an H.264/AAC QuickTime MOV file."
        )
    )
    parser.add_argument("urls", nargs="+", metavar="URL")
    # Accepted so the shared music-library dispatcher can forward a custom
    # database path consistently. Standalone video downloads do not use it.
    parser.add_argument("--db", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Destination folder. Default: {DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--max-height",
        type=int,
        default=1080,
        help="Maximum video height. Use 0 for the best available. Default: 1080.",
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=20,
        help="H.264 quality from 0 (lossless) to 51 (smallest). Default: 20.",
    )
    parser.add_argument(
        "--preset",
        choices=(
            "ultrafast", "superfast", "veryfast", "faster", "fast",
            "medium", "slow", "slower", "veryslow",
        ),
        default="medium",
        help="FFmpeg H.264 encoding speed/size tradeoff. Default: medium.",
    )
    parser.add_argument(
        "--audio-bitrate",
        type=int,
        default=192,
        metavar="KBPS",
        help="AAC audio bitrate in kbps. Default: 192.",
    )
    parser.add_argument(
        "--playlist",
        action="store_true",
        help="Allow a playlist URL to download every playlist item.",
    )
    parser.add_argument(
        "--redownload",
        action="store_true",
        help="Ignore the completion archive and overwrite an existing output.",
    )
    parser.add_argument(
        "--keep-source",
        action="store_true",
        help="Keep the pre-conversion source video in addition to the MOV.",
    )
    parser.add_argument(
        "--po-token-provider",
        action="store_true",
        help="Use yt-dlp's mweb YouTube client with an installed PO-token provider.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the yt-dlp commands without downloading or creating folders.",
    )
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    if args.max_height < 0:
        raise AppError("--max-height cannot be negative. Use 0 for no limit.")
    if not 0 <= args.crf <= 51:
        raise AppError("--crf must be between 0 and 51.")
    if args.audio_bitrate < 32:
        raise AppError("--audio-bitrate must be at least 32 kbps.")
    for url in args.urls:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise AppError(f"A full http(s) video URL is required: {url!r}")


def check_dependencies() -> None:
    if shutil.which("yt-dlp") is None:
        raise AppError(
            "yt-dlp was not found. Activate .venv and run: "
            "python -m pip install -r requirements.txt"
        )
    if shutil.which("ffmpeg") is None:
        raise AppError("ffmpeg was not found. On a Mac, run: brew install ffmpeg")


def format_selector(max_height: int) -> str:
    limit = f"[height<={max_height}]" if max_height else ""
    return f"bv*{limit}+ba/b{limit}/best{limit}"


def yt_dlp_video_command(args: argparse.Namespace, url: str) -> list[str]:
    output_template = str(
        args.output / "%(title).180B [%(id)s].%(ext)s"
    )
    ffmpeg_output_args = (
        f"-c:v libx264 -preset {args.preset} -crf {args.crf} "
        f"-pix_fmt yuv420p -c:a aac -b:a {args.audio_bitrate}k "
        "-movflags +faststart"
    )
    command = [
        "yt-dlp",
        "--ignore-config",
        "--continue",
        "--newline",
        "--embed-metadata",
        "-f",
        format_selector(args.max_height),
        "--recode-video",
        "mov",
        "--postprocessor-args",
        f"VideoConvertor+ffmpeg_o:{ffmpeg_output_args}",
        "--print",
        "after_move:Saved MOV: %(filepath)s",
        "-o",
        output_template,
    ]
    command.append("--yes-playlist" if args.playlist else "--no-playlist")
    if args.redownload:
        command.append("--force-overwrites")
    else:
        command.extend(
            [
                "--no-overwrites",
                "--download-archive",
                str(DEFAULT_ARCHIVE),
            ]
        )
    if args.keep_source:
        command.append("--keep-video")
    if args.po_token_provider:
        command.extend(["--extractor-args", "youtube:player_client=mweb"])
    command.append(url)
    return command


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        validate_args(args)
        check_dependencies()
        if not args.dry_run:
            args.output.mkdir(parents=True, exist_ok=True)
            DEFAULT_ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
        failed = 0
        for index, url in enumerate(args.urls, start=1):
            command = yt_dlp_video_command(args, url)
            print(f"[{index:,}/{len(args.urls):,}] {url}")
            if args.dry_run:
                print(shlex.join(command))
                continue
            result = subprocess.run(command, check=False)
            if result.returncode != 0:
                failed += 1
                print(
                    f"Video failed with yt-dlp exit code {result.returncode}; "
                    "continuing with later URLs.",
                    file=sys.stderr,
                )
        if failed:
            print(
                f"Completed with failures: {failed:,}/{len(args.urls):,}. "
                "Run the same command to resume/retry.",
                file=sys.stderr,
            )
            return 1
        print(f"Video run complete. MOV files: {args.output}")
        return 0
    except (AppError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
