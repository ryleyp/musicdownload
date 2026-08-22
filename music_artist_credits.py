#!/usr/bin/env python3
"""Safely merge duplicate Music artist entries caused by credit variants."""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import tempfile
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from activity_log import record_event
from apple_music_duplicates import require_mac, run_bridge
from common import AppError, DEFAULT_DB_PATH, PROJECT_DIR, backup_existing_file, connect_db, utc_now
from music_library_consistency import choose_text, credit_parts, normalize


DEFAULT_REPORT = PROJECT_DIR / "data" / "music_artist_credit_cleanup.csv"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Normalize multi-artist credit strings (Laufey/Los Angeles Philharmonic, "
            "Laufey; dodie, ...) so Music stops splitting one collaboration into "
            "duplicate artist entries. Default is report-only; albums, files, and "
            "solo-artist credits are never changed."
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--list-runs", action="store_true")
    parser.add_argument("--restore-run", metavar="RUN_ID")
    return parser.parse_args(argv)


def split_credits(value: Any) -> list[str]:
    return [
        part.strip() for part in re.split(r"\s*(?:;|/|\|)\s*", str(value or ""))
        if part.strip()
    ]


def scan_artist_credits() -> list[dict[str, Any]]:
    print("Reading local Music artist credits...")
    output = run_bridge(["artist-credit-scan"])
    tracks: list[dict[str, Any]] = []
    for record in output.split("\x1e"):
        if not record:
            continue
        fields = record.split("\x1f")
        if len(fields) != 9:
            continue
        (
            pid, title, artist, album_artist, sort_artist,
            sort_album_artist, album, compilation, comment,
        ) = fields
        tracks.append({
            "persistent_id": pid,
            "title": title,
            "artist": artist,
            "album_artist": album_artist,
            "sort_artist": sort_artist,
            "sort_album_artist": sort_album_artist,
            "album": album,
            "compilation": compilation.casefold() == "true",
            "comment": comment,
        })
    print(f"Read {len(tracks):,} local Music file tracks.")
    return tracks


def spotify_catalog(connection: Any) -> dict[tuple[str, ...], list[tuple[str, str]]]:
    catalog: dict[tuple[str, ...], list[tuple[str, str]]] = defaultdict(list)
    for row in connection.execute("SELECT artists, primary_artist FROM tracks"):
        artists = str(row["artists"] or "")
        key = credit_parts(artists)
        if key:
            catalog[key].append((artists, str(row["primary_artist"] or "")))
    return catalog


def canonical_artist(
    items: list[dict[str, Any]],
    key: tuple[str, ...],
    catalog: dict[tuple[str, ...], list[tuple[str, str]]],
) -> tuple[str, str, str]:
    # A primary artist that itself spans every credit part means the separator
    # belongs to one act's name (AC/DC), not to a collaboration.
    singles: Counter[str] = Counter()
    names: Counter[str] = Counter()
    primaries: Counter[str] = Counter()
    for artists, primary in catalog.get(key, []):
        if primary and credit_parts(primary) == key:
            singles[primary] += 1
        else:
            names[artists] += 1
            if primary and normalize(primary) in key:
                primaries[primary] += 1
    if singles and sum(singles.values()) >= sum(names.values()):
        one = singles.most_common(1)[0][0]
        return one, one, "Spotify single-artist name"
    if names:
        artist = names.most_common(1)[0][0]
        primary = primaries.most_common(1)[0][0] if primaries else split_credits(artist)[0]
        return artist, primary, "Spotify track credit"
    variants = Counter(str(item.get("artist") or "") for item in items)
    dominant = choose_text(variants)
    separators = {tuple(re.findall(r"[;/|]", value)) for value in variants}
    if len(separators) > 1:
        # Mixed separator styles for one part set prove a joined credit, so
        # collapse to the project-standard "A; B" text.
        artist = "; ".join(split_credits(dominant))
        return artist, split_credits(artist)[0], "dominant Music credit variant"
    # Without Spotify or variant evidence this could be a single act whose
    # name contains a separator; keep the text and skip artist promotion.
    return dominant, "", "unconfirmed credit"


def sort_target(values: list[str], key: tuple[str, ...], artist: str) -> str:
    distinct = set(values)
    if len(distinct) > 1:
        # Mixed hidden sort values split one credit into several artist
        # entries. Blank lets Music derive one consistent sort key.
        return ""
    only = next(iter(distinct))
    if only and credit_parts(only) == key and only != artist:
        return ""
    return only


def build_plan(
    tracks: list[dict[str, Any]],
    catalog: dict[tuple[str, ...], list[tuple[str, str]]],
) -> tuple[list[dict[str, Any]], int]:
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for track in tracks:
        key = credit_parts(track.get("artist"))
        if len(key) >= 2:
            groups[key].append(track)
    rows: list[dict[str, Any]] = []
    group_count = 0
    for key, items in sorted(groups.items()):
        artist, primary, source = canonical_artist(items, key, catalog)
        new_sort_artist = sort_target(
            [str(item.get("sort_artist") or "") for item in items], key, artist
        )
        new_sort_album_artist = sort_target(
            [str(item.get("sort_album_artist") or "") for item in items], key, artist
        )
        variants = Counter(str(item.get("artist") or "") for item in items)
        variant_text = " | ".join(
            f"{value} ({count})" for value, count in variants.most_common()
        )
        group_changed = False
        group_rows = []
        for item in items:
            old_album_artist = str(item.get("album_artist") or "")
            if primary and credit_parts(old_album_artist) == key:
                # The album artist repeats the full collaboration credit, so
                # promote the primary performer; other album artists (a solo
                # name, Various Artists, a cast) are grouping decisions left
                # to the album-level cleanup tools.
                new_album_artist = primary
            else:
                new_album_artist = old_album_artist
            protected = "(VINYL)" in str(item.get("album") or "").upper()
            changed = (
                str(item.get("artist") or "") != artist
                or old_album_artist != new_album_artist
                or str(item.get("sort_artist") or "") != new_sort_artist
                or str(item.get("sort_album_artist") or "") != new_sort_album_artist
            )
            group_changed = group_changed or (changed and not protected)
            group_rows.append({
                "music_persistent_id": str(item["persistent_id"]),
                "title": str(item.get("title") or ""),
                "album": str(item.get("album") or ""),
                "old_artist": str(item.get("artist") or ""),
                "new_artist": artist,
                "old_album_artist": old_album_artist,
                "new_album_artist": new_album_artist,
                "old_sort_artist": str(item.get("sort_artist") or ""),
                "new_sort_artist": new_sort_artist,
                "old_sort_album_artist": str(item.get("sort_album_artist") or ""),
                "new_sort_album_artist": new_sort_album_artist,
                "credit_variants": variant_text,
                "canonical_source": source,
                "action": "protected_vinyl" if protected else "would_update" if changed else "current",
            })
        group_count += int(group_changed)
        rows.extend(group_rows)
    return rows, group_count


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0]) if rows else ["music_persistent_id"]
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_existing_file(path)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        with temporary.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader(); writer.writerows(rows)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def set_credits(rows: list[dict[str, Any]], batch_size: int, restore: bool = False) -> None:
    for offset in range(0, len(rows), batch_size):
        arguments = ["artist-credit-set"]
        for row in rows[offset:offset + batch_size]:
            prefix = "old" if restore else "new"
            arguments.extend([
                row["music_persistent_id"], row[f"{prefix}_artist"],
                row[f"{prefix}_album_artist"], row[f"{prefix}_sort_artist"],
                row[f"{prefix}_sort_album_artist"],
            ])
        result = run_bridge(arguments).strip().split("\x1f")
        if len(result) != 3 or not all(value.isdigit() for value in result):
            raise AppError(f"Music returned an invalid artist-credit result: {result!r}")
        print(f"Artist credit progress: {min(offset + batch_size, len(rows)):,}/{len(rows):,}")


