#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from activity_log import record_event
from common import AppError, DEFAULT_DB_PATH, connect_db
from download_mp3 import fetch_artwork, tag_mp3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Embed missing Spotify album artwork in downloaded MP3 files. "
            "Existing artwork is preserved."
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--limit", type=int)
    return parser.parse_args()


def has_embedded_artwork(path: Path) -> bool:
    try:
        from mutagen.id3 import ID3

        return bool(ID3(path).getall("APIC"))
    except Exception:
        return False


def main() -> int:
    args = parse_args()
    try:
        with connect_db(args.db) as connection:
            sql = """
                SELECT * FROM tracks
                WHERE download_status = 'downloaded'
                  AND download_path IS NOT NULL
                ORDER BY primary_artist COLLATE NOCASE, album COLLATE NOCASE,
                         disc_number, track_number
            """
            parameters: list[int] = []
            if args.limit is not None:
                if args.limit < 1:
                    raise AppError("--limit must be at least 1.")
                sql += " LIMIT ?"
                parameters.append(args.limit)
            tracks = connection.execute(sql, parameters).fetchall()
            counts = {
                "checked": 0,
                "already_present": 0,
                "embedded": 0,
                "no_source": 0,
                "missing_file": 0,
                "failed": 0,
            }
            cache_dir = args.db.parent.parent / "downloads" / ".artwork"
            for index, track in enumerate(tracks, start=1):
                counts["checked"] += 1
                path = Path(track["download_path"])
                print(
                    f"[{index:,}/{len(tracks):,}] "
                    f"{track['primary_artist']} - {track['title']}"
                )
                if not path.exists():
                    counts["missing_file"] += 1
                    print("  Download file is missing; skipped.")
                    continue
                if has_embedded_artwork(path):
                    counts["already_present"] += 1
                    continue
                try:
                    artwork = fetch_artwork(track, cache_dir)
                    if artwork is None:
                        counts["no_source"] += 1
                        print("  Spotify has no cover URL; skipped.")
                        continue
                    tag_mp3(path, track, artwork)
                    counts["embedded"] += 1
                    record_event(
                        connection,
                        stage="artwork",
                        event="artwork_embedded",
                        status="completed",
                        spotify_id=track["spotify_id"],
                        details={"path": str(path)},
                        log_path=args.db.parent / "activity.jsonl",
                    )
                    connection.commit()
                    print("  Embedded Spotify album artwork.")
                except Exception as exc:
                    counts["failed"] += 1
                    print(f"  Artwork warning: {str(exc)[:500]}")
            print("Artwork summary")
            for key, value in counts.items():
                print(f"  {key.replace('_', ' ').title()}: {value:,}")
        return 0
    except (AppError, KeyboardInterrupt) as exc:
        print(f"\nError: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
