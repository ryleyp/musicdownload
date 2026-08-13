#!/usr/bin/env python3
"""Recreate Spotify playlists in Music from already-downloaded local tracks."""
from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import tempfile
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from activity_log import record_event
from apple_music_duplicates import (
    marker_spotify_id,
    require_mac,
    run_bridge,
    scan_music_library,
)
from common import (
    AppError,
    DEFAULT_DB_PATH,
    PROJECT_DIR,
    SpotifyClient,
    backup_existing_file,
    connect_db,
    utc_now,
)


DEFAULT_REPORT = PROJECT_DIR / "data" / "spotify_playlist_music_report.csv"
DEFAULT_PREFIX = "Spotify - "
PLAYLIST_SCOPES = ("playlist-read-private", "playlist-read-collaborative")
PLAYLIST_TOKEN = ".spotify_playlist_token.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sync Spotify playlist membership and recreate additive-only Music "
            "playlists using already-downloaded tracks."
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--prefix", default=DEFAULT_PREFIX)
    parser.add_argument(
        "--playlist",
        action="append",
        default=[],
        help="Only process a Spotify playlist name or ID. May be repeated.",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help=(
            "Fetch a fresh immutable playlist snapshot from Spotify. Uses a "
            "separate playlist authorization token."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Create missing Music playlists and append missing downloaded "
            "tracks when the existing order is a safe prefix. Never removes."
        ),
    )
    parser.add_argument(
        "--open-music",
        action="store_true",
        help="Open Music after completion.",
    )
    return parser.parse_args(argv)


def fetch_all_playlists(client: SpotifyClient) -> list[dict[str, Any]]:
    playlists: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    fetched_count = 0
    url: str | None = "/me/playlists"
    params: dict[str, Any] | None = {"limit": 50, "offset": 0}
    while url:
        page = client.get(url, params=params)
        page_items = page.get("items") or []
        fetched_count += len(page_items)
        for playlist in page_items:
            playlist_id = str(playlist.get("id") or "")
            if playlist_id and playlist_id not in seen_ids:
                seen_ids.add(playlist_id)
                playlists.append(playlist)
        total = int(page.get("total", fetched_count))
        print(f"\rFetched {fetched_count:,} of {total:,} playlists", end="", flush=True)
        url = page.get("next")
        params = None
    print()
    if fetched_count != len(playlists):
        print(
            f"Spotify returned {fetched_count - len(playlists):,} duplicate "
            "playlist listing(s); kept the first occurrence of each ID."
        )
    return playlists


