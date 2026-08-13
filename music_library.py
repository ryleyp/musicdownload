#!/usr/bin/env python3
"""One entry point for the resumable workflow; individual scripts remain valid."""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

from common import (
    DEFAULT_DB_PATH,
    PROJECT_DIR,
    load_dotenv,
    running_project_venv,
)
from library_status import print_progress, progress_counts
from common import connect_db


STAGE_SCRIPTS = {
    "sync": "spotify_sync.py",
    "match": "youtube_match.py",
    "review": "import_review.py",
    "download": "download_mp3.py",
    "video": "download_video.py",
    "video-download": "download_video.py",
    "recent": "recent_additions.py",
    "recent-additions": "recent_additions.py",
    "liked-playlist": "liked_songs_playlist.py",
    "liked-songs-playlist": "liked_songs_playlist.py",
    "delete-queue": "music_delete_queue.py",
    "music-delete-queue": "music_delete_queue.py",
    "cleanup-hadestown": "music_album_artist_cleanup.py",
    "album-artist-cleanup": "music_album_artist_cleanup.py",
    "cleanup-library-artists": "music_library_consistency.py",
    "library-consistency": "music_library_consistency.py",
    "artwork": "repair_artwork.py",
    "local-music": "apple_music_duplicates.py",
    "apple-music": "apple_music_duplicates.py",
    "reset-matches": "reset_matches.py",
    "iphone": "iphone_sync.py",
    "iphone-sync": "iphone_sync.py",
    "playlists": "spotify_playlists_to_music.py",
    "spotify-playlists": "spotify_playlists_to_music.py",
    "genres": "music_genres.py",
    "music-genres": "music_genres.py",
    "metadata": "music_metadata.py",
    "spotify-metadata": "music_metadata.py",
}


def parse_args() -> tuple[argparse.Namespace, list[str]]:
    raw_args = sys.argv[1:]
    stage_help = bool(
        raw_args
        and raw_args[0] in STAGE_SCRIPTS
        and any(value in {"-h", "--help"} for value in raw_args[1:])
    )
    if stage_help:
        raw_args = [
            value for index, value in enumerate(raw_args)
            if index == 0 or value not in {"-h", "--help"}
        ]
    parser = argparse.ArgumentParser(
        description="Guide or run each Spotify library archive stage."
    )
    parser.add_argument(
        "command",
        choices=(*STAGE_SCRIPTS, "status", "history", "guide", "doctor"),
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args, forwarded = parser.parse_known_args(raw_args)
    if stage_help:
        forwarded.append("--help")
    return args, forwarded


def doctor() -> int:
    load_dotenv()
    checks: list[tuple[str, bool, str]] = [
        (
            "Project virtual environment",
            running_project_venv(),
            f"activate with: cd {PROJECT_DIR} && source .venv/bin/activate",
        ),
        (
            "Spotify Client ID",
            bool(
                os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
                not in {"", "paste_your_client_id_here"}
            ),
            "set SPOTIFY_CLIENT_ID in .env",
        ),
    ]
    for module in ("requests", "openpyxl", "mutagen", "yt_dlp", "setuptools"):
        checks.append(
            (
                f"Python package {module}",
                importlib.util.find_spec(module) is not None,
                "run: python -m pip install -r requirements.txt",
            )
        )
    for command, remedy in (
        ("yt-dlp", "activate .venv and install requirements"),
        ("ffmpeg", "run: brew install ffmpeg"),
        ("osascript", "included with macOS"),
        ("osacompile", "included with macOS"),
    ):
        checks.append(
            (f"Command {command}", shutil.which(command) is not None, remedy)
        )
    checks.append(
        (
            "Installed music-library command",
            (PROJECT_DIR / ".venv" / "bin" / "music-library").exists(),
            "run: python -m pip install --no-deps --no-build-isolation -e .",
        )
    )
    failed = False
    for label, ok, remedy in checks:
        print(f"{'OK' if ok else 'MISSING':7} {label}")
        if not ok:
            failed = True
            print(f"        {remedy}")
    if shutil.which("osacompile"):
        from apple_music_duplicates import validate_bridge

        try:
            validate_bridge()
            print("OK      Apple Music AppleScript compiles")
        except Exception as exc:
            failed = True
            print(f"ERROR   Apple Music AppleScript: {exc}")
    return 1 if failed else 0


def show_status(db_path: Path) -> None:
    with connect_db(db_path) as connection:
        print_progress(progress_counts(connection))


def guide(db_path: Path) -> int:
    print("Current checkpoint")
    show_status(db_path)
    print(
        "\nRecommended safe sequence:\n"
        "  1. music-library doctor\n"
        "  2. music-library sync --include-albums  # optional saved albums\n"
        "  3. music-library match --auto-approve 95\n"
        "  4. Review data/music_library_review.xlsx\n"
        "  5. music-library review\n"
        "  6. music-library download --min-score 95 --dry-run\n"
        "  7. music-library download --min-score 95\n"
        "  8. music-library local-music\n"
        "  9. music-library iphone  # report-only iPhone sync manifest\n"
        " 10. music-library playlists --sync  # report-only Spotify playlists\n"
        " 11. music-library genres  # report-only local genre audit\n"
        " 12. music-library metadata  # report-only imported-track metadata audit\n"
        " 13. music-library recent --sync --match --auto-approve 95\n"
        " 14. music-library liked-playlist  # report-only ordered playlist audit\n"
        " 15. music-library delete-queue  # report-only deletion queue audit\n"
        " 16. music-library cleanup-hadestown  # report-only artist cleanup\n"
        " 17. music-library cleanup-library-artists  # full-library report\n"
        "\nStandalone MOV: music-library video URL --dry-run\n"
        "\nStep 8 is report-only. This guide never runs --apply."
    )
    return 0


def main() -> int:
    args, forwarded = parse_args()
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]
    if args.command == "doctor":
        return doctor()
    if args.command == "status":
        show_status(args.db)
        return 0
    if args.command == "history":
        from audit_history import main as history_main

        history_args = ["--db", str(args.db), *forwarded]
        return history_main(history_args)
    if args.command == "guide":
        return guide(args.db)
    script = PROJECT_DIR / STAGE_SCRIPTS[args.command]
    command = [sys.executable, str(script)]
    if args.db != DEFAULT_DB_PATH:
        command.extend(["--db", str(args.db)])
    command.extend(forwarded)
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
