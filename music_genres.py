#!/usr/bin/env python3
"""Audit and safely apply genres to local Music file tracks."""
from __future__ import annotations

import argparse
import csv
import hashlib
import os
import sys
import tempfile
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from activity_log import record_event
from apple_music_duplicates import normalize, require_mac, run_bridge
from common import (
    AppError,
    DEFAULT_DB_PATH,
    PROJECT_DIR,
    backup_existing_file,
    connect_db,
    dependency_help,
    utc_now,
)


DEFAULT_REPORT = PROJECT_DIR / "data" / "music_genre_report.csv"
APPLE_SEARCH_URL = "https://itunes.apple.com/search"
INVALID_GENRES = {"", "unknown", "other", "none", "not available"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fill missing genres on local Music file tracks using existing "
            "album consensus, embedded tags, and optional exact Apple catalog matches."
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--lookup",
        action="store_true",
        help="Look up uncached album genres in Apple's catalog before reporting.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Maximum uncached album lookups this run. Default: 100.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Look up every uncached album instead of one resumable batch.",
    )
    parser.add_argument("--country", default="US")
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=3.2,
        help="Delay between Apple Search API calls. Default: 3.2 seconds.",
    )
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="Retry catalog cache entries that previously failed.",
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Refresh existing catalog cache results for selected albums.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply high-confidence genre candidates. Default is report-only.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="With --apply, replace a different existing genre. Default fills blanks only.",
    )
    parser.add_argument("--list-runs", action="store_true")
    parser.add_argument("--restore-run", metavar="RUN_ID")
    return parser.parse_args(argv)


def valid_genre(value: str | None) -> str:
    genre = " ".join((value or "").split()).strip(";,")
    return "" if genre.casefold() in INVALID_GENRES else genre


def album_key(track: dict[str, Any]) -> str:
    artist = track.get("album_artist") or track.get("artist") or ""
    album = track.get("album") or ""
    payload = f"{normalize(str(artist))}\x00{normalize(str(album))}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def scan_music_genres() -> list[dict[str, Any]]:
    print("Reading local Music genres...")
    output = run_bridge(["genre-scan"])
    tracks: list[dict[str, Any]] = []
    for record in output.split("\x1e"):
        if not record:
            continue
        fields = record.split("\x1f")
        if len(fields) != 10:
            continue
        (
            pid, title, artist, album_artist, album, duration, enabled,
            location, comment, genre,
        ) = fields
        try:
            duration_value: float | None = float(duration)
        except (TypeError, ValueError):
            duration_value = None
        tracks.append({
            "persistent_id": pid,
            "title": title,
            "artist": artist,
            "album_artist": album_artist,
            "album": album,
            "duration": duration_value,
            "enabled": enabled.casefold() == "true",
            "location": location,
            "comment": comment,
            "genre": genre,
        })
    print(f"Read {len(tracks):,} local file tracks.")
    return tracks


def embedded_genre(path_text: str) -> str:
    if not path_text:
        return ""
    path = Path(path_text)
    if not path.is_file():
        return ""
    try:
        from mutagen import File
    except ImportError as exc:
        raise AppError(dependency_help("mutagen")) from exc
    try:
        audio = File(path, easy=True)
        if audio is None or not audio.tags:
            return ""
        values = audio.tags.get("genre") or []
        return valid_genre(str(values[0])) if values else ""
    except Exception:
        return ""


def group_consensus(tracks: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    evidence: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {"existing": set(), "embedded": set()}
    )
    for track in tracks:
        key = album_key(track)
        current = valid_genre(str(track.get("genre") or ""))
        if current:
            evidence[key]["existing"].add(current)
        embedded = embedded_genre(str(track.get("location") or ""))
        if embedded:
            evidence[key]["embedded"].add(embedded)
    output: dict[str, tuple[str, str]] = {}
    for key, sources in evidence.items():
        if len(sources["existing"]) == 1:
            output[key] = (next(iter(sources["existing"])), "music_album_consensus")
        elif not sources["existing"] and len(sources["embedded"]) == 1:
            output[key] = (next(iter(sources["embedded"])), "embedded_album_consensus")
    return output


