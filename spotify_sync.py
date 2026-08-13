#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from common import (
    AppError,
    DEFAULT_CSV_PATH,
    DEFAULT_DB_PATH,
    DEFAULT_XLSX_PATH,
    SpotifyClient,
    connect_db,
    export_catalog,
    parse_release_year,
    utc_now,
)
from activity_log import record_event


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sync Spotify liked songs and optionally saved albums into "
            "SQLite, CSV, and Excel."
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV_PATH)
    parser.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX_PATH)
    parser.add_argument(
        "--skip-genres",
        action="store_true",
        help="Skip best-effort artist genre enrichment.",
    )
    parser.add_argument(
        "--include-albums",
        action="store_true",
        help=(
            "Also sync every track from albums saved in Your Library. "
            "Duplicate liked tracks are stored only once."
        ),
    )
    return parser.parse_args()


def fetch_saved_tracks(client: SpotifyClient) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    url: str | None = "/me/tracks"
    params: dict[str, Any] | None = {"limit": 50, "offset": 0}
    while url:
        page = client.get(url, params=params)
        items.extend(page.get("items", []))
        total = int(page.get("total", len(items)))
        print(f"\rFetched {len(items):,} of {total:,} liked songs", end="", flush=True)
        url = page.get("next")
        params = None
    print()
    return items


def fetch_saved_album_tracks(
    client: SpotifyClient,
) -> tuple[list[dict[str, Any]], int]:
    saved_albums: list[dict[str, Any]] = []
    url: str | None = "/me/albums"
    params: dict[str, Any] | None = {"limit": 50, "offset": 0}
    while url:
        page = client.get(url, params=params)
        saved_albums.extend(page.get("items", []))
        total = int(page.get("total", len(saved_albums)))
        print(
            f"\rFetched {len(saved_albums):,} of {total:,} saved albums",
            end="",
            flush=True,
        )
        url = page.get("next")
        params = None
    print()

    membership: dict[str, dict[str, Any]] = {}
    for saved_album in saved_albums:
        album = saved_album.get("album") or {}
        tracks_page = album.get("tracks") or {}
        while tracks_page:
            for track in tracks_page.get("items") or []:
                spotify_id = track.get("id")
                if not spotify_id:
                    continue
                fallback = dict(track)
                fallback["album"] = album
                membership[spotify_id] = {
                    "added_at": saved_album.get("added_at"),
                    "track": fallback,
                }
            next_url = tracks_page.get("next")
            if not next_url:
                tracks_page = {}
                continue
            try:
                tracks_page = client.get(next_url)
            except AppError as exc:
                print(
                    "\nAlbum-track pagination warning; keeping the tracks "
                    f"already returned by Spotify: {exc}"
                )
                tracks_page = {}

    track_ids = list(membership)
    for offset in range(0, len(track_ids), 50):
        batch = track_ids[offset : offset + 50]
        try:
            result = client.get("/tracks", params={"ids": ",".join(batch)})
        except AppError as exc:
            print(
                "\nFull saved-album track metadata is restricted by Spotify; "
                "continuing with the embedded album metadata. "
                f"Details: {exc}"
            )
            break
        for track in result.get("tracks") or []:
            if track and track.get("id") in membership:
                membership[track["id"]]["track"] = track
        print(
            f"\rHydrated {min(offset + 50, len(track_ids)):,} of "
            f"{len(track_ids):,} saved-album tracks",
            end="",
            flush=True,
        )
    if track_ids:
        print()
    return list(membership.values()), len(saved_albums)


def track_values(saved_item: dict[str, Any]) -> dict[str, Any] | None:
    track = saved_item.get("track")
    if not track or not track.get("id"):
        return None
    album = track.get("album") or {}
    artists = track.get("artists") or []
    album_artists = album.get("artists") or []
    images = album.get("images") or []
    external_ids = track.get("external_ids") or {}
    release_date = album.get("release_date")
    return {
        "spotify_id": track["id"],
        "title": track.get("name") or "",
        "artists": "; ".join(a.get("name", "") for a in artists if a.get("name")),
        "artist_ids": json.dumps(
            [a["id"] for a in artists if a.get("id")], ensure_ascii=False
        ),
        "primary_artist": artists[0].get("name") if artists else "",
        "primary_artist_id": artists[0].get("id") if artists else None,
        "album": album.get("name") or "",
        "album_id": album.get("id"),
        "album_artist": "; ".join(
            a.get("name", "") for a in album_artists if a.get("name")
        ),
        "release_date": release_date,
        "release_year": parse_release_year(release_date),
        "disc_number": track.get("disc_number"),
        "track_number": track.get("track_number"),
        "total_tracks": album.get("total_tracks"),
        "duration_ms": track.get("duration_ms"),
        "explicit": 1 if track.get("explicit") else 0,
        "isrc": external_ids.get("isrc"),
        "spotify_url": (track.get("external_urls") or {}).get("spotify"),
        "album_url": (album.get("external_urls") or {}).get("spotify"),
        "cover_url": images[0].get("url") if images else None,
        "added_at": saved_item.get("added_at"),
        "updated_at": utc_now(),
    }


