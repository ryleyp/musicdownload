#!/usr/bin/env python3
"""Create an explainable, review-only audit of Music album grouping variants."""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from apple_music_duplicates import marker_spotify_id, require_mac
from common import AppError, DEFAULT_DB_PATH, PROJECT_DIR, backup_existing_file, connect_db, utc_now
from music_genres import scan_music_genres
from music_library_consistency import (
    GENERIC_ARTISTS,
    attach_durations,
    canonical_values,
    credit_parts,
    normalize,
    spotify_preferences,
)
from music_metadata import scan_music_metadata


DEFAULT_SUMMARY = PROJECT_DIR / "data" / "music_artist_album_consistency_audit.csv"
DEFAULT_TRACKS = PROJECT_DIR / "data" / "music_artist_album_consistency_tracks.csv"
DEFAULT_JSON = PROJECT_DIR / "data" / "music_artist_album_consistency_audit.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a detailed, read-only audit of album title, album artist, "
            "compilation, duplicate-title, and runtime grouping inconsistencies."
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--summary-csv", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--tracks-csv", type=Path, default=DEFAULT_TRACKS)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    return parser.parse_args(argv)


def variant_text(values: Counter[str], blank: str = "[blank]") -> str:
    return " | ".join(
        f"{value or blank} ({count})" for value, count in values.most_common()
    )


def group_evidence(items: list[dict[str, Any]]) -> dict[str, Any]:
    names = Counter(str(item.get("album") or "") for item in items)
    artists = Counter(str(item.get("album_artist") or "") for item in items)
    compilations = Counter(bool(item.get("compilation")) for item in items)
    nonblank_artists = [value for value in artists if value.strip()]
    by_title: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        by_title[normalize(item.get("title"))].append(item)
    duplicate_titles: set[str] = set()
    runtime_close_titles: set[str] = set()
    max_runtime_difference = 0.0
    for title, title_items in by_title.items():
        if len({str(item.get("album_artist") or "") for item in title_items}) < 2:
            continue
        duplicate_titles.add(title)
        for index, first in enumerate(title_items):
            for second in title_items[index + 1:]:
                if first.get("album_artist") == second.get("album_artist"):
                    continue
                first_duration = first.get("duration")
                second_duration = second.get("duration")
                if first_duration is None or second_duration is None:
                    continue
                difference = abs(float(first_duration) - float(second_duration))
                if difference < 5:
                    runtime_close_titles.add(title)
                    max_runtime_difference = max(max_runtime_difference, difference)
    equivalent_artist = any(
        credit_parts(first) == credit_parts(second) and bool(credit_parts(first))
        for index, first in enumerate(nonblank_artists)
        for second in nonblank_artists[index + 1:]
    )
    similar_artist = any(
        SequenceMatcher(None, normalize(first), normalize(second)).ratio() >= 0.72
        for index, first in enumerate(nonblank_artists)
        for second in nonblank_artists[index + 1:]
    )
    generic_split = any(normalize(value) in GENERIC_ARTISTS for value in artists)
    mixed_compilation = len(compilations) > 1
    album_name_variants = len(names) > 1
    if len(runtime_close_titles) >= 2 or equivalent_artist or (
        mixed_compilation and generic_split and len(items) >= 4
    ):
        confidence = "Safe to automate"
        priority = 1
    elif runtime_close_titles or mixed_compilation or album_name_variants or similar_artist:
        confidence = "Manual review"
        priority = 2
    else:
        confidence = "Likely distinct"
        priority = 3
    score = min(
        100,
        len(runtime_close_titles) * 12
        + int(bool(duplicate_titles)) * 12
        + int(equivalent_artist) * 30
        + int(similar_artist) * 10
        + int(mixed_compilation) * 20
        + int(album_name_variants) * 15
        + int(generic_split) * 10,
    )
    issue_types = []
    if runtime_close_titles:
        issue_types.append("runtime-close duplicate titles")
    elif duplicate_titles:
        issue_types.append("duplicate titles")
    if equivalent_artist:
        issue_types.append("equivalent artist separators/case")
    elif similar_artist:
        issue_types.append("similar artist labels")
    if mixed_compilation:
        issue_types.append("mixed compilation flags")
    if album_name_variants:
        issue_types.append("album punctuation/case variants")
    if generic_split:
        issue_types.append("Various Artists/Soundtrack split")
    if confidence == "Likely distinct":
        recommendation = "Keep separate unless listening confirms the same recording"
    elif runtime_close_titles:
        recommendation = "Review duplicate recordings, then normalize grouping metadata"
    elif mixed_compilation:
        recommendation = "Confirm release ownership and compilation flag"
    else:
        recommendation = "Confirm canonical album and album artist"
    return {
        "album_names": names,
        "album_artists": artists,
        "compilations": compilations,
        "duplicate_titles": duplicate_titles,
        "runtime_close_titles": runtime_close_titles,
        "max_runtime_difference": round(max_runtime_difference, 3),
        "equivalent_artist": equivalent_artist,
        "similar_artist": similar_artist,
        "generic_split": generic_split,
        "mixed_compilation": mixed_compilation,
        "album_name_variants": album_name_variants,
        "confidence": confidence,
        "priority": priority,
        "evidence_score": score,
        "issue_types": "; ".join(issue_types),
        "recommendation": recommendation,
    }