def catalog_result(
    session: Any,
    album_artist: str,
    album: str,
    country: str,
) -> tuple[str, str, str, str | None]:
    try:
        response = session.get(
            APPLE_SEARCH_URL,
            params={
                "term": f"{album_artist} {album}",
                "country": country.upper(),
                "media": "music",
                "entity": "album",
                "limit": 15,
            },
            timeout=30,
            headers={"User-Agent": "spotify-youtube-library-mac/2.0"},
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return "", "apple_catalog", "error", str(exc)[:1000]
    exact = [
        item for item in payload.get("results") or []
        if normalize(item.get("collectionName") or "") == normalize(album)
        and normalize(item.get("artistName") or "") == normalize(album_artist)
        and valid_genre(item.get("primaryGenreName"))
    ]
    genres = {valid_genre(item.get("primaryGenreName")) for item in exact}
    if len(genres) == 1:
        genre = next(iter(genres))
        url = next(
            (item.get("collectionViewUrl") for item in exact if item.get("collectionViewUrl")),
            "",
        )
        return genre, "apple_catalog", "matched", str(url or "")
    if len(genres) > 1:
        return "", "apple_catalog", "ambiguous", "multiple exact genres"
    return "", "apple_catalog", "no_match", None


def load_cache(connection: Any) -> dict[str, dict[str, Any]]:
    return {
        str(row["cache_key"]): dict(row)
        for row in connection.execute("SELECT * FROM music_genre_cache")
    }


def perform_lookups(
    connection: Any,
    tracks: list[dict[str, Any]],
    consensus: dict[str, tuple[str, str]],
    args: argparse.Namespace,
) -> None:
    try:
        import requests
    except ImportError as exc:
        raise AppError(dependency_help("requests")) from exc
    if args.batch_size <= 0:
        raise AppError("--batch-size must be positive.")
    if args.delay_seconds < 3:
        raise AppError("--delay-seconds must be at least 3 seconds for Apple's API.")
    cached = load_cache(connection)
    groups: dict[str, dict[str, str]] = {}
    for track in tracks:
        key = album_key(track)
        album_artist = str(track.get("album_artist") or track.get("artist") or "")
        album = str(track.get("album") or "")
        if not album_artist or not album or key in consensus:
            continue
        row = cached.get(key)
        if row and not args.refresh_cache:
            if row["status"] != "error" or not args.retry_errors:
                continue
        groups[key] = {"album_artist": album_artist, "album": album}
    selected = list(groups.items())
    if not args.all:
        selected = selected[: args.batch_size]
    if not selected:
        print("Genre catalog cache is current for this batch.")
        return
    session = requests.Session()
    for index, (key, group) in enumerate(selected, start=1):
        print(
            f"[{index:,}/{len(selected):,}] Apple genre: "
            f"{group['album_artist']} - {group['album']}"
        )
        genre, source, status, extra = catalog_result(
            session, group["album_artist"], group["album"], args.country
        )
        catalog_url = extra if status == "matched" else ""
        error = extra if status in {"error", "ambiguous"} else ""
        connection.execute(
            """
            INSERT INTO music_genre_cache (
                cache_key, album_artist, album, genre, source, status,
                catalog_url, error, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                album_artist=excluded.album_artist, album=excluded.album,
                genre=excluded.genre, source=excluded.source,
                status=excluded.status, catalog_url=excluded.catalog_url,
                error=excluded.error, updated_at=excluded.updated_at
            """,
            (
                key, group["album_artist"], group["album"], genre, source,
                status, catalog_url, error, utc_now(),
            ),
        )
        connection.commit()
        if index < len(selected):
            time.sleep(args.delay_seconds)
    print(f"Catalog lookups cached: {len(selected):,}")


def candidate_rows(
    tracks: list[dict[str, Any]],
    consensus: dict[str, tuple[str, str]],
    cache: dict[str, dict[str, Any]],
    overwrite: bool,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for track in tracks:
        current = valid_genre(str(track.get("genre") or ""))
        key = album_key(track)
        candidate, source, catalog_url = "", "", ""
        if key in consensus:
            candidate, source = consensus[key]
        cached = cache.get(key)
        if not candidate and cached and cached["status"] == "matched":
            candidate = valid_genre(cached.get("genre"))
            source = str(cached.get("source") or "apple_catalog")
            catalog_url = str(cached.get("catalog_url") or "")
        location = str(track.get("location") or "")
        exists = bool(location and Path(location).is_file())
        if "(VINYL)" in str(track.get("album") or "").upper():
            action, reason = "protected_vinyl", "VINYL album is never modified"
        elif not track.get("enabled"):
            action, reason = "skip_disabled", "Music entry is disabled"
        elif not exists:
            action, reason = "local_file_missing", "Local file is unavailable"
        elif current and not overwrite:
            action, reason = "keep_existing", "Existing genre preserved"
        elif not candidate:
            action, reason = "no_genre_found", "No unambiguous genre evidence"
        elif normalize(current) == normalize(candidate):
            action, reason = "already_matches", "Genre already matches"
        elif current:
            action, reason = "would_overwrite", f"{source} candidate"
        else:
            action, reason = "would_fill_missing", f"{source} candidate"
        rows.append({
            "music_persistent_id": track["persistent_id"],
            "title": track["title"],
            "artist": track["artist"],
            "album_artist": track["album_artist"],
            "album": track["album"],
            "duration_seconds": track["duration"] or "",
            "location": location,
            "current_genre": current,
            "proposed_genre": candidate,
            "source": source,
            "catalog_url": catalog_url,
            "action": action,
            "reason": reason,
        })
    return rows


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_existing_file(path)
    fields = list(rows[0]) if rows else [
        "music_persistent_id", "title", "artist", "album_artist", "album",
        "duration_seconds", "location", "current_genre", "proposed_genre",
        "source", "catalog_url", "action", "reason",
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
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def set_genres(
    changes: list[tuple[str, str]], batch_size: int = 100
) -> dict[str, int]:
    totals = {"applied": 0, "missing": 0, "protected_vinyl": 0}
    for offset in range(0, len(changes), batch_size):
        batch = changes[offset : offset + batch_size]
        arguments = ["genre-set"]
        for persistent_id, genre in batch:
            arguments.extend([persistent_id, genre])
        output = run_bridge(arguments).strip().split("\x1f")
        if len(output) != 3 or not all(value.isdigit() for value in output):
            raise AppError(
                "Music returned an invalid genre apply result. The saved run can "
                f"still be audited and retried. Result: {output!r}"
            )
        totals["applied"] += int(output[0])
        totals["missing"] += int(output[1])
        totals["protected_vinyl"] += int(output[2])
        print(f"Applied genre batch: {min(offset + len(batch), len(changes)):,}/{len(changes):,}")
    if totals["missing"] or totals["protected_vinyl"]:
        print(
            "Music skipped entries during apply: "
            f"missing={totals['missing']:,}, "
            f"protected_vinyl={totals['protected_vinyl']:,}. "
            "The verification scan will record the exact outcome."
        )
    return totals


def verify_run(connection: Any, run_id: str, expected_column: str, success: str) -> int:
    current = {track["persistent_id"]: valid_genre(track["genre"]) for track in scan_music_genres()}
    rows = connection.execute(
        "SELECT * FROM music_genre_changes WHERE run_id = ?", (run_id,)
    ).fetchall()
    count = 0
    for row in rows:
        expected = valid_genre(row[expected_column])
        ok = normalize(current.get(row["music_persistent_id"], "")) == normalize(expected)
        connection.execute(
            "UPDATE music_genre_changes SET status = ?, error = ? "
            "WHERE run_id = ? AND music_persistent_id = ?",
            (
                success if ok else "verification_failed",
                None if ok else "Music genre did not match after apply",
                run_id,
                row["music_persistent_id"],
            ),
        )
        count += 1 if ok else 0
    return count


def list_runs(connection: Any) -> None:
    rows = connection.execute(
        "SELECT * FROM music_genre_runs ORDER BY created_at DESC"
    ).fetchall()
    if not rows:
        print("No genre apply runs.")
    for row in rows:
        print(
            f"{row['run_id']}  {row['status']:<10}  "
            f"{row['applied_count']:,}/{row['planned_count']:,}  {row['created_at']}"
        )


def restore_run(connection: Any, run_id: str) -> None:
    run = connection.execute(
        "SELECT * FROM music_genre_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if not run:
        raise AppError(f"Unknown genre run: {run_id}")
    changes = connection.execute(
        "SELECT music_persistent_id, old_genre FROM music_genre_changes "
        "WHERE run_id = ? AND status = 'applied'",
        (run_id,),
    ).fetchall()
    set_genres([(row[0], row[1]) for row in changes])
    restored = verify_run(connection, run_id, "old_genre", "restored")
    connection.execute(
        "UPDATE music_genre_runs SET status = 'restored', completed_at = ? "
        "WHERE run_id = ?",
        (utc_now(), run_id),
    )
    print(f"Restored genres: {restored:,}/{len(changes):,}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        require_mac()
        with connect_db(args.db) as connection:
            if args.list_runs:
                list_runs(connection)
                return 0
            if args.restore_run:
                restore_run(connection, args.restore_run)
                return 0

        tracks = scan_music_genres()
        consensus = group_consensus(tracks)
        with connect_db(args.db) as connection:
            if args.lookup:
                perform_lookups(connection, tracks, consensus, args)
            cache = load_cache(connection)
        rows = candidate_rows(tracks, consensus, cache, args.overwrite)
        write_report(args.report, rows)
        counts: dict[str, int] = defaultdict(int)
        for row in rows:
            counts[row["action"]] += 1
        print(f"Report: {args.report}")
        for action in sorted(counts):
            print(f"  {action}: {counts[action]:,}")

        eligible_actions = {"would_fill_missing"}
        if args.overwrite:
            eligible_actions.add("would_overwrite")
        changes = [row for row in rows if row["action"] in eligible_actions]
        if not args.apply:
            print("Report-only: no Music genres changed. Add --apply after review.")
            return 0
        run_id = uuid.uuid4().hex
        with connect_db(args.db) as connection:
            connection.execute(
                "INSERT INTO music_genre_runs "
                "(run_id, mode, status, planned_count, created_at) "
                "VALUES (?, ?, 'planned', ?, ?)",
                (run_id, "overwrite" if args.overwrite else "missing_only", len(changes), utc_now()),
            )
            connection.executemany(
                "INSERT INTO music_genre_changes "
                "(run_id, music_persistent_id, old_genre, new_genre, source, status) "
                "VALUES (?, ?, ?, ?, ?, 'planned')",
                [
                    (
                        run_id, row["music_persistent_id"], row["current_genre"],
                        row["proposed_genre"], row["source"],
                    )
                    for row in changes
                ],
            )
        set_genres(
            [(row["music_persistent_id"], row["proposed_genre"]) for row in changes]
        )
        with connect_db(args.db) as connection:
            applied = verify_run(connection, run_id, "new_genre", "applied")
            status = "applied" if applied == len(changes) else "partial"
            connection.execute(
                "UPDATE music_genre_runs SET status = ?, applied_count = ?, "
                "completed_at = ? WHERE run_id = ?",
                (status, applied, utc_now(), run_id),
            )
            record_event(
                connection,
                stage="music_genres",
                event="genres_applied",
                status=status,
                details={"run_id": run_id, "planned": len(changes), "applied": applied},
                log_path=args.db.parent / "activity.jsonl",
            )
        print(f"Genre restore run: {run_id}")
        print(f"Genres applied and verified: {applied:,}/{len(changes):,}")
        return 0 if applied == len(changes) else 1
    except (AppError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
