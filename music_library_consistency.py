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
        normalize(part) for part in re.split(r"\s*(?:;|/|\||,)\s*", str(value or ""))
        if normalize(part)
    }))


def named_credits(artists: Counter[str]) -> list[str]:
    """Album artists that actually name someone, ignoring blanks and placeholders."""
    return [
        artist for artist in artists
        if artist.strip() and normalize(artist) not in GENERIC_ARTISTS
    ]


def spelling_key(value: Any) -> str:
    """Case- and whitespace-insensitive form that still keeps punctuation.

    Unlike normalize(), "Panic! At The Disco" and "Panic At The Disco" stay
    distinct here; only casing, unicode form, and stray whitespace collapse.
    """
    text = unicodedata.normalize("NFC", str(value or "")).replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip().casefold()


def credit_names(value: Any) -> tuple[list[str], list[str]]:
    """Split a credit into its names and the separators between them, verbatim."""
    parts = re.split(r"(\s*(?:;|/|\||,)\s*)", str(value or "").strip())
    return [part for part in parts[0::2]], [part for part in parts[1::2]]


def primary_credit(value: Any, known_artists: set[str]) -> str:
    """Consolidate a multi-artist album credit to its primary artist.

    The cut happens only at a proper prefix that is a KNOWN standalone
    artist, longest first, so "HUNTR/X/EJAE/..." cuts to "HUNTR/X" and
    never to "HUNTR", and "AC/DC" -- whose only proper prefix "AC" is no
    artist -- stays whole. With no confirmed prefix the credit is returned
    unchanged rather than guessed at.
    """
    raw = str(value or "").strip()
    names, separators = credit_names(raw)
    names = [name for name in names if name]
    if len(names) <= 1:
        return str(value or "")
    known = {normalize(artist) for artist in known_artists if str(artist).strip()}
    rebuilt = names[0]
    prefixes = [rebuilt]
    for name, separator in zip(names[1:], separators):
        rebuilt = rebuilt + separator + name
        prefixes.append(rebuilt)
    for prefix in reversed(prefixes[:-1]):
        if normalize(prefix) in known:
            return prefix
    return str(value or "")


def known_single_artists(
    connection: Any, tracks: list[dict[str, Any]]
) -> set[str]:
    """Names confirmed to be one artist: Spotify primaries plus every
    single-name album artist already in the Music library."""
    known = {
        str(row["primary_artist"])
        for row in connection.execute(
            "SELECT DISTINCT primary_artist FROM tracks "
            "WHERE primary_artist IS NOT NULL AND primary_artist != ''"
        )
    }
    # Every element of a Spotify credit list is one artist entity. Split only
    # on the "; " that spotify_sync joins with -- splitting on commas here
    # would turn "Tyler, The Creator" into a bogus known artist "Tyler".
    for row in connection.execute(
        "SELECT DISTINCT artists FROM tracks WHERE artists IS NOT NULL"
    ):
        for name in str(row["artists"]).split("; "):
            if name.strip():
                known.add(name.strip())
    for track in tracks:
        value = str(track.get("album_artist") or "").strip()
        if not value or normalize(value) in GENERIC_ARTISTS:
            continue
        names, _ = credit_names(value)
        if len([name for name in names if name]) == 1:
            known.add(value)
    return known


def one_credit_set(named: list[str]) -> bool:
    """True when every named album artist is the same credit, however spelled.

    "A/B", "A; B" and "A,B" are one artist written three ways and should be
    unified. "Cody Fry" and "Cody Fry/Ben Rector" are two different artists
    that happen to share an album, and so are two unrelated singles both
    called "Bad Guy"; neither may be merged into the other.
    """
    return len({credit_parts(value) for value in named}) <= 1


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


def spotify_sources(
    connection: Any, tracks: list[dict[str, Any]]
) -> dict[str, tuple[str, str]]:
    """Map Music persistent IDs to the Spotify (album, album artist) they came from."""
    spotify = {
        row["spotify_id"]: row
        for row in connection.execute(
            "SELECT spotify_id, album, album_artist FROM tracks"
        )
    }
    output: dict[str, tuple[str, str]] = {}
    for track in tracks:
        spotify_id = marker_spotify_id(str(track.get("comment") or ""))
        source = spotify.get(spotify_id) if spotify_id else None
        if source and source["album"] and source["album_artist"]:
            output[str(track["persistent_id"])] = (
                str(source["album"]), str(source["album_artist"])
            )
    return output


