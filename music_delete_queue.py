#!/usr/bin/env python3
"""Report and explicitly apply a recoverable local Music deletion queue."""
from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from activity_log import record_event
from apple_music_duplicates import (
    marker_spotify_id,
    normalized_location,
    require_mac,
    run_bridge,
    scan_music_library,
)
from common import AppError, DEFAULT_DB_PATH, PROJECT_DIR, backup_existing_file, connect_db, utc_now
from iphone_sync import playlist_persistent_ids


DEFAULT_PLAYLIST = "delete me pls"
DEFAULT_REPORT = PROJECT_DIR / "data" / "music_delete_queue_report.csv"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit a Music playlist used as a deletion queue. Default is report-only. "
            "Applied files move to macOS Trash rather than being permanently erased."
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--playlist", default=DEFAULT_PLAYLIST)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--create-playlist",
        action="store_true",
        help=(
            "Create the empty deletion queue in Music if it is missing. "
            "This never queues or deletes a track."
        ),
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--confirm",
        help="With --apply, must exactly equal the deletion playlist name.",
    )
    parser.add_argument("--list-runs", action="store_true")
    parser.add_argument(
        "--unblock",
        action="append",
        default=[],
        metavar="SPOTIFY_ID",
        help="Allow a previously deleted Spotify ID to be downloaded again.",
    )
    return parser.parse_args(argv)


def build_rows(
    queue_ids: list[str],
    music_tracks: list[dict[str, Any]],
    download_paths: dict[str, str],
) -> list[dict[str, Any]]:
    by_id = {str(track["persistent_id"]): track for track in music_tracks}
    location_counts = Counter(
        normalized_location(track.get("location"))
        for track in music_tracks
        if normalized_location(track.get("location"))
    )
    download_counts = Counter(
        normalized_location(path)
        for path in download_paths.values()
        if normalized_location(path)
    )
    rows: list[dict[str, Any]] = []
    for persistent_id in dict.fromkeys(queue_ids):
        track = by_id.get(persistent_id)
        if not track:
            rows.append({
                "music_persistent_id": persistent_id,
                "spotify_id": "", "title": "", "artist": "", "album": "",
                "music_location": "", "download_path": "",
                "shared_music_file": "", "shared_download_file": "",
                "action": "not_local_file_track",
                "reason": "Playlist item is cloud-only, missing, or not a file track",
            })
            continue
        spotify_id = marker_spotify_id(str(track.get("comment") or "")) or ""
        location = str(track.get("location") or "")
        location_key = normalized_location(location)
        shared = bool(location_key and location_counts[location_key] > 1)
        download_path = download_paths.get(spotify_id, "") if spotify_id else ""
        shared_download = bool(
            download_path
            and download_counts[normalized_location(download_path)] > 1
        )
        if "(VINYL)" in str(track.get("album") or "").upper():
            action, reason = "protected_vinyl", "VINYL albums are never deleted"
        else:
            action = "would_delete"
            reason = (
                "Music entry will be removed; shared file will be preserved"
                if shared else
                "Music entry will be removed and unique local files moved to Trash"
            )
        rows.append({
            "music_persistent_id": persistent_id,
            "spotify_id": spotify_id,
            "title": track.get("title") or "",
            "artist": track.get("artist") or "",
            "album": track.get("album") or "",
            "music_location": location,
            "download_path": download_path,
            "shared_music_file": "yes" if shared else "no",
            "shared_download_file": "yes" if shared_download else "no",
            "action": action,
            "reason": reason,
        })
    return rows


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "music_persistent_id", "spotify_id", "title", "artist", "album",
        "music_location", "download_path", "shared_music_file",
        "shared_download_file", "action", "reason",
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


def list_runs(connection: Any) -> None:
    rows = connection.execute(
        "SELECT * FROM music_delete_runs ORDER BY created_at DESC"
    ).fetchall()
    if not rows:
        print("No Music deletion runs.")
    for row in rows:
        print(
            f"{row['run_id']}  {row['status']:<9}  "
            f"{row['deleted_count']:,}/{row['planned_count']:,}  "
            f"{row['playlist_name']}  {row['created_at']}"
        )


