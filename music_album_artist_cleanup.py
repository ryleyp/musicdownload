#!/usr/bin/env python3
"""Normalize Hadestown album artists without changing track performers or files."""
from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from activity_log import record_event
from apple_music_duplicates import require_mac, run_bridge
from common import AppError, DEFAULT_DB_PATH, PROJECT_DIR, backup_existing_file, connect_db, utc_now
from music_metadata import scan_music_metadata


DEFAULT_REPORT = PROJECT_DIR / "data" / "hadestown_artist_cleanup.csv"
HADESTOWN_RULES = {
    "Hadestown (Original Broadway Cast Recording)": (
        "Original Broadway Cast of Hadestown", True
    ),
    "Hadestown: Live From London": (
        "Hadestown Original West End Cast", True
    ),
    "Hadestown: The Myth, The Musical (Demo)": (
        "Hadestown Original Cast", True
    ),
    "Hadestown: The Myth. The Musical. (Original Cast Recording) [Live]": (
        "Hadestown Original Cast", True
    ),
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Normalize Hadestown cast-album artists and compilation flags. "
            "Default is report-only and track performer credits are preserved."
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--list-runs", action="store_true")
    parser.add_argument("--restore-run", metavar="RUN_ID")
    return parser.parse_args(argv)


def build_rows(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for track in tracks:
        rule = HADESTOWN_RULES.get(str(track.get("album") or ""))
        if not rule:
            continue
        new_artist, new_compilation = rule
        protected = "(VINYL)" in str(track.get("album") or "").upper()
        changed = (
            str(track.get("album_artist") or "") != new_artist
            or bool(track.get("compilation")) != new_compilation
        )
        rows.append({
            "music_persistent_id": str(track["persistent_id"]),
            "title": str(track.get("title") or ""),
            "track_artist": str(track.get("artist") or ""),
            "album": str(track.get("album") or ""),
            "old_album_artist": str(track.get("album_artist") or ""),
            "new_album_artist": new_artist,
            "old_compilation": bool(track.get("compilation")),
            "new_compilation": new_compilation,
            "action": "protected_vinyl" if protected else "would_update" if changed else "current",
        })
    return rows


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "music_persistent_id", "title", "track_artist", "album",
        "old_album_artist", "new_album_artist", "old_compilation",
        "new_compilation", "action",
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


def set_album_artists(rows: list[dict[str, Any]], batch_size: int, *, restore: bool = False) -> dict[str, int]:
    totals = {"applied": 0, "missing": 0, "protected_vinyl": 0}
    for offset in range(0, len(rows), batch_size):
        arguments = ["album-artist-set"]
        for row in rows[offset : offset + batch_size]:
            artist_key = "old_album_artist" if restore else "new_album_artist"
            compilation_key = "old_compilation" if restore else "new_compilation"
            arguments.extend([
                row["music_persistent_id"], row[artist_key],
                "true" if row[compilation_key] else "false",
            ])
        result = run_bridge(arguments).strip().split("\x1f")
        if len(result) != 3 or not all(value.isdigit() for value in result):
            raise AppError(f"Music returned an invalid album-artist result: {result!r}")
        for key, value in zip(totals, result):
            totals[key] += int(value)
        print(f"Artist cleanup progress: {min(offset + batch_size, len(rows)):,}/{len(rows):,}")
    return totals


def verify(connection: Any, run_id: str, *, restore: bool = False) -> int:
    current = {row["persistent_id"]: row for row in scan_music_metadata()}
    changes = connection.execute(
        "SELECT * FROM music_album_artist_changes WHERE run_id = ?", (run_id,)
    ).fetchall()
    verified = 0
    for change in changes:
        artist_key = "old_album_artist" if restore else "new_album_artist"
        compilation_key = "old_compilation" if restore else "new_compilation"
        actual = current.get(change["music_persistent_id"])
        ok = bool(
            actual
            and actual["album_artist"] == change[artist_key]
            and bool(actual["compilation"]) == bool(change[compilation_key])
        )
        connection.execute(
            "UPDATE music_album_artist_changes SET status = ?, error = ? "
            "WHERE run_id = ? AND music_persistent_id = ?",
            (
                "restored" if restore and ok else "applied" if ok else "verification_failed",
                None if ok else "Album artist or compilation flag did not verify",
                run_id, change["music_persistent_id"],
            ),
        )
        verified += int(ok)
    return verified


def list_runs(connection: Any) -> None:
    rows = connection.execute(
        "SELECT * FROM music_album_artist_runs ORDER BY created_at DESC"
    ).fetchall()
    if not rows:
        print("No album-artist cleanup runs.")
    for row in rows:
        print(
            f"{row['run_id']}  {row['status']:<9}  "
            f"{row['applied_count']:,}/{row['planned_count']:,}  {row['created_at']}"
        )


def restore_run(connection: Any, run_id: str, batch_size: int) -> int:
    run = connection.execute(
        "SELECT * FROM music_album_artist_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if not run:
        raise AppError(f"Unknown album-artist cleanup run: {run_id}")
    rows = [dict(row) for row in connection.execute(
        "SELECT * FROM music_album_artist_changes WHERE run_id = ?", (run_id,)
    )]
    set_album_artists(rows, batch_size, restore=True)
    restored = verify(connection, run_id, restore=True)
    status = "restored" if restored == len(rows) else "partial"
    connection.execute(
        "UPDATE music_album_artist_runs SET status = ?, applied_count = ?, completed_at = ? WHERE run_id = ?",
        (status, restored, utc_now(), run_id),
    )
    print(f"Restored and verified: {restored:,}/{len(rows):,}")
    return 0 if status == "restored" else 1


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
                return restore_run(connection, args.restore_run, args.batch_size)
        music_tracks = scan_music_metadata()
        rows = build_rows(music_tracks)
        write_report(args.report, rows)
        changes = [row for row in rows if row["action"] == "would_update"]
        print(f"Report: {args.report}")
        print(f"Hadestown cast-album tracks reviewed: {len(rows):,}")
        print(f"Tracks needing normalization: {len(changes):,}")
        if not args.apply:
            print("Report-only: no Music metadata changed. Add --apply after review.")
            return 0
        with connect_db(args.db) as connection:
            unfinished_probe = connection.execute(
                "SELECT run_id FROM music_album_artist_runs "
                "WHERE scope = 'hadestown' AND status IN ('planned', 'partial') "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if not changes and not unfinished_probe:
            print("Music metadata is already normalized; no audit run created.")
            return 0
        current_by_id = {
            str(track["persistent_id"]): track for track in music_tracks
        }
        with connect_db(args.db) as connection:
            unfinished = connection.execute(
                "SELECT * FROM music_album_artist_runs "
                "WHERE scope = 'hadestown' AND status IN ('planned', 'partial') "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if unfinished:
                run_id = str(unfinished["run_id"])
                saved_changes = [dict(row) for row in connection.execute(
                    "SELECT * FROM music_album_artist_changes WHERE run_id = ?",
                    (run_id,),
                )]
                pending = []
                for row in saved_changes:
                    current = current_by_id.get(row["music_persistent_id"])
                    if not current or (
                        current["album_artist"] != row["new_album_artist"]
                        or bool(current["compilation"]) != bool(row["new_compilation"])
                    ):
                        pending.append(row)
                print(
                    f"Resuming cleanup run {run_id}: "
                    f"{len(pending):,}/{len(saved_changes):,} entries remain."
                )
            else:
                run_id = uuid.uuid4().hex
                saved_changes = changes
                pending = changes
                connection.execute(
                    "INSERT INTO music_album_artist_runs "
                    "(run_id, scope, status, planned_count, created_at) "
                    "VALUES (?, 'hadestown', 'planned', ?, ?)",
                    (run_id, len(changes), utc_now()),
                )
                connection.executemany(
                    "INSERT INTO music_album_artist_changes "
                    "(run_id, music_persistent_id, title, album, old_album_artist, new_album_artist, "
                    "old_compilation, new_compilation, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'planned')",
                    [(
                        run_id, row["music_persistent_id"], row["title"], row["album"],
                        row["old_album_artist"], row["new_album_artist"],
                        int(row["old_compilation"]), int(row["new_compilation"]),
                    ) for row in changes],
                )
        set_album_artists(pending, args.batch_size)
        with connect_db(args.db) as connection:
            applied = verify(connection, run_id)
            status = "applied" if applied == len(saved_changes) else "partial"
            connection.execute(
                "UPDATE music_album_artist_runs SET status = ?, applied_count = ?, completed_at = ? WHERE run_id = ?",
                (status, applied, utc_now(), run_id),
            )
            record_event(
                connection, stage="music_album_artist_cleanup", event="hadestown_normalized",
                status=status, details={"run_id": run_id, "applied": applied},
                log_path=args.db.parent / "activity.jsonl",
            )
        print(f"Restore run ID: {run_id}")
        print(f"Applied and verified: {applied:,}/{len(saved_changes):,}")
        return 0 if status == "applied" else 1
    except (AppError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