def split_release(
    items: list[dict[str, Any]],
    spotify_by_pid: dict[str, tuple[str, str]],
) -> tuple[bool, str, tuple[str, str] | None]:
    """Decide whether same-title tracks under different credits are one release.

    A release fragmented by tagging conventions has disjoint track titles
    across its credit sides; different recordings of one song (covers, a
    single next to its album) share the song's title, and unrelated albums
    that merely share a name ("Greatest Hits") share neither titles nor
    credits nor a Spotify source. Only the fragmented release may merge.
    """
    sides: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        value = str(item.get("album_artist") or "")
        key = credit_parts(value)
        if key and normalize(value) not in GENERIC_ARTISTS:
            sides[key].append(item)
    if len(sides) < 2:
        return False, "", None
    title_sets = [
        {normalize(item["title"]) for item in group} for group in sides.values()
    ]
    for index, first in enumerate(title_sets):
        for second in title_sets[index + 1:]:
            if first & second:
                return False, "", None
    sources = {
        spotify_by_pid[str(item["persistent_id"])]
        for group in sides.values() for item in group
        if str(item["persistent_id"]) in spotify_by_pid
    }
    album_words = set(normalize(str(items[0].get("album") or "")).split())

    def within_title(side_key: tuple[str, ...]) -> bool:
        # A side credited to the show or film itself -- "Hamilton" on
        # "Hamilton (Original Broadway Cast Recording)" -- belongs to this
        # album by name, even though it shares no word with the other credit.
        words = {word for part in side_key for word in part.split()}
        return bool(words) and words <= album_words

    # More than one Spotify source is not a veto: a deluxe album often sits
    # next to a single release of one of its tracks. It only rules out using
    # Spotify as the canonical credit; the relation rules below still decide.
    if len(sources) == 1:
        source = next(iter(sources))
        every_side_accounted = all(
            any(str(item["persistent_id"]) in spotify_by_pid for item in group)
            or within_title(side_key)
            for side_key, group in sides.items()
        )
        if every_side_accounted:
            return True, "every side belongs to one Spotify release", source
    largest = max(sides, key=lambda key: len(sides[key]))

    def related(first: tuple[str, ...], second: tuple[str, ...]) -> bool:
        if set(first) <= set(second) or set(second) <= set(first):
            return True
        words_first = {word for part in first for word in part.split()}
        words_second = {word for part in second for word in part.split()}
        return bool(words_first & words_second)

    if all(related(key, largest) for key in sides if key != largest):
        return True, "same-title album split across related credits", None
    if all(within_title(key) for key in sides):
        return True, "same-title album split across related credits", None
    return False, "", None


def high_confidence_group(items: list[dict[str, Any]]) -> tuple[bool, str]:
    artists = Counter(str(item.get("album_artist") or "") for item in items)
    names = Counter(str(item.get("album") or "") for item in items)
    compilations = Counter(bool(item.get("compilation")) for item in items)
    nonblank = [artist for artist in artists if artist.strip()]
    if len(nonblank) <= 1 and len(names) <= 1 and len(compilations) <= 1:
        return False, ""
    # Only exact repeats of one artist get unified. Distinct credits are
    # distinct artists -- a solo release and a collaboration are separate
    # entries by design, and two unrelated singles sharing a title are separate
    # albums. Either way there is nothing here to merge, so stop before every
    # repair signal below. Placeholders like "Various Artists" are not credits
    # and are left to canonical_values to weigh against the album title.
    named = named_credits(artists)
    if not one_credit_set(named):
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
    mixed_compilation = len(compilations) > 1
    blank_mixed = bool(nonblank) and any(not artist.strip() for artist in artists)
    high = bool(
        len(close_titles) >= 2
        or equivalent
        or mixed_compilation
        or blank_mixed
        or len(names) > 1
    )
    reasons = []
    if close_titles:
        reasons.append(f"{len(close_titles)} runtime-close duplicate titles")
    if equivalent:
        reasons.append("equivalent artist credit variants")
    if len({str(value) for value in named}) > 1:
        reasons.append("album artist spelled inconsistently")
    if blank_mixed:
        reasons.append("blank album artist on some tracks")
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
    named = Counter({
        value: count for value, count in artists.items()
        if normalize(value) not in GENERIC_ARTISTS
    })
    named_values = [value for value in named if value.strip()]
    # Spotify supplies the canonical spelling, not a new set of credits. It may
    # rename "A/B" to "A; B", but promoting its release credit onto an album
    # whose tracks all say "Zara Larsson" would file that album under a
    # different artist than the one it belongs to.
    spotify_matches_credits = bool(
        spotify
        and (not named_values or one_credit_set(named_values + [spotify[1]]))
    )
    if spotify_matches_credits and normalize(spotify[1]) not in GENERIC_ARTISTS:
        album_artist = spotify[1]
        source = "Spotify album metadata"
    else:
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
    album = str(album).strip()
    album_artist = str(album_artist).strip()
    combined = f"{album} {album_artist}".casefold()
    compilation = (
        normalize(album_artist) in {"various artists", "soundtrack"}
        or any(word in combined for word in COMPILATION_WORDS)
    )
    return album, album_artist, compilation, source


