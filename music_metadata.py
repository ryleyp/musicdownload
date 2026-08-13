#!/usr/bin/env python3
"""Audit, apply, verify, and restore Spotify metadata on project imports."""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from activity_log import record_event
from apple_music_duplicates import marker_spotify_id, normalize, require_mac, run_bridge
from common import (
    AppError,
    DEFAULT_DB_PATH,
    PROJECT_DIR,
    backup_existing_file,
    connect_db,
    utc_now,
)


DEFAULT_REPORT = PROJECT_DIR / "data" / "music_metadata_report.csv"
FIELDS = (
    "title", "artist", "album_artist", "album", "genre", "year",
    "track_number", "track_count", "disc_number", "compilation", "comment",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Apply Spotify metadata to local Music entries imported by this project. "
            "Default is report-only."
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--list-runs", action="store_true")
    parser.add_argument("--restore-run", metavar="RUN_ID")
    return parser.parse_args(argv)


def as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def scan_music_metadata() -> list[dict[str, Any]]:
    print("Reading local Music metadata...")
    output = run_bridge(["metadata-scan"])
    tracks: list[dict[str, Any]] = []
    for record in output.split("\x1e"):
        if not record:
            continue
        fields = record.split("\x1f")
        if len(fields) != 14:
            continue
        (
            pid, title, artist, album_artist, album, genre, year,
            track_number, track_count, disc_number, compilation, comment,
            enabled, location,
        ) = fields
        tracks.append({
            "persistent_id": pid,
            "title": title,
            "artist": artist,
            "album_artist": album_artist,
            "album": album,
            "genre": genre,
            "year": as_int(year),
            "track_number": as_int(track_number),
            "track_count": as_int(track_count),
            "disc_number": as_int(disc_number),
            "compilation": compilation.casefold() == "true",
            "comment": comment,
            "enabled": enabled.casefold() == "true",
            "location": location,
        })
    print(f"Read {len(tracks):,} local Music file tracks.")
    return tracks


def project_comment(current: str, spotify: Any) -> str:
    prefixes = (
        "SPOTIFY_ARCHIVE_ID=", "Spotify: ", "Spotify album: ", "YouTube: ",
        "ISRC: ", "Explicit: ", "Spotify release date: ",
        "Spotify library added: ", "SPOTIFY_ALBUM_ID=", "YOUTUBE_ID=",
        "ISRC=", "EXPLICIT=", "RELEASE_DATE=", "SPOTIFY_ADDED=",
    )
    preserved = [
        line for line in (current or "").splitlines()
        if line.strip() and not line.startswith(prefixes)
    ]
    managed = [
        f"SPOTIFY_ARCHIVE_ID={spotify['spotify_id']}",
        f"SPOTIFY_ALBUM_ID={spotify['album_id'] or ''}",
        f"YOUTUBE_ID={spotify['youtube_video_id'] or ''}",
        f"ISRC={spotify['isrc'] or ''}",
        f"EXPLICIT={1 if spotify['explicit'] else 0}",
        f"RELEASE_DATE={spotify['release_date'] or ''}",
        f"SPOTIFY_ADDED={spotify['added_at'] or ''}",
    ]
    # Music truncates Comment and Artist text at 255 characters. Keep every
    # reconstructable identifier first, then retain as much custom text as fits.
    return "\n".join([*managed, *preserved])[:255]


def music_text(value: Any) -> str:
    return str(value or "")[:255]


def current_values(track: dict[str, Any]) -> dict[str, Any]:
    return {field: track[field] for field in FIELDS}


def desired_values(track: dict[str, Any], spotify: Any) -> dict[str, Any]:
    return {
        "title": music_text(spotify["title"] or track["title"]),
        "artist": music_text(spotify["artists"] or track["artist"]),
        "album_artist": music_text(
            spotify["album_artist"] or spotify["primary_artist"]
            or track["album_artist"] or track["artist"]
        ),
        "album": music_text(spotify["album"] or track["album"]),
        # Spotify no longer reliably supplies genres. Never erase a Music genre.
        "genre": music_text(spotify["genres"] or track["genre"]),
        "year": as_int(spotify["release_year"]) or track["year"],
        "track_number": as_int(spotify["track_number"]) or track["track_number"],
        "track_count": as_int(spotify["total_tracks"]) or track["track_count"],
        "disc_number": as_int(spotify["disc_number"]) or track["disc_number"],
        # Imported archive copies are deliberately marked as compilations.
        "compilation": True,
        "comment": project_comment(track["comment"], spotify),
    }


def equivalent(field: str, left: Any, right: Any) -> bool:
    if field in {"year", "track_number", "track_count", "disc_number"}:
        return as_int(left) == as_int(right)
    if field == "compilation":
        return bool(left) == bool(right)
    if field == "comment":
        return str(left or "").strip() == str(right or "").strip()
    return normalize(str(left or "")) == normalize(str(right or ""))


def changed_fields(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    return [field for field in FIELDS if not equivalent(field, old[field], new[field])]


def build_rows(
    music_tracks: list[dict[str, Any]], spotify_rows: dict[str, Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for track in music_tracks:
        spotify_id = marker_spotify_id(track["comment"])
        if not spotify_id:
            continue
        spotify = spotify_rows.get(spotify_id)
        old = current_values(track)
        if "(VINYL)" in track["album"].upper():
            new, action, reason = old, "protected_vinyl", "VINYL album is never modified"
        elif not spotify:
            new, action, reason = old, "spotify_row_missing", "Spotify ID is absent from SQLite"
        elif not track["location"] or not Path(track["location"]).is_file():
            new, action, reason = old, "local_file_missing", "Local file is unavailable"
        else:
            new = desired_values(track, spotify)
            differences = changed_fields(old, new)
            action = "would_update" if differences else "already_current"
            reason = ", ".join(differences) if differences else "All supported fields match"
        rows.append({
            "music_persistent_id": track["persistent_id"],
            "spotify_id": spotify_id,
            "title": new["title"],
            "artist": new["artist"],
            "album_artist": new["album_artist"],
            "album": new["album"],
            "genre": new["genre"],
            "year": new["year"],
            "track_number": new["track_number"],
            "track_count": new["track_count"],
            "disc_number": new["disc_number"],
            "compilation": new["compilation"],
            "location": track["location"],
            "action": action,
            "changed_fields": reason,
            "old_values": old,
            "new_values": new,
        })
    return rows


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_existing_file(path)
    fields = [
        "music_persistent_id", "spotify_id", "title", "artist", "album_artist",
        "album", "genre", "year", "track_number", "track_count", "disc_number",
        "compilation", "location", "action", "changed_fields",
    ]
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows({field: row[field] for field in fields} for row in rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def set_metadata(rows: list[dict[str, Any]], batch_size: int) -> dict[str, int]:
    totals = {"applied": 0, "missing": 0, "protected_vinyl": 0}
    for offset in range(0, len(rows), batch_size):
        arguments = ["metadata-set"]
        for row in rows[offset : offset + batch_size]:
            value = row["new_values"]
            arguments.extend([
                row["music_persistent_id"], value["title"], value["artist"],
                value["album_artist"], value["album"], value["genre"],
                str(value["year"]), str(value["track_number"]),
                str(value["track_count"]), str(value["disc_number"]),
                "true" if value["compilation"] else "false", value["comment"],
            ])
        result = run_bridge(arguments).strip().split("\x1f")
        if len(result) != 3 or not all(item.isdigit() for item in result):
            raise AppError(f"Music returned an invalid metadata result: {result!r}")
        totals["applied"] += int(result[0])
        totals["missing"] += int(result[1])
        totals["protected_vinyl"] += int(result[2])
        print(
            f"Metadata apply progress: {min(offset + batch_size, len(rows)):,}/"
            f"{len(rows):,}"
        )
    return totals


def verify_run(connection: Any, run_id: str, expected: str, success: str) -> int:
    current = {row["persistent_id"]: current_values(row) for row in scan_music_metadata()}
    changes = connection.execute(
        "SELECT * FROM music_metadata_changes WHERE run_id = ?", (run_id,)
    ).fetchall()
    verified = 0
    for change in changes:
        wanted = json.loads(change[expected])
        actual = current.get(change["music_persistent_id"])
        fields = changed_fields(actual, wanted) if actual else list(FIELDS)
        ok = not fields
        connection.execute(
            "UPDATE music_metadata_changes SET status = ?, error = ? "
            "WHERE run_id = ? AND music_persistent_id = ?",
            (
                success if ok else "verification_failed",
                None if ok else f"Fields did not verify: {', '.join(fields)}",
                run_id, change["music_persistent_id"],
            ),
        )
        verified += int(ok)
    return verified


def list_runs(connection: Any) -> None:
    rows = connection.execute(
        "SELECT * FROM music_metadata_runs ORDER BY created_at DESC"
    ).fetchall()
    if not rows:
        print("No metadata apply runs.")
    for row in rows:
        print(
            f"{row['run_id']}  {row['status']:<10}  "
            f"{row['applied_count']:,}/{row['planned_count']:,}  {row['created_at']}"
        )


def restore_run(connection: Any, run_id: str, batch_size: int) -> None:
    run = connection.execute(
        "SELECT * FROM music_metadata_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if not run:
        raise AppError(f"Unknown metadata run: {run_id}")
    changes = connection.execute(
        # Restore every saved before-state. This is safe even if an apply was
        # interrupted: unchanged entries simply receive their original values.
        "SELECT * FROM music_metadata_changes WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    rows = [
        {
            "music_persistent_id": row["music_persistent_id"],
            "new_values": json.loads(row["old_values_json"]),
        }
        for row in changes
    ]
    set_metadata(rows, batch_size)
    restored = verify_run(connection, run_id, "old_values_json", "restored")
    connection.execute(
        "UPDATE music_metadata_runs SET status = 'restored', applied_count = ?, "
        "completed_at = ? WHERE run_id = ?",
        (restored, utc_now(), run_id),
    )
    print(f"Metadata restored and verified: {restored:,}/{len(rows):,}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        require_mac()
        if args.batch_size < 1:
            raise AppError("--batch-size must be at least 1.")
        with connect_db(args.db) as connection:
            if args.list_runs:
                list_runs(connection)
                return 0
            if args.restore_run:
                restore_run(connection, args.restore_run, args.batch_size)
                return 0
        music = scan_music_metadata()
        with connect_db(args.db) as connection:
            spotify = {
                row["spotify_id"]: row
                for row in connection.execute("SELECT * FROM tracks").fetchall()
            }
        rows = build_rows(music, spotify)
        write_report(args.report, rows)
        counts: dict[str, int] = defaultdict(int)
        for row in rows:
            counts[row["action"]] += 1
        print(f"Report: {args.report}")
        for action in sorted(counts):
            print(f"  {action}: {counts[action]:,}")
        changes = [row for row in rows if row["action"] == "would_update"]
        if not args.apply:
            print("Report-only: no Music metadata changed. Add --apply after review.")
            return 0
        run_id = uuid.uuid4().hex
        with connect_db(args.db) as connection:
            connection.execute(
                "INSERT INTO music_metadata_runs "
                "(run_id, status, planned_count, created_at) VALUES (?, 'planned', ?, ?)",
                (run_id, len(changes), utc_now()),
            )
            connection.executemany(
                "INSERT INTO music_metadata_changes "
                "(run_id, music_persistent_id, spotify_id, old_values_json, "
                "new_values_json, status) VALUES (?, ?, ?, ?, ?, 'planned')",
                [
                    (
                        run_id, row["music_persistent_id"], row["spotify_id"],
                        json.dumps(row["old_values"], ensure_ascii=False),
                        json.dumps(row["new_values"], ensure_ascii=False),
                    )
                    for row in changes
                ],
            )
        set_metadata(changes, args.batch_size)
        with connect_db(args.db) as connection:
            applied = verify_run(connection, run_id, "new_values_json", "applied")
            status = "applied" if applied == len(changes) else "partial"
            connection.execute(
                "UPDATE music_metadata_runs SET status = ?, applied_count = ?, "
                "completed_at = ? WHERE run_id = ?",
                (status, applied, utc_now(), run_id),
            )
            record_event(
                connection, stage="music_metadata", event="metadata_applied",
                status=status,
                details={"run_id": run_id, "planned": len(changes), "applied": applied},
                log_path=args.db.parent / "activity.jsonl",
            )
        print(f"Metadata restore run: {run_id}")
        print(f"Metadata applied and verified: {applied:,}/{len(changes):,}")
        return 0 if applied == len(changes) else 1
    except (AppError, OSError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
