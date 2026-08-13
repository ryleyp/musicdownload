#!/usr/bin/env python3
"""Create a Music playlist in the same newest-first order as Spotify Likes."""
from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from activity_log import record_event
from apple_music_duplicates import marker_spotify_id, require_mac, scan_music_library
from common import AppError, DEFAULT_DB_PATH, PROJECT_DIR, backup_existing_file, connect_db
from spotify_playlists_to_music import playlist_state
from apple_music_duplicates import run_bridge


DEFAULT_NAME = "Spotify - Liked Songs"
DEFAULT_REPORT = PROJECT_DIR / "data" / "spotify_liked_songs_music_report.csv"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create an ordered Music playlist from currently liked Spotify songs, "
            "using only project-imported local tracks."
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--name", default=DEFAULT_NAME)
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Refresh Spotify liked songs and saved albums before reporting.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create an exact newest-first playlist; keeps an old playlist as backup.",
    )
    parser.add_argument("--open-music", action="store_true")
    return parser.parse_args(argv)


def liked_rows(connection: Any) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT spotify_id, title, artists, album, added_at, spotify_url
            FROM tracks
            WHERE is_liked = 1 AND user_deleted = 0
            ORDER BY added_at DESC, rowid DESC
            """
        )
    ]


def map_music_tracks(music_tracks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for track in music_tracks:
        spotify_id = marker_spotify_id(str(track.get("comment") or ""))
        if not spotify_id or not track.get("enabled"):
            continue
        previous = result.get(spotify_id)
        if previous is None or (track.get("location") and not previous.get("location")):
            result[spotify_id] = track
    return result


def build_report(
    spotify_rows: list[dict[str, Any]], music_by_spotify: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    report: list[dict[str, Any]] = []
    desired: list[str] = []
    for position, row in enumerate(spotify_rows, start=1):
        music = music_by_spotify.get(row["spotify_id"])
        if music:
            desired.append(str(music["persistent_id"]))
        report.append({
            "position": position,
            "spotify_id": row["spotify_id"],
            "added_at": row["added_at"] or "",
            "title": row["title"],
            "artist": row["artists"],
            "album": row["album"] or "",
            "music_persistent_id": music["persistent_id"] if music else "",
            "music_location": music.get("location", "") if music else "",
            "status": "ready" if music else "not_downloaded_or_not_in_music",
            "spotify_url": row["spotify_url"] or "",
        })
    return report, desired


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "position", "spotify_id", "added_at", "title", "artist", "album",
        "music_persistent_id", "music_location", "status", "spotify_url",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_existing_file(path)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        with temporary.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def rebuild_playlist(name: str, desired_ids: list[str]) -> dict[str, Any]:
    backup = f"{name} backup {uuid.uuid4().hex[:8]}"[:250]
    output = run_bridge(["playlist-rebuild", name, backup, *desired_ids]).strip()
    fields = output.split("\x1f")
    if len(fields) != 5:
        raise AppError(f"Music returned an invalid liked-playlist result: {output!r}")
    return {
        "requested": int(fields[0]),
        "added": int(fields[1]),
        "missing": int(fields[2]),
        "final": int(fields[3]),
        "backup": fields[4],
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        require_mac()
        if not args.name.strip():
            raise AppError("--name cannot be empty.")
        if args.sync:
            result = subprocess.run(
                [
                    sys.executable, str(PROJECT_DIR / "spotify_sync.py"),
                    "--db", str(args.db), "--include-albums",
                ],
                check=False,
            )
            if result.returncode:
                raise AppError("Spotify refresh failed; Music was not changed.")
        with connect_db(args.db) as connection:
            spotify_rows = liked_rows(connection)
        music_by_spotify = map_music_tracks(scan_music_library())
        report_rows, desired = build_report(spotify_rows, music_by_spotify)
        write_report(args.report, report_rows)
        print(f"Report: {args.report}")
        print(f"Currently liked on Spotify: {len(spotify_rows):,}")
        print(f"Downloaded Music entries ready: {len(desired):,}")
        print(f"Not yet available in Music: {len(spotify_rows) - len(desired):,}")
        rebuilt = False
        result: dict[str, Any] | None = None
        _exists, existing = playlist_state(args.name)
        if existing == desired:
            print(f"Music playlist is already current: {args.name}")
        elif args.apply:
            result = rebuild_playlist(args.name, desired)
            rebuilt = True
            print(f"Music playlist rebuilt: {args.name}")
            print(f"Entries added in Spotify order: {result['added']:,}")
            print(f"Entries skipped by Music: {result['missing']:,}")
            if result["backup"]:
                print(f"Previous playlist retained as: {result['backup']}")
        else:
            print("Report-only: no Music playlist changed. Add --apply when ready.")
        with connect_db(args.db) as connection:
            record_event(
                connection,
                stage="liked_songs_playlist",
                event="playlist_rebuilt" if rebuilt else "playlist_report",
                status="completed",
                details={
                    "playlist": args.name,
                    "liked": len(spotify_rows),
                    "ready": len(desired),
                    "rebuilt": rebuilt,
                    "backup": result["backup"] if result else "",
                },
                log_path=args.db.parent / "activity.jsonl",
            )
        if args.open_music:
            subprocess.run(["open", "-a", "Music"], check=False)
        return 0
    except (AppError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