def merged_canonical(
    items: list[dict[str, Any]],
    spotify_source: tuple[str, str] | None,
) -> tuple[str, str, bool, str]:
    if spotify_source:
        album, album_artist = spotify_source
        source = "Spotify album metadata"
    else:
        album = choose_text(Counter(str(item.get("album") or "") for item in items))
        album_artist = choose_text(
            Counter(str(item.get("album_artist") or "") for item in items)
        )
        source = "majority side of the split release"
    album = str(album).strip()
    album_artist = str(album_artist).strip()
    combined = f"{album} {album_artist}".casefold()
    compilation = (
        normalize(album_artist) in {"various artists", "soundtrack"}
        or any(word in combined for word in COMPILATION_WORDS)
    )
    return album, album_artist, compilation, source


def build_plan(
    tracks: list[dict[str, Any]],
    spotify: dict[str, tuple[str, str]],
    spotify_by_pid: dict[str, tuple[str, str]] | None = None,
    known_artists: set[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    spotify_by_pid = spotify_by_pid or {}
    known_artists = known_artists or set()
    # One performer spelled several ways is still one artist; every row below
    # also snaps the performer to the library-dominant spelling of its credit.
    performer_spellings: dict[str, Counter[str]] = defaultdict(Counter)
    for track in tracks:
        value = str(track.get("artist") or "")
        if value.strip():
            performer_spellings[spelling_key(value)][value] += 1
    dominant_performer = {
        key: choose_text(values).strip()
        for key, values in performer_spellings.items()
        if len(values) > 1
    }

    def performer_target(track: dict[str, Any]) -> str:
        value = str(track.get("artist") or "")
        if not value.strip():
            return value
        return dominant_performer.get(spelling_key(value), value) or value

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for track in tracks:
        if str(track.get("album") or "").strip():
            groups[normalize(track["album"])].append(track)
    rows = []
    group_count = 0
    for key, items in groups.items():
        high, reason = high_confidence_group(items)
        merge_source = None
        if not high:
            merged, reason, merge_source = split_release(items, spotify_by_pid)
            if not merged:
                continue
        group_count += 1
        if high:
            album, artist, compilation, source = canonical_values(items, spotify.get(key))
        else:
            album, artist, compilation, source = merged_canonical(items, merge_source)
        # Albums file under their primary artist; the full collaboration
        # credit stays on each track's own artist field.
        artist = primary_credit(artist, known_artists)
        for item in items:
            protected = "(VINYL)" in str(item.get("album") or "").upper()
            old_performer = str(item.get("artist") or "")
            new_performer = performer_target(item)
            changed = (
                item.get("album") != album
                or item.get("album_artist") != artist
                or bool(item.get("compilation")) != compilation
                or old_performer != new_performer
            )
            rows.append({
                "music_persistent_id": str(item["persistent_id"]),
                "title": str(item.get("title") or ""),
                "track_artist": old_performer,
                "old_album": str(item.get("album") or ""),
                "new_album": album,
                "old_album_artist": str(item.get("album_artist") or ""),
                "new_album_artist": artist,
                "old_compilation": bool(item.get("compilation")),
                "new_compilation": compilation,
                "old_track_artist": old_performer,
                "new_track_artist": new_performer,
                "reason": reason,
                "canonical_source": source,
                "action": "protected_vinyl" if protected else "would_update" if changed else "current",
            })
    # Second pass: one artist spelled several ways across otherwise-consistent
    # albums still splits into several artist entries, so snap every minority
    # spelling to the library-dominant one. Only casing, unicode form, and
    # whitespace may differ -- the credit set itself never changes here.
    handled = {row["music_persistent_id"] for row in rows}
    spellings: dict[str, Counter[str]] = defaultdict(Counter)
    for track in tracks:
        value = str(track.get("album_artist") or "")
        if value.strip() and normalize(value) not in GENERIC_ARTISTS:
            spellings[spelling_key(value)][value] += 1
    dominant = {
        key: choose_text(values).strip()
        for key, values in spellings.items()
        if len(values) > 1
    }
    for track in tracks:
        pid = str(track["persistent_id"])
        if pid in handled:
            continue
        value = str(track.get("album_artist") or "")
        target = dominant.get(spelling_key(value)) if value.strip() else None
        respelled = target if target is not None else value
        consolidated = (
            primary_credit(respelled, known_artists) if value.strip() else respelled
        )
        target = consolidated if consolidated != value else None
        old_performer = str(track.get("artist") or "")
        new_performer = performer_target(track)
        if target is None and old_performer == new_performer:
            continue
        protected = "(VINYL)" in str(track.get("album") or "").upper()
        if target is None:
            reason = "performer spelled inconsistently across the library"
        elif consolidated != respelled:
            reason = "album artist consolidated to the primary artist"
        else:
            reason = "album artist spelled inconsistently across albums"
        rows.append({
            "music_persistent_id": pid,
            "title": str(track.get("title") or ""),
            "track_artist": old_performer,
            "old_album": str(track.get("album") or ""),
            "new_album": str(track.get("album") or ""),
            "old_album_artist": value,
            "new_album_artist": target if target is not None else value,
            "old_compilation": bool(track.get("compilation")),
            "new_compilation": bool(track.get("compilation")),
            "old_track_artist": old_performer,
            "new_track_artist": new_performer,
            "reason": reason,
            "canonical_source": "dominant library spelling",
            "action": "protected_vinyl" if protected else "would_update",
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
            old_performer = str(row.get("old_track_artist") or "")
            new_performer = str(row.get("new_track_artist") or "")
            # An empty fifth field tells the bridge to leave the performer
            # untouched; rows from before this field existed stay inert.
            performer = ""
            if old_performer != new_performer:
                performer = old_performer if restore else new_performer
            arguments.extend([
                row["music_persistent_id"], row[f"{prefix}_album"],
                row[f"{prefix}_album_artist"],
                "true" if row[f"{prefix}_compilation"] else "false",
                performer,
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
        keys = change.keys() if hasattr(change, "keys") else []
        old_performer = str(change["old_track_artist"] or "") if "old_track_artist" in keys else ""
        new_performer = str(change["new_track_artist"] or "") if "new_track_artist" in keys else ""
        ok = bool(actual and actual["album"] == change[f"{prefix}_album"]
                  and actual["album_artist"] == change[f"{prefix}_album_artist"]
                  and bool(actual["compilation"]) == bool(change[f"{prefix}_compilation"]))
        if ok and old_performer != new_performer:
            expected = old_performer if restore else new_performer
            ok = str(actual["artist"] or "") == expected
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
            spotify_by_pid = spotify_sources(connection, metadata)
            known = known_single_artists(connection, metadata)
        rows, groups = build_plan(metadata, spotify, spotify_by_pid, known)
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
                    "old_album_artist,new_album_artist,old_compilation,new_compilation,"
                    "old_track_artist,new_track_artist,status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'planned')",
                    [(run_id,row["music_persistent_id"],row["title"],row["track_artist"],
                      row["old_album"],row["new_album"],row["old_album_artist"],
                      row["new_album_artist"],int(row["old_compilation"]),
                      int(row["new_compilation"]),row["old_track_artist"],
                      row["new_track_artist"]) for row in changes],
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
