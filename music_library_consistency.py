#!/usr/bin/env python3
"""Safely normalize high-confidence Music album grouping inconsistencies."""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import tempfile
import unicodedata
import uuid
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from activity_log import record_event
from apple_music_duplicates import marker_spotify_id, require_mac, run_bridge
from common import AppError, DEFAULT_DB_PATH, PROJECT_DIR, backup_existing_file, connect_db, utc_now
from music_genres import scan_music_genres
from music_metadata import scan_music_metadata


DEFAULT_REPORT = PROJECT_DIR / "data" / "music_library_consistency_cleanup.csv"
GENERIC_ARTISTS = {"", "various artists", "soundtrack"}
COMPILATION_WORDS = ("cast", "soundtrack", "musical", "motion picture", "original recording")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Normalize only high-confidence album/album-artist splits. Default is "
            "report-only; track performers and audio files are never changed."
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--list-runs", action="store_true")
    parser.add_argument("--restore-run", metavar="RUN_ID")
    return parser.parse_args(argv)


def normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode().casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def credit_parts(value: Any) -> tuple[str, ...]:
    return tuple(sorted({
        normalize(part) for part in re.split(r"\s*(?:;|/|\|)\s*", str(value or ""))
        if normalize(part)
    }))


def choose_text(values: Counter[str]) -> str:
    return sorted(
        values,
        key=lambda value: (
            -values[value],
            "/" in value or "," in value,
            -len(value),
            value.casefold(),
        ),
    )[0]


def attach_durations(metadata: list[dict[str, Any]], durations: list[dict[str, Any]]) -> None:
    by_id = {row["persistent_id"]: row for row in durations}
    for row in metadata:
        extra = by_id.get(row["persistent_id"], {})
        row["duration"] = extra.get("duration")
        row["enabled"] = extra.get("enabled", row.get("enabled"))


