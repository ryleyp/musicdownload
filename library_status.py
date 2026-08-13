#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import AppError, DEFAULT_DB_PATH, connect_db


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report resumable Spotify/YouTube/download/local Music totals."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def progress_counts(connection: Any) -> dict[str, int]:
    row = connection.execute(
        """
        SELECT
            SUM(CASE
                    WHEN is_liked = 1 OR is_saved_album = 1 THEN 1 ELSE 0
                END) AS synced,
            SUM(CASE WHEN is_liked = 1 THEN 1 ELSE 0 END) AS liked,
            SUM(CASE WHEN is_saved_album = 1 THEN 1 ELSE 0 END)
                AS saved_album_tracks,
            COUNT(DISTINCT CASE
                    WHEN is_saved_album = 1 THEN album_id ELSE NULL
                END) AS saved_albums,
            SUM(CASE
                    WHEN (is_liked = 1 OR is_saved_album = 1)
                     AND youtube_url IS NOT NULL THEN 1
                    ELSE 0
                END) AS matched,
            SUM(CASE
                    WHEN (is_liked = 1 OR is_saved_album = 1)
                     AND match_status LIKE 'approved_%' THEN 1
                    ELSE 0
                END) AS approved,
            SUM(CASE
                    WHEN (is_liked = 1 OR is_saved_album = 1)
                     AND download_status = 'downloaded' THEN 1
                    ELSE 0
                END) AS downloaded,
            SUM(CASE
                    WHEN (is_liked = 1 OR is_saved_album = 1)
                     AND (
                         match_status = 'match_error'
                         OR download_status = 'error'
                     )
                    THEN 1 ELSE 0
                END) AS failed,
            SUM(CASE
                    WHEN (is_liked = 1 OR is_saved_album = 1)
                     AND match_status = 'match_error'
                    THEN 1 ELSE 0
                END) AS match_failed,
            SUM(CASE
                    WHEN (is_liked = 1 OR is_saved_album = 1)
                     AND download_status = 'error'
                    THEN 1 ELSE 0
                END) AS download_failed,
            SUM(CASE
                    WHEN (is_liked = 1 OR is_saved_album = 1)
                     AND (
                         (match_status = 'match_error'
                          AND match_next_retry_at IS NOT NULL
                          AND strftime('%s', match_next_retry_at)
                              <= strftime('%s', 'now'))
                         OR
                         (download_status = 'error'
                          AND download_next_retry_at IS NOT NULL
                          AND strftime('%s', download_next_retry_at)
                              <= strftime('%s', 'now'))
                     )
                    THEN 1 ELSE 0
                END) AS retry_due,
            SUM(CASE
                    WHEN (is_liked = 1 OR is_saved_album = 1)
                     AND apple_music_status IN (
                         'preferred_download', 'imported_new'
                     )
                    THEN 1 ELSE 0
                END) AS added_to_apple_music
        FROM tracks
        """
    ).fetchone()
    names = (
        "synced",
        "liked",
        "saved_album_tracks",
        "saved_albums",
        "matched",
        "approved",
        "downloaded",
        "failed",
        "match_failed",
        "download_failed",
        "retry_due",
        "added_to_apple_music",
    )
    return {name: int(row[name] or 0) for name in names}


def print_progress(counts: dict[str, int]) -> None:
    labels = (
        ("synced", "Synced"),
        ("liked", "Liked songs"),
        ("saved_album_tracks", "Saved-album tracks"),
        ("saved_albums", "Saved albums"),
        ("matched", "Matched"),
        ("approved", "Approved"),
        ("downloaded", "Downloaded"),
        ("failed", "Failed"),
        ("match_failed", "Matching failures"),
        ("download_failed", "Download failures"),
        ("retry_due", "Retries due"),
        ("added_to_apple_music", "Added to local Music"),
    )
    width = max(len(label) for _, label in labels)
    for key, label in labels:
        print(f"{label:<{width}}  {counts[key]:,}")


def main() -> int:
    args = parse_args()
    try:
        with connect_db(args.db) as connection:
            counts = progress_counts(connection)
        if args.json:
            print(json.dumps(counts, indent=2, sort_keys=True))
        else:
            print_progress(counts)
        return 0
    except (AppError, OSError) as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