def unique_trash_paths(row: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    music_location = str(row.get("music_location") or "")
    download_path = str(row.get("download_path") or "")
    if music_location and row.get("shared_music_file") != "yes":
        paths.append(music_location)
    if (
        download_path
        and not (
            row.get("shared_music_file") == "yes"
            and normalized_location(download_path) == normalized_location(music_location)
        )
        and row.get("shared_download_file") != "yes"
        and normalized_location(download_path) not in {
        normalized_location(path) for path in paths
        }
    ):
        paths.append(download_path)
    return paths


def apply_rows(
    connection: Any,
    args: argparse.Namespace,
    rows: list[dict[str, Any]],
) -> tuple[str, int, str]:
    eligible = [row for row in rows if row["action"] == "would_delete"]
    run_id = uuid.uuid4().hex
    connection.execute(
        "INSERT INTO music_delete_runs "
        "(run_id, playlist_name, status, planned_count, created_at) "
        "VALUES (?, ?, 'planned', ?, ?)",
        (run_id, args.playlist, len(eligible), utc_now()),
    )
    connection.executemany(
        "INSERT INTO music_delete_items "
        "(run_id, music_persistent_id, spotify_id, title, artist, album, "
        "music_location, download_path, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'planned')",
        [
            (
                run_id, row["music_persistent_id"], row["spotify_id"] or None,
                row["title"], row["artist"], row["album"],
                row["music_location"], row["download_path"],
            )
            for row in eligible
        ],
    )
    connection.commit()
    deleted = 0
    failed_items = 0
    for index, row in enumerate(eligible, start=1):
        errors: list[str] = []
        try:
            result = run_bridge(
                ["delete-library-track", row["music_persistent_id"]]
            ).strip()
            if result != "deleted":
                raise AppError(f"Music returned {result!r}")
        except Exception as exc:
            errors.append(f"Music entry: {exc}")
        if not errors:
            for path in unique_trash_paths(row):
                try:
                    trash_result = run_bridge(["trash-file", path]).strip()
                    if trash_result not in {"trashed", "missing"}:
                        errors.append(f"Trash {path}: {trash_result}")
                except Exception as exc:
                    errors.append(f"Trash {path}: {exc}")
            if row["spotify_id"]:
                connection.execute(
                    """
                    UPDATE tracks
                    SET user_deleted = 1,
                        download_status = 'deleted_by_user',
                        download_path = NULL,
                        apple_music_status = 'deleted_by_user',
                        apple_music_preferred_id = NULL,
                        apple_music_updated_at = ?
                    WHERE spotify_id = ?
                    """,
                    (utc_now(), row["spotify_id"]),
                )
            deleted += 1
        status = "deleted" if not errors else "failed"
        if errors:
            failed_items += 1
        connection.execute(
            "UPDATE music_delete_items SET status = ?, error = ? "
            "WHERE run_id = ? AND music_persistent_id = ?",
            (
                status, "; ".join(errors)[:2000] if errors else None,
                run_id, row["music_persistent_id"],
            ),
        )
        connection.commit()
        print(
            f"[{index:,}/{len(eligible):,}] {row['artist']} - {row['title']}: {status}"
        )
    status = (
        "completed"
        if deleted == len(eligible) and failed_items == 0
        else "partial"
    )
    connection.execute(
        "UPDATE music_delete_runs SET status = ?, deleted_count = ?, "
        "completed_at = ? WHERE run_id = ?",
        (status, deleted, utc_now(), run_id),
    )
    record_event(
        connection,
        stage="music_delete_queue",
        event="delete_queue_applied",
        status=status,
        details={"run_id": run_id, "planned": len(eligible), "deleted": deleted},
        log_path=args.db.parent / "activity.jsonl",
    )
    return run_id, deleted, status


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        with connect_db(args.db) as connection:
            if args.list_runs:
                list_runs(connection)
                return 0
            if args.unblock:
                placeholders = ", ".join("?" for _ in args.unblock)
                result = connection.execute(
                    f"UPDATE tracks SET user_deleted = 0, "
                    "download_status = CASE WHEN download_status = 'deleted_by_user' "
                    "THEN 'not_downloaded' ELSE download_status END "
                    f"WHERE spotify_id IN ({placeholders})",
                    args.unblock,
                )
                print(f"Spotify IDs unblocked: {result.rowcount:,}")
                return 0
        require_mac()
        if not args.playlist.strip():
            raise AppError("--playlist cannot be empty.")
        if args.apply and args.confirm != args.playlist:
            raise AppError(
                f"Deletion not confirmed. Re-run with --confirm {args.playlist!r}."
            )
        if args.create_playlist:
            output = run_bridge(["playlist-add", args.playlist]).strip()
            fields = output.split("\x1f")
            if len(fields) != 5:
                raise AppError(
                    f"Music returned an invalid playlist result: {output!r}"
                )
            print(
                f"Deletion queue ready in Music: {args.playlist} "
                f"({fields[4]} current entries)"
            )
        queue_ids = playlist_persistent_ids(args.playlist)
        music_tracks = scan_music_library()
        with connect_db(args.db) as connection:
            download_paths = {
                row["spotify_id"]: str(row["download_path"] or "")
                for row in connection.execute(
                    "SELECT spotify_id, download_path FROM tracks "
                    "WHERE download_path IS NOT NULL"
                )
            }
        rows = build_rows(queue_ids, music_tracks, download_paths)
        write_report(args.report, rows)
        counts = Counter(row["action"] for row in rows)
        print(f"Report: {args.report}")
        print(f"Queue entries: {len(rows):,}")
        for action in sorted(counts):
            print(f"  {action}: {counts[action]:,}")
        if not args.apply:
            print(
                "Report-only: nothing deleted. Applying removes Music entries "
                "and dependent playlist references, but moves unique files to Trash."
            )
            return 0
        with connect_db(args.db) as connection:
            run_id, deleted, run_status = apply_rows(connection, args, rows)
        print(f"Deletion audit run: {run_id}")
        print(f"Music entries deleted: {deleted:,}/{counts['would_delete']:,}")
        print("Local files were moved to macOS Trash, not permanently erased.")
        return 0 if run_status == "completed" else 1
    except (AppError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