def spotify_preferences(connection: Any, tracks: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    preferences: dict[str, list[tuple[str, str]]] = defaultdict(list)
    spotify = {
        row["spotify_id"]: row
        for row in connection.execute(
            "SELECT spotify_id, album, album_artist FROM tracks"
        )
    }
    for track in tracks:
        spotify_id = marker_spotify_id(str(track.get("comment") or ""))
        source = spotify.get(spotify_id) if spotify_id else None
        if source and source["album"] and source["album_artist"]:
            preferences[normalize(track["album"])].append(
                (str(source["album"]), str(source["album_artist"]))
            )
    output = {}
    for key, values in preferences.items():
        output[key] = Counter(values).most_common(1)[0][0]
    return output


def high_confidence_group(items: list[dict[str, Any]]) -> tuple[bool, str]:
    artists = Counter(str(item.get("album_artist") or "") for item in items)
    names = Counter(str(item.get("album") or "") for item in items)
    compilations = Counter(bool(item.get("compilation")) for item in items)
    nonblank = [artist for artist in artists if artist.strip()]
    if len(nonblank) <= 1 and len(names) <= 1 and len(compilations) <= 1:
        return False, ""
    by_title: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        by_title[normalize(item["title"])].append(item)
    close_titles = set()
    for title, title_items in by_title.items():
        for index, first in enumerate(title_items):
            for second in title_items[index + 1:]:
                if first.get("album_artist") == second.get("album_artist"):
                    continue
                if first.get("duration") is None or second.get("duration") is None:
                    continue
                if abs(float(first["duration"]) - float(second["duration"])) < 5:
                    close_titles.add(title)
    equivalent = any(
        credit_parts(first) == credit_parts(second) and bool(credit_parts(first))
        for index, first in enumerate(nonblank)
        for second in nonblank[index + 1:]
    )
    has_generic = any(normalize(value) in GENERIC_ARTISTS for value in artists)
    mixed_compilation = len(compilations) > 1
    high = bool(
        len(close_titles) >= 2
        or equivalent
        or (mixed_compilation and has_generic and len(items) >= 4)
    )
    reasons = []
    if close_titles:
        reasons.append(f"{len(close_titles)} runtime-close duplicate titles")
    if equivalent:
        reasons.append("equivalent artist credit variants")
    if mixed_compilation:
        reasons.append("mixed compilation flags")
    if len(names) > 1:
        reasons.append("album punctuation/case variants")
    return high, "; ".join(reasons)


def canonical_values(
    items: list[dict[str, Any]],
    spotify: tuple[str, str] | None,
) -> tuple[str, str, bool, str]:
    names = Counter(str(item.get("album") or "") for item in items)
    artists = Counter(str(item.get("album_artist") or "") for item in items)
    album = spotify[0] if spotify else choose_text(names)
    if spotify and normalize(spotify[1]) not in GENERIC_ARTISTS:
        album_artist = spotify[1]
        source = "Spotify album metadata"
    else:
        named = Counter({
            value: count for value, count in artists.items()
            if normalize(value) not in GENERIC_ARTISTS
        })
        various_count = sum(
            count for value, count in artists.items()
            if normalize(value) == "various artists"
        )
        cast_like = any(
            word in str(album).casefold()
            for word in ("cast", "musical", "soundtrack")
        )
        if various_count > sum(named.values()) and not cast_like:
            album_artist = "Various Artists"
        else:
            album_artist = choose_text(named or artists)
        source = "dominant non-generic Music album artist"
    combined = f"{album} {album_artist}".casefold()
    compilation = (
        normalize(album_artist) in {"various artists", "soundtrack"}
        or any(word in combined for word in COMPILATION_WORDS)
    )
    return album, album_artist, compilation, source


def build_plan(
    tracks: list[dict[str, Any]],
    spotify: dict[str, tuple[str, str]],
) -> tuple[list[dict[str, Any]], int]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for track in tracks:
        if str(track.get("album") or "").strip():
            groups[normalize(track["album"])].append(track)
    rows = []
    group_count = 0
    for key, items in groups.items():
        high, reason = high_confidence_group(items)
        if not high:
            continue
        group_count += 1
        album, artist, compilation, source = canonical_values(items, spotify.get(key))
        for item in items:
            protected = "(VINYL)" in str(item.get("album") or "").upper()
            changed = (
                item.get("album") != album
                or item.get("album_artist") != artist
                or bool(item.get("compilation")) != compilation
            )
            rows.append({
                "music_persistent_id": str(item["persistent_id"]),
                "title": str(item.get("title") or ""),
                "track_artist": str(item.get("artist") or ""),
                "old_album": str(item.get("album") or ""),
                "new_album": album,
                "old_album_artist": str(item.get("album_artist") or ""),
                "new_album_artist": artist,
                "old_compilation": bool(item.get("compilation")),
                "new_compilation": compilation,
                "reason": reason,
                "canonical_source": source,
                "action": "protected_vinyl" if protected else "would_update" if changed else "current",
            })
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


def set_groups(rows: list[dict[str, Any]], batch_size: int, restore: bool = False) -> None:
    for offset in range(0, len(rows), batch_size):
        arguments = ["album-group-set"]
        for row in rows[offset:offset + batch_size]:
            prefix = "old" if restore else "new"
            arguments.extend([
                row["music_persistent_id"], row[f"{prefix}_album"],
                row[f"{prefix}_album_artist"],
                "true" if row[f"{prefix}_compilation"] else "false",
            ])
        result = run_bridge(arguments).strip().split("\x1f")
        if len(result) != 3 or not all(value.isdigit() for value in result):
            raise AppError(f"Music returned an invalid grouping result: {result!r}")
        print(f"Library cleanup progress: {min(offset + batch_size, len(rows)):,}/{len(rows):,}")


def verify(connection: Any, run_id: str, restore: bool = False) -> int:
    current = {row["persistent_id"]: row for row in scan_music_metadata()}
    changes = connection.execute(
        "SELECT * FROM music_group_cleanup_changes WHERE run_id = ?", (run_id,)
    ).fetchall()
    verified = 0
    prefix = "old" if restore else "new"
    for change in changes:
        actual = current.get(change["music_persistent_id"])
        ok = bool(actual and actual["album"] == change[f"{prefix}_album"]
                  and actual["album_artist"] == change[f"{prefix}_album_artist"]
                  and bool(actual["compilation"]) == bool(change[f"{prefix}_compilation"]))
        connection.execute(
            "UPDATE music_group_cleanup_changes SET status = ?, error = ? "
            "WHERE run_id = ? AND music_persistent_id = ?",
            ("restored" if restore and ok else "applied" if ok else "verification_failed",
             None if ok else "Grouping metadata did not verify", run_id,
             change["music_persistent_id"]),
        )
        verified += int(ok)
    return verified


def list_runs(connection: Any) -> None:
    rows = connection.execute(
        "SELECT * FROM music_group_cleanup_runs ORDER BY created_at DESC"
    ).fetchall()
    if not rows: print("No full-library cleanup runs.")
    for row in rows:
        print(f"{row['run_id']}  {row['status']:<9}  {row['applied_count']:,}/{row['planned_count']:,}  {row['group_count']:,} groups")


def restore_run(connection: Any, run_id: str, batch_size: int) -> int:
    run = connection.execute(
        "SELECT * FROM music_group_cleanup_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if not run: raise AppError(f"Unknown cleanup run: {run_id}")
    rows = [dict(row) for row in connection.execute(
        "SELECT * FROM music_group_cleanup_changes WHERE run_id = ?", (run_id,)
    )]
    set_groups(rows, batch_size, restore=True)
    restored = verify(connection, run_id, restore=True)
    status = "restored" if restored == len(rows) else "partial"
    connection.execute(
        "UPDATE music_group_cleanup_runs SET status=?, applied_count=?, completed_at=? WHERE run_id=?",
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
        metadata = scan_music_metadata()
        attach_durations(metadata, scan_music_genres())
        with connect_db(args.db) as connection:
            spotify = spotify_preferences(connection, metadata)
        rows, groups = build_plan(metadata, spotify)
        write_report(args.report, rows)
        changes = [row for row in rows if row["action"] == "would_update"]
        print(f"Report: {args.report}")
        print(f"High-confidence groups: {groups:,}")
        print(f"Tracks needing normalization: {len(changes):,}")
        if not args.apply:
            print("Report-only: no Music metadata changed. Add --apply after review."); return 0
        with connect_db(args.db) as connection:
            unfinished_probe = connection.execute(
                "SELECT run_id FROM music_group_cleanup_runs "
                "WHERE status IN ('planned','partial') "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if not changes and not unfinished_probe:
            print("Music metadata is already normalized; no audit run created."); return 0
        current_by_id = {str(row["persistent_id"]): row for row in metadata}
        with connect_db(args.db) as connection:
            unfinished = connection.execute(
                "SELECT * FROM music_group_cleanup_runs "
                "WHERE status IN ('planned','partial') ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if unfinished:
                run_id = str(unfinished["run_id"])
                saved_changes = [dict(row) for row in connection.execute(
                    "SELECT * FROM music_group_cleanup_changes WHERE run_id=?",
                    (run_id,),
                )]
                pending = []
                for row in saved_changes:
                    current = current_by_id.get(row["music_persistent_id"])
                    if not current or (
                        current["album"] != row["new_album"]
                        or current["album_artist"] != row["new_album_artist"]
                        or bool(current["compilation"]) != bool(row["new_compilation"])
                    ):
                        pending.append(row)
                print(f"Resuming cleanup run {run_id}: {len(pending):,}/{len(saved_changes):,} remain.")
            else:
                run_id = uuid.uuid4().hex
                saved_changes = changes
                pending = changes
                connection.execute(
                    "INSERT INTO music_group_cleanup_runs "
                    "(run_id,status,group_count,planned_count,created_at) VALUES (?,'planned',?,?,?)",
                    (run_id, groups, len(changes), utc_now()),
                )
                connection.executemany(
                    "INSERT INTO music_group_cleanup_changes "
                    "(run_id,music_persistent_id,title,track_artist,old_album,new_album,"
                    "old_album_artist,new_album_artist,old_compilation,new_compilation,status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,'planned')",
                    [(run_id,row["music_persistent_id"],row["title"],row["track_artist"],
                      row["old_album"],row["new_album"],row["old_album_artist"],
                      row["new_album_artist"],int(row["old_compilation"]),
                      int(row["new_compilation"])) for row in changes],
                )
        set_groups(pending, args.batch_size)
        with connect_db(args.db) as connection:
            applied = verify(connection, run_id)
            status = "applied" if applied == len(saved_changes) else "partial"
            connection.execute(
                "UPDATE music_group_cleanup_runs SET status=?,applied_count=?,completed_at=? WHERE run_id=?",
                (status, applied, utc_now(), run_id),
            )
            record_event(connection, stage="music_library_consistency", event="groups_normalized",
                         status=status, details={"run_id":run_id,"groups":groups,"applied":applied},
                         log_path=args.db.parent / "activity.jsonl")
        print(f"Restore run ID: {run_id}")
        print(f"Applied and verified: {applied:,}/{len(saved_changes):,}")
        return 0 if status == "applied" else 1
    except (AppError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