def fetch_playlist_items(
    client: SpotifyClient, playlist_id: str
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    url: str | None = f"/playlists/{playlist_id}/items"
    params: dict[str, Any] | None = {
        "limit": 50,
        "offset": 0,
        "additional_types": "track,episode",
    }
    while url:
        page = client.get(url, params=params)
        items.extend(page.get("items") or [])
        url = page.get("next")
        params = None
    return items


def normalized_playlist_item(entry: dict[str, Any]) -> dict[str, Any]:
    item = entry.get("track") or entry.get("item") or {}
    item_type = item.get("type") or "unknown"
    artists = item.get("artists") or []
    album = item.get("album") or {}
    return {
        "spotify_track_id": item.get("id"),
        "title": item.get("name") or "",
        "artist": "; ".join(
            artist.get("name", "") for artist in artists if artist.get("name")
        ),
        "album": album.get("name") or "",
        "added_at": entry.get("added_at"),
        "item_type": item_type,
        "is_local": 1 if item.get("is_local") else 0,
    }


def sync_snapshot(
    connection: Any, client: SpotifyClient
) -> tuple[str, int, int]:
    playlists = fetch_all_playlists(client)
    captured: list[tuple[dict[str, Any], list[dict[str, Any]], str | None]] = []
    total_items = 0
    for index, playlist in enumerate(playlists, start=1):
        playlist_id = str(playlist.get("id") or "")
        if not playlist_id:
            continue
        print(f"[{index:,}/{len(playlists):,}] {playlist.get('name') or playlist_id}")
        try:
            entries = fetch_playlist_items(client, playlist_id)
            error = None
        except AppError as exc:
            entries = []
            error = str(exc)
            print(f"  Could not read items; recorded for review: {exc}")
        items = [normalized_playlist_item(entry) for entry in entries]
        total_items += len(items)
        captured.append((playlist, items, error))

    run_id = uuid.uuid4().hex
    synced_at = utc_now()
    connection.execute(
        "INSERT INTO spotify_playlist_sync_runs "
        "(run_id, synced_at, playlist_count, item_count) VALUES (?, ?, ?, ?)",
        (run_id, synced_at, len(captured), total_items),
    )
    for playlist, items, error in captured:
        playlist_id = str(playlist["id"])
        connection.execute(
            """
            INSERT INTO spotify_playlists (
                run_id, spotify_playlist_id, name, description, owner_name,
                spotify_url, snapshot_id, item_count, fetch_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                playlist_id,
                playlist.get("name") or "Untitled Spotify Playlist",
                playlist.get("description") or "",
                (playlist.get("owner") or {}).get("display_name") or "",
                (playlist.get("external_urls") or {}).get("spotify") or "",
                playlist.get("snapshot_id"),
                len(items),
                error,
            ),
        )
        connection.executemany(
            """
            INSERT INTO spotify_playlist_tracks (
                run_id, spotify_playlist_id, position, spotify_track_id,
                title, artist, album, added_at, item_type, is_local
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    playlist_id,
                    position,
                    item["spotify_track_id"],
                    item["title"],
                    item["artist"],
                    item["album"],
                    item["added_at"],
                    item["item_type"],
                    item["is_local"],
                )
                for position, item in enumerate(items)
            ],
        )
    record_event(
        connection,
        stage="spotify_playlists",
        event="snapshot_synced",
        status="completed",
        details={
            "run_id": run_id,
            "playlist_count": len(captured),
            "item_count": total_items,
        },
    )
    connection.commit()
    return run_id, len(captured), total_items


def latest_run_id(connection: Any) -> str:
    row = connection.execute(
        "SELECT run_id FROM spotify_playlist_sync_runs "
        "ORDER BY synced_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise AppError(
            "No Spotify playlist snapshot exists. Run: "
            "music-library playlists --sync"
        )
    return str(row["run_id"])


def target_playlist_names(playlists: list[dict[str, Any]], prefix: str) -> dict[str, str]:
    name_counts = Counter(playlist["name"].casefold() for playlist in playlists)
    result: dict[str, str] = {}
    for playlist in playlists:
        suffix = (
            f" [{playlist['spotify_playlist_id'][:6]}]"
            if name_counts[playlist["name"].casefold()] > 1
            else ""
        )
        clean_name = " ".join(str(playlist["name"]).replace("\x00", " ").split())
        result[playlist["spotify_playlist_id"]] = f"{prefix}{clean_name}{suffix}"[:250]
    return result


def playlist_state(name: str) -> tuple[bool, list[str]]:
    output = run_bridge(["playlist-state", name]).rstrip("\n")
    fields = output.split("\x1f", 1)
    exists = fields[0].casefold() == "true"
    ids = [value for value in (fields[1] if len(fields) > 1 else "").split("\x1e") if value]
    return exists, ids


def append_playlist(name: str, ids: list[str]) -> dict[str, int]:
    output = run_bridge(["playlist-append", name, *ids]).strip()
    fields = output.split("\x1f")
    if len(fields) != 4:
        raise AppError(f"Music returned an invalid playlist append result: {output!r}")
    labels = ("requested", "added", "missing", "final")
    return {label: int(value) for label, value in zip(labels, fields, strict=True)}