def verify(connection: Any, run_id: str, restore: bool = False) -> int:
    current = {row["persistent_id"]: row for row in scan_artist_credits()}
    changes = connection.execute(
        "SELECT * FROM music_artist_credit_changes WHERE run_id = ?", (run_id,)
    ).fetchall()
    verified = 0
    prefix = "old" if restore else "new"
    for change in changes:
        actual = current.get(change["music_persistent_id"])
        ok = bool(
            actual
            and actual["artist"] == change[f"{prefix}_artist"]
            and actual["album_artist"] == change[f"{prefix}_album_artist"]
            and actual["sort_artist"] == change[f"{prefix}_sort_artist"]
            and actual["sort_album_artist"] == change[f"{prefix}_sort_album_artist"]
        )
        connection.execute(
            "UPDATE music_artist_credit_changes SET status = ?, error = ? "
            "WHERE run_id = ? AND music_persistent_id = ?",
            ("restored" if restore and ok else "applied" if ok else "verification_failed",
             None if ok else "Artist credit metadata did not verify", run_id,
             change["music_persistent_id"]),
        )
        verified += int(ok)
    return verified


def list_runs(connection: Any) -> None:
    rows = connection.execute(
        "SELECT * FROM music_artist_credit_runs ORDER BY created_at DESC"
    ).fetchall()
    if not rows: print("No artist-credit cleanup runs.")
    for row in rows:
        print(f"{row['run_id']}  {row['status']:<9}  {row['applied_count']:,}/{row['planned_count']:,}  {row['group_count']:,} credits")


