from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from common import DEFAULT_DB_PATH, connect_db


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show immutable YouTube matching and approval history."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--spotify-id")
    parser.add_argument("--limit", type=int, default=50)
    return parser.parse_args(argv)


def history_rows(
    connection: Any, spotify_id: str | None, limit: int
) -> list[Any]:
    where = ""
    parameters: list[Any] = []
    if spotify_id:
        where = "WHERE spotify_id = ?"
        parameters.append(spotify_id)
    parameters.append(limit)
    return connection.execute(
        f"""
        SELECT created_at, spotify_id, decision, score,
               youtube_duration_seconds, youtube_channel, youtube_title,
               reason, scoring_version
        FROM match_assessments
        {where}
        ORDER BY id DESC
        LIMIT ?
        """,
        parameters,
    ).fetchall()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.limit < 1:
        print("Error: --limit must be at least 1.")
        return 1
    with connect_db(args.db) as connection:
        rows = history_rows(connection, args.spotify_id, args.limit)
    if not rows:
        print("No matching history.")
        return 0
    for row in rows:
        score = "" if row["score"] is None else f"{row['score']:.1f}"
        print(
            f"{row['created_at']}  {row['spotify_id']}  "
            f"{row['decision']:<16} score={score:<5} "
            f"v{row['scoring_version']}  {row['youtube_title'] or ''}"
        )
        if row["reason"]:
            print(f"  {row['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