def safe_append_tail(existing: list[str], desired: list[str]) -> list[str]:
    """Resume after an order-preserving existing subsequence.

    Music sometimes silently refuses duplicate-equivalent library entries. An
    ordered subsequence proves that the entries it accepted retain Spotify
    order without requiring any removal or reorder operation.
    """
    desired_index = 0
    for existing_id in existing:
        try:
            match_index = desired.index(existing_id, desired_index)
        except ValueError as exc:
            raise AppError(
                "Existing Music playlist order conflicts with the downloaded "
                "Spotify order; left unchanged for manual review."
            ) from exc
        desired_index = match_index + 1
    return desired[desired_index:]


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_existing_file(path)
    fields = [
        "spotify_playlist_id", "spotify_playlist", "spotify_playlist_url",
        "music_playlist", "position", "spotify_track_id", "title", "artist",
        "album", "music_persistent_id", "music_location", "status", "reason",
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        require_mac()
        with connect_db(args.db) as connection:
            if args.sync:
                client = SpotifyClient(
                    args.db.parent,
                    scopes=PLAYLIST_SCOPES,
                    token_filename=PLAYLIST_TOKEN,
                )
                run_id, playlist_count, item_count = sync_snapshot(connection, client)
                print(
                    f"Saved immutable snapshot {run_id}: {playlist_count:,} "
                    f"playlists, {item_count:,} items."
                )
            else:
                run_id = latest_run_id(connection)

            playlist_rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM spotify_playlists WHERE run_id = ? "
                    "ORDER BY name COLLATE NOCASE, spotify_playlist_id",
                    (run_id,),
                )
            ]
            filters = {value.casefold() for value in args.playlist}
            if filters:
                playlist_rows = [
                    row for row in playlist_rows
                    if row["name"].casefold() in filters
                    or row["spotify_playlist_id"].casefold() in filters
                ]
                if not playlist_rows:
                    raise AppError("No playlist matched --playlist.")
            item_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in connection.execute(
                "SELECT * FROM spotify_playlist_tracks WHERE run_id = ? "
                "ORDER BY spotify_playlist_id, position",
                (run_id,),
            ):
                item_rows[row["spotify_playlist_id"]].append(dict(row))
            blocked_ids = {
                str(row["spotify_id"])
                for row in connection.execute(
                    "SELECT spotify_id FROM tracks WHERE user_deleted = 1"
                )
            }

        music_tracks = scan_music_library()
        music_by_spotify: dict[str, dict[str, Any]] = {}
        for track in music_tracks:
            spotify_id = marker_spotify_id(str(track.get("comment") or ""))
            if not spotify_id or not track.get("enabled"):
                continue
            current = music_by_spotify.get(spotify_id)
            if current is None or (track.get("location") and not current.get("location")):
                music_by_spotify[spotify_id] = track

        target_names = target_playlist_names(playlist_rows, args.prefix)
        report_rows: list[dict[str, Any]] = []
        desired_by_playlist: dict[str, list[str]] = defaultdict(list)
        ready_count = 0
        unavailable_count = 0
        for playlist in playlist_rows:
            playlist_id = playlist["spotify_playlist_id"]
            target_name = target_names[playlist_id]
            if playlist.get("fetch_error"):
                report_rows.append({
                    "spotify_playlist_id": playlist_id,
                    "spotify_playlist": playlist["name"],
                    "spotify_playlist_url": playlist["spotify_url"],
                    "music_playlist": target_name,
                    "position": "", "spotify_track_id": "", "title": "",
                    "artist": "", "album": "", "music_persistent_id": "",
                    "music_location": "", "status": "playlist_unavailable",
                    "reason": playlist["fetch_error"],
                })
                continue
            for item in item_rows[playlist_id]:
                spotify_id = item.get("spotify_track_id")
                music_track = music_by_spotify.get(str(spotify_id or ""))
                if item.get("item_type") != "track":
                    status, reason = "unavailable", "unsupported_non_track_item"
                elif not spotify_id:
                    status, reason = "unavailable", "spotify_local_or_removed_track"
                elif str(spotify_id) in blocked_ids:
                    status, reason = "unavailable", "user_deleted_blocked"
                elif not music_track:
                    status, reason = "unavailable", "downloaded_music_copy_not_found"
                else:
                    status, reason = "ready", "downloaded_music_copy_found"
                    ready_count += 1
                    desired_by_playlist[playlist_id].append(
                        str(music_track["persistent_id"])
                    )
                if status != "ready":
                    unavailable_count += 1
                report_rows.append({
                    "spotify_playlist_id": playlist_id,
                    "spotify_playlist": playlist["name"],
                    "spotify_playlist_url": playlist["spotify_url"],
                    "music_playlist": target_name,
                    "position": int(item["position"]) + 1,
                    "spotify_track_id": spotify_id or "",
                    "title": item.get("title") or "",
                    "artist": item.get("artist") or "",
                    "album": item.get("album") or "",
                    "music_persistent_id": music_track["persistent_id"] if music_track else "",
                    "music_location": music_track.get("location", "") if music_track else "",
                    "status": status,
                    "reason": reason,
                })

        write_report(args.report, report_rows)
        print(f"Report: {args.report}")
        print(f"Spotify playlists: {len(playlist_rows):,}")
        print(f"Downloaded playlist entries ready: {ready_count:,}")
        print(f"Unavailable playlist entries: {unavailable_count:,}")

        created_or_resumed = 0
        appended = 0
        conflicts = 0
        apply_errors = 0
        rejected_by_music = 0
        if args.apply:
            for index, playlist in enumerate(playlist_rows, start=1):
                playlist_id = playlist["spotify_playlist_id"]
                if playlist.get("fetch_error"):
                    continue
                target_name = target_names[playlist_id]
                desired = desired_by_playlist[playlist_id]
                try:
                    _exists, existing = playlist_state(target_name)
                    tail = safe_append_tail(existing, desired)
                except AppError as exc:
                    conflicts += 1
                    print(f"[{index:,}/{len(playlist_rows):,}] {target_name}: {exc}")
                    continue
                try:
                    result = append_playlist(target_name, tail)
                except AppError as exc:
                    apply_errors += 1
                    print(
                        f"[{index:,}/{len(playlist_rows):,}] {target_name}: "
                        f"apply error; saved for retry: {exc}"
                    )
                    continue
                created_or_resumed += 1
                appended += result["added"]
                rejected_by_music += result["missing"]
                print(
                    f"[{index:,}/{len(playlist_rows):,}] {target_name}: "
                    f"{result['added']:,} added, {result['missing']:,} skipped "
                    f"by Music, {result['final']:,} total"
                )
            print(f"Music playlists safely created/resumed: {created_or_resumed:,}")
            print(f"Playlist entries appended: {appended:,}")
            print(f"Existing-order conflicts left unchanged: {conflicts:,}")
            print(f"Playlist apply errors saved for retry: {apply_errors:,}")
            print(f"Individual entries skipped by Music: {rejected_by_music:,}")
        else:
            print("Report-only: no Music playlist was changed. Add --apply when ready.")

        with connect_db(args.db) as connection:
            record_event(
                connection,
                stage="spotify_playlists",
                event="music_playlists_applied" if args.apply else "music_playlist_report",
                status="completed",
                details={
                    "run_id": run_id,
                    "playlists": len(playlist_rows),
                    "ready_entries": ready_count,
                    "unavailable_entries": unavailable_count,
                    "appended": appended,
                    "conflicts": conflicts,
                    "apply_errors": apply_errors,
                    "rejected_by_music": rejected_by_music,
                },
                log_path=args.db.parent / "activity.jsonl",
            )
        if args.open_music:
            subprocess.run(["open", "-a", "Music"], check=False)
        return 0
    except (AppError, OSError, subprocess.SubprocessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
