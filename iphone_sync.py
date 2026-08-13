#!/usr/bin/env python3
"""Prepare an additive-only Music playlist for Finder iPhone syncing."""
from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from activity_log import record_event
from apple_music_duplicates import require_mac, run_bridge, scan_music_library
from common import (
    AppError,
    DEFAULT_DB_PATH,
    PROJECT_DIR,
    backup_existing_file,
    connect_db,
)


DEFAULT_SOURCE_PLAYLIST = "Spotify Archive Preferred"
DEFAULT_TARGET_PLAYLIST = "iPhone Offline - Spotify Archive"
DEFAULT_REPORT = PROJECT_DIR / "data" / "iphone_sync_manifest.csv"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit local Music files and optionally add them to an additive-only "
            "playlist that can be selected in Finder for iPhone syncing."
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--source-playlist", default=DEFAULT_SOURCE_PLAYLIST)
    parser.add_argument("--target-playlist", default=DEFAULT_TARGET_PLAYLIST)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--all-local",
        action="store_true",
        help=(
            "Include every enabled file track in the Mac Music library. The "
            "default includes only tracks in Spotify Archive Preferred."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Create/update the target playlist by adding missing tracks. "
            "Existing playlist members are never removed."
        ),
    )
    parser.add_argument(
        "--open-finder",
        action="store_true",
        help="Open Finder after the report/apply step. Does not click Sync.",
    )
    return parser.parse_args(argv)


def playlist_persistent_ids(name: str) -> list[str]:
    output = run_bridge(["playlist-ids", name]).strip()
    return [value for value in output.split("\x1f") if value]


def add_to_sync_playlist(name: str, persistent_ids: list[str]) -> dict[str, int]:
    output = run_bridge(["playlist-add", name, *persistent_ids]).strip()
    fields = output.split("\x1f")
    if len(fields) != 5:
        raise AppError(f"Music returned an invalid playlist result: {output!r}")
    labels = ("requested", "previous", "added", "missing", "final")
    try:
        return {label: int(value) for label, value in zip(labels, fields, strict=True)}
    except ValueError as exc:
        raise AppError(f"Music returned invalid playlist counts: {output!r}") from exc


def track_readiness(track: dict[str, Any]) -> tuple[bool, str, int]:
    if not track.get("enabled"):
        return False, "disabled_in_music", 0
    location = str(track.get("location") or "")
    if not location:
        return False, "no_local_file_location", 0
    path = Path(location)
    if not path.is_file():
        return False, "local_file_missing", 0
    try:
        size = path.stat().st_size
    except OSError:
        return False, "local_file_unreadable", 0
    return True, "ready", size


def build_manifest(
    music_tracks: list[dict[str, Any]],
    source_ids: list[str] | None,
) -> tuple[list[dict[str, Any]], list[str], int]:
    by_id = {str(track["persistent_id"]): track for track in music_tracks}
    selected_ids = list(by_id) if source_ids is None else list(dict.fromkeys(source_ids))
    rows: list[dict[str, Any]] = []
    ready_ids: list[str] = []
    total_bytes = 0
    for persistent_id in selected_ids:
        track = by_id.get(persistent_id)
        if track is None:
            rows.append(
                {
                    "persistent_id": persistent_id,
                    "title": "",
                    "artist": "",
                    "album": "",
                    "duration_seconds": "",
                    "location": "",
                    "size_bytes": 0,
                    "size_mb": 0,
                    "ready": "no",
                    "reason": "playlist_entry_not_local_or_missing",
                }
            )
            continue
        ready, reason, size = track_readiness(track)
        if ready:
            ready_ids.append(persistent_id)
            total_bytes += size
        rows.append(
            {
                "persistent_id": persistent_id,
                "title": track.get("title") or "",
                "artist": track.get("artist") or "",
                "album": track.get("album") or "",
                "duration_seconds": track.get("duration") or "",
                "location": track.get("location") or "",
                "size_bytes": size,
                "size_mb": round(size / 1_000_000, 3),
                "ready": "yes" if ready else "no",
                "reason": reason,
            }
        )
    return rows, ready_ids, total_bytes


def write_manifest(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_existing_file(path)
    fieldnames = [
        "persistent_id",
        "title",
        "artist",
        "album",
        "duration_seconds",
        "location",
        "size_bytes",
        "size_mb",
        "ready",
        "reason",
    ]
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        require_mac()
        if not args.target_playlist.strip():
            raise AppError("--target-playlist cannot be empty.")
        if not args.all_local and not args.source_playlist.strip():
            raise AppError("--source-playlist cannot be empty.")
        if (
            not args.all_local
            and args.source_playlist.casefold() == args.target_playlist.casefold()
        ):
            raise AppError("The source and target playlists must have different names.")

        music_tracks = scan_music_library()
        source_ids = (
            None
            if args.all_local
            else playlist_persistent_ids(args.source_playlist)
        )
        rows, ready_ids, total_bytes = build_manifest(music_tracks, source_ids)
        write_manifest(args.report, rows)
        not_ready = len(rows) - len(ready_ids)
        print(f"Manifest: {args.report}")
        print(f"Selected: {len(rows):,}")
        print(f"Ready local files: {len(ready_ids):,}")
        print(f"Not ready: {not_ready:,}")
        print(f"Estimated audio size: {total_bytes / 1_000_000_000:.2f} GB")

        result: dict[str, int] | None = None
        if args.apply:
            result = add_to_sync_playlist(args.target_playlist, ready_ids)
            print(f"Target playlist: {args.target_playlist}")
            print(f"Already present: {result['previous']:,}")
            print(f"Added this run: {result['added']:,}")
            print(f"Final playlist entries: {result['final']:,}")
            if result["missing"]:
                print(f"Music IDs not found while applying: {result['missing']:,}")
        else:
            print("Report-only: no Music playlist was changed. Add --apply when ready.")

        with connect_db(args.db) as connection:
            record_event(
                connection,
                stage="iphone_sync",
                event="playlist_prepared" if args.apply else "manifest_created",
                status="completed",
                details={
                    "scope": "all_local" if args.all_local else "source_playlist",
                    "source_playlist": None if args.all_local else args.source_playlist,
                    "target_playlist": args.target_playlist,
                    "selected": len(rows),
                    "ready": len(ready_ids),
                    "not_ready": not_ready,
                    "bytes": total_bytes,
                    "apply_result": result,
                },
                log_path=args.db.parent / "activity.jsonl",
            )

        if args.open_finder:
            subprocess.run(["open", "-a", "Finder"], check=False)
            print(
                "Finder opened. Connect and unlock the iPhone, select it in "
                "Finder, then choose Music > selected playlists and select "
                f"'{args.target_playlist}'. Review the summary before Sync."
            )
        return 0
    except (AppError, OSError, subprocess.SubprocessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