def analyze(
    tracks: list[dict[str, Any]],
    spotify: dict[str, tuple[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for track in tracks:
        if str(track.get("album") or "").strip():
            groups[normalize(track["album"])].append(track)
    summary_rows: list[dict[str, Any]] = []
    detail_rows: list[dict[str, Any]] = []
    pending: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
    for key, items in groups.items():
        evidence = group_evidence(items)
        if (
            len(evidence["album_names"]) <= 1
            and len(evidence["album_artists"]) <= 1
            and len(evidence["compilations"]) <= 1
        ):
            continue
        pending.append((key, items, evidence))
    pending.sort(key=lambda value: (
        value[2]["priority"], -value[2]["evidence_score"],
        -len(value[1]), normalize(value[1][0].get("album")),
    ))
    for position, (key, items, evidence) in enumerate(pending, start=1):
        group_id = f"G{position:03d}"
        suggested_album, suggested_artist, suggested_compilation, source = (
            canonical_values(items, spotify.get(key))
        )
        spotify_marked = sum(
            bool(marker_spotify_id(str(item.get("comment") or ""))) for item in items
        )
        formats = Counter(
            Path(str(item.get("location") or "")).suffix.lower().lstrip(".") or "unknown"
            for item in items
        )
        protected = sum(
            "(VINYL)" in str(item.get("album") or "").upper() for item in items
        )
        summary_rows.append({
            "group_id": group_id,
            "review_priority": evidence["priority"],
            "decision": evidence["confidence"],
            "evidence_score": evidence["evidence_score"],
            "album": evidence["album_names"].most_common(1)[0][0],
            "track_count": len(items),
            "enabled_count": sum(bool(item.get("enabled")) for item in items),
            "protected_vinyl_count": protected,
            "album_name_variant_count": len(evidence["album_names"]),
            "album_artist_variant_count": len(evidence["album_artists"]),
            "duplicate_title_count": len(evidence["duplicate_titles"]),
            "runtime_close_duplicate_count": len(evidence["runtime_close_titles"]),
            "largest_close_runtime_difference_seconds": evidence["max_runtime_difference"],
            "mixed_compilation_flags": evidence["mixed_compilation"],
            "spotify_marked_tracks": spotify_marked,
            "file_formats": variant_text(formats),
            "issue_types": evidence["issue_types"],
            "recommended_action": evidence["recommendation"],
            "suggested_album": suggested_album if evidence["confidence"] != "Likely distinct" else "",
            "suggested_album_artist": suggested_artist if evidence["confidence"] != "Likely distinct" else "",
            "suggested_compilation": suggested_compilation if evidence["confidence"] != "Likely distinct" else "",
            "suggestion_source": source if evidence["confidence"] != "Likely distinct" else "",
            "album_name_variants": variant_text(evidence["album_names"]),
            "album_artist_variants": variant_text(evidence["album_artists"]),
            "compilation_variants": " | ".join(
                f"{value}: {count}" for value, count in evidence["compilations"].items()
            ),
            "review_decision": "",
            "review_notes": "",
        })
        close_titles = evidence["runtime_close_titles"]
        duplicate_titles = evidence["duplicate_titles"]
        for item in sorted(items, key=lambda row: (
            normalize(row.get("title")), normalize(row.get("album_artist")),
            str(row.get("persistent_id")),
        )):
            normalized_title = normalize(item.get("title"))
            detail_rows.append({
                "group_id": group_id,
                "decision": evidence["confidence"],
                "music_persistent_id": str(item.get("persistent_id") or ""),
                "title": str(item.get("title") or ""),
                "track_artist": str(item.get("artist") or ""),
                "album": str(item.get("album") or ""),
                "album_artist": str(item.get("album_artist") or ""),
                "compilation": bool(item.get("compilation")),
                "duration_seconds": round(float(item["duration"]), 3) if item.get("duration") is not None else "",
                "duplicate_title_across_variants": normalized_title in duplicate_titles,
                "runtime_close_duplicate": normalized_title in close_titles,
                "enabled": bool(item.get("enabled")),
                "spotify_id": marker_spotify_id(str(item.get("comment") or "")) or "",
                "file_format": Path(str(item.get("location") or "")).suffix.lower().lstrip(".") or "unknown",
                "location": str(item.get("location") or ""),
                "suggested_album": suggested_album if evidence["confidence"] != "Likely distinct" else "",
                "suggested_album_artist": suggested_artist if evidence["confidence"] != "Likely distinct" else "",
                "suggested_compilation": suggested_compilation if evidence["confidence"] != "Likely distinct" else "",
            })
    return summary_rows, detail_rows


def atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0]) if rows else ["group_id"]
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


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_existing_file(path)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def resolved_history(connection: Any) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT r.run_id, r.status, r.group_count, r.planned_count,
               r.applied_count, r.created_at, r.completed_at,
               COUNT(DISTINCT c.old_album) AS albums_touched
        FROM music_group_cleanup_runs r
        LEFT JOIN music_group_cleanup_changes c ON c.run_id = r.run_id
        GROUP BY r.run_id
        ORDER BY r.created_at DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        require_mac()
        metadata = scan_music_metadata()
        attach_durations(metadata, scan_music_genres())
        with connect_db(args.db) as connection:
            spotify = spotify_preferences(connection, metadata)
            history = resolved_history(connection)
        summary, details = analyze(metadata, spotify)
        atomic_csv(args.summary_csv, summary)
        atomic_csv(args.tracks_csv, details)
        payload = {
            "generated_at": utc_now(),
            "tracks_scanned": len(metadata),
            "groups": summary,
            "tracks": details,
            "resolved_history": history,
            "counts": dict(Counter(row["decision"] for row in summary)),
            "rules": [
                {"decision": "Safe to automate", "meaning": "Multiple strong signals identify one release; no apply occurs from this audit."},
                {"decision": "Manual review", "meaning": "One or more warning signals exist, but an automatic merge could combine different recordings."},
                {"decision": "Likely distinct", "meaning": "The shared title probably represents different covers, artists, or releases."},
                {"decision": "Runtime-close", "meaning": "Same normalized title across variants differs by less than five seconds."},
                {"decision": "Suggested values", "meaning": "Reference only; Spotify metadata is preferred when available, then dominant non-generic Music metadata."},
            ],
        }
        atomic_json(args.json, payload)
        counts = Counter(row["decision"] for row in summary)
        print(f"Tracks scanned: {len(metadata):,}")
        print(f"Review groups: {len(summary):,}")
        for decision in ("Safe to automate", "Manual review", "Likely distinct"):
            print(f"  {decision}: {counts[decision]:,}")
        print(f"Summary CSV: {args.summary_csv}")
        print(f"Track details CSV: {args.tracks_csv}")
        print(f"Workbook data: {args.json}")
        print("Read-only audit: no Music metadata or files changed.")
        return 0
    except (AppError, OSError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