def restore_run(connection: Any, run_id: str, batch_size: int) -> int:
    run = connection.execute(
        "SELECT * FROM music_artist_credit_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if not run: raise AppError(f"Unknown artist-credit cleanup run: {run_id}")
    rows = [dict(row) for row in connection.execute(
        "SELECT * FROM music_artist_credit_changes WHERE run_id = ?", (run_id,)
    )]
    set_credits(rows, batch_size, restore=True)
    restored = verify(connection, run_id, restore=True)
    status = "restored" if restored == len(rows) else "partial"
    connection.execute(
        "UPDATE music_artist_credit_runs SET status=?, applied_count=?, completed_at=? WHERE run_id=?",
        (status, restored, utc_now(), run_id),
    )
    print(f"Restored and verified: {restored:,}/{len(rows):,}")
    return 0 if status == "restored" else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        require_mac()
        if args.batch_size < 1: raise AppError("--batch-size must be at least 1.")
        with connect_db(args.db) as connection:
            if args.list_runs: list_runs(connection); return 0
            if args.restore_run: return restore_run(connection, args.restore_run, args.batch_size)
        tracks = scan_artist_credits()
        with connect_db(args.db) as connection:
            catalog = spotify_catalog(connection)
        rows, groups = build_plan(tracks, catalog)
        write_report(args.report, rows)
        changes = [row for row in rows if row["action"] == "would_update"]
        print(f"Report: {args.report}")
        print(f"Collaboration credits needing normalization: {groups:,}")
        print(f"Tracks needing normalization: {len(changes):,}")
        if not args.apply:
            print("Report-only: no Music metadata changed. Add --apply after review."); return 0
        with connect_db(args.db) as connection:
            unfinished_probe = connection.execute(
                "SELECT run_id FROM music_artist_credit_runs "
                "WHERE status IN ('planned','partial') "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if not changes and not unfinished_probe:
            print("Music artist credits are already normalized; no audit run created."); return 0
        current_by_id = {str(track["persistent_id"]): track for track in tracks}
        with connect_db(args.db) as connection:
            unfinished = connection.execute(
                "SELECT * FROM music_artist_credit_runs "
                "WHERE status IN ('planned','partial') ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if unfinished:
                run_id = str(unfinished["run_id"])
                saved_changes = [dict(row) for row in connection.execute(
                    "SELECT * FROM music_artist_credit_changes WHERE run_id=?",
                    (run_id,),
                )]
                pending = []
                for row in saved_changes:
                    current = current_by_id.get(row["music_persistent_id"])
                    if not current or (
                        current["artist"] != row["new_artist"]
                        or current["album_artist"] != row["new_album_artist"]
                        or current["sort_artist"] != row["new_sort_artist"]
                        or current["sort_album_artist"] != row["new_sort_album_artist"]
                    ):
                        pending.append(row)
                print(f"Resuming cleanup run {run_id}: {len(pending):,}/{len(saved_changes):,} remain.")
            else:
                run_id = uuid.uuid4().hex
                saved_changes = changes
                pending = changes
                connection.execute(
                    "INSERT INTO music_artist_credit_runs "
                    "(run_id,status,group_count,planned_count,created_at) VALUES (?,'planned',?,?,?)",
                    (run_id, groups, len(changes), utc_now()),
                )
                connection.executemany(
                    "INSERT INTO music_artist_credit_changes "
                    "(run_id,music_persistent_id,title,album,old_artist,new_artist,"
                    "old_album_artist,new_album_artist,old_sort_artist,new_sort_artist,"
                    "old_sort_album_artist,new_sort_album_artist,status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'planned')",
                    [(run_id,row["music_persistent_id"],row["title"],row["album"],
                      row["old_artist"],row["new_artist"],row["old_album_artist"],
                      row["new_album_artist"],row["old_sort_artist"],row["new_sort_artist"],
                      row["old_sort_album_artist"],row["new_sort_album_artist"])
                     for row in changes],
                )
        set_credits(pending, args.batch_size)
        with connect_db(args.db) as connection:
            applied = verify(connection, run_id)
            status = "applied" if applied == len(saved_changes) else "partial"
            connection.execute(
                "UPDATE music_artist_credit_runs SET status=?,applied_count=?,completed_at=? WHERE run_id=?",
                (status, applied, utc_now(), run_id),
            )
            record_event(connection, stage="music_artist_credits", event="credits_normalized",
                         status=status, details={"run_id":run_id,"credits":groups,"applied":applied},
                         log_path=args.db.parent / "activity.jsonl")
        print(f"Restore run ID: {run_id}")
        print(f"Applied and verified: {applied:,}/{len(saved_changes):,}")
        return 0 if status == "applied" else 1
    except (AppError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