def upsert_tracks(
    connection: sqlite3.Connection,
    saved_items: list[dict[str, Any]],
    *,
    membership_column: str = "is_liked",
    label: str = "liked songs",
) -> None:
    if membership_column not in {"is_liked", "is_saved_album"}:
        raise ValueError(f"Unsupported membership column: {membership_column}")
    parsed_tracks = [item for saved in saved_items if (item := track_values(saved))]
    for item in parsed_tracks:
        item["is_liked"] = 1 if membership_column == "is_liked" else 0
        item["is_saved_album"] = (
            1 if membership_column == "is_saved_album" else 0
        )
    connection.execute(f"UPDATE tracks SET {membership_column} = 0")
    added_at_update = (
        "excluded.added_at"
        if membership_column == "is_liked"
        else "CASE WHEN tracks.is_liked = 1 THEN tracks.added_at "
        "ELSE excluded.added_at END"
    )
    sql = f"""
        INSERT INTO tracks (
            spotify_id, title, artists, artist_ids, primary_artist,
            primary_artist_id, album, album_id, album_artist, release_date,
            release_year, disc_number, track_number, total_tracks, duration_ms,
            explicit, isrc, spotify_url, album_url, cover_url, added_at,
            is_liked, is_saved_album, updated_at
        ) VALUES (
            :spotify_id, :title, :artists, :artist_ids, :primary_artist,
            :primary_artist_id, :album, :album_id, :album_artist, :release_date,
            :release_year, :disc_number, :track_number, :total_tracks,
            :duration_ms, :explicit, :isrc, :spotify_url, :album_url,
            :cover_url, :added_at, :is_liked, :is_saved_album, :updated_at
        )
        ON CONFLICT(spotify_id) DO UPDATE SET
            title = excluded.title,
            artists = excluded.artists,
            artist_ids = excluded.artist_ids,
            primary_artist = excluded.primary_artist,
            primary_artist_id = excluded.primary_artist_id,
            album = excluded.album,
            album_id = excluded.album_id,
            album_artist = excluded.album_artist,
            release_date = excluded.release_date,
            release_year = excluded.release_year,
            disc_number = excluded.disc_number,
            track_number = excluded.track_number,
            total_tracks = excluded.total_tracks,
            duration_ms = excluded.duration_ms,
            explicit = excluded.explicit,
            isrc = excluded.isrc,
            spotify_url = excluded.spotify_url,
            album_url = excluded.album_url,
            cover_url = excluded.cover_url,
            added_at = {added_at_update},
            {membership_column} = 1,
            updated_at = excluded.updated_at
    """
    connection.executemany(sql, parsed_tracks)
    connection.commit()
    print(f"Saved {len(parsed_tracks):,} current {label} to SQLite.")


def enrich_genres(
    connection: sqlite3.Connection, client: SpotifyClient
) -> None:
    artists = connection.execute(
        """
        SELECT DISTINCT t.primary_artist_id, t.primary_artist
        FROM tracks t
        LEFT JOIN artist_cache a
            ON a.spotify_artist_id = t.primary_artist_id
        WHERE (t.is_liked = 1 OR t.is_saved_album = 1)
          AND t.primary_artist_id IS NOT NULL
          AND a.spotify_artist_id IS NULL
        ORDER BY t.primary_artist
        """
    ).fetchall()
    if not artists:
        print("Genre cache is already current.")
    for index, artist in enumerate(artists, start=1):
        artist_id = artist["primary_artist_id"]
        artist_name = artist["primary_artist"]
        print(
            f"\rFetching artist genres {index:,} of {len(artists):,}: "
            f"{artist_name[:40]:40}",
            end="",
            flush=True,
        )
        try:
            result = client.get(f"/artists/{artist_id}")
            genres = "; ".join(result.get("genres") or [])
            status = "ok" if genres else "empty"
        except AppError as exc:
            if "quota" in str(exc).lower():
                print()
                print(str(exc))
                break
            genres = ""
            status = "unavailable"
        connection.execute(
            """
            INSERT INTO artist_cache (
                spotify_artist_id, artist_name, genres, fetch_status, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(spotify_artist_id) DO UPDATE SET
                artist_name = excluded.artist_name,
                genres = excluded.genres,
                fetch_status = excluded.fetch_status,
                updated_at = excluded.updated_at
            """,
            (artist_id, artist_name, genres, status, utc_now()),
        )
        connection.commit()
    if artists:
        print()
    connection.execute(
        """
        UPDATE tracks
        SET genres = COALESCE(
            (SELECT genres
             FROM artist_cache
             WHERE artist_cache.spotify_artist_id = tracks.primary_artist_id),
            genres
        )
        WHERE is_liked = 1 OR is_saved_album = 1
        """
    )
    connection.commit()


def main() -> int:
    args = parse_args()
    try:
        client = SpotifyClient(args.db.parent)
        saved_tracks = fetch_saved_tracks(client)
        saved_album_tracks: list[dict[str, Any]] = []
        saved_album_count = 0
        if args.include_albums:
            saved_album_tracks, saved_album_count = fetch_saved_album_tracks(client)
        with connect_db(args.db) as connection:
            upsert_tracks(connection, saved_tracks)
            if args.include_albums:
                upsert_tracks(
                    connection,
                    saved_album_tracks,
                    membership_column="is_saved_album",
                    label="saved-album tracks",
                )
            else:
                connection.execute("UPDATE tracks SET is_saved_album = 0")
            if not args.skip_genres:
                enrich_genres(connection, client)
            record_event(
                connection,
                stage="spotify",
                event="sync_completed",
                status="ok",
                details={
                    "saved_track_count": len(saved_tracks),
                    "include_albums": args.include_albums,
                    "saved_album_count": saved_album_count,
                    "saved_album_track_count": len(saved_album_tracks),
                },
                log_path=args.db.parent / "activity.jsonl",
            )
            export_catalog(connection, args.csv, args.xlsx)
        print(f"Database: {args.db}")
        print(f"Excel review file: {args.xlsx}")
        print(f"CSV export: {args.csv}")
        return 0
    except (AppError, KeyboardInterrupt) as exc:
        print(f"\nError: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
