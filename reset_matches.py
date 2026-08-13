#!/usr/bin/env python3
"""Safely clear the current YouTube matching checkpoint."""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from activity_log import record_event
from common import (
    AppError,
    DEFAULT_CSV_PATH,
    DEFAULT_DB_PATH,
    DEFAULT_XLSX_PATH,
    connect_db,
    export_catalog,
    utc_now,
)
from matching_rules import SCORING_VERSION


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Preview or reset all current YouTube matches. Matching audit "
            "history, downloaded files, Spotify metadata, and Apple Music "
            "state are preserved."
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV_PATH)
    parser.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX_PATH)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the reset. Without this flag, only a preview is shown.",
    )
    parser.add_argument(
        "--include-downloaded",
        action="store_true",
        help=(
            "Also reset matching metadata for downloaded rows. MP3 files and "
            "Apple Music data are still never deleted."
        ),
    )
    parser.add_argument(
        "--below-score",
        type=float,
        metavar="SCORE",
        help=(
            "Reset only selected matches whose saved score is strictly below "
            "SCORE. A score equal to SCORE is kept."
        ),
    )
    return parser.parse_args(argv)


def reset_scope(
    include_downloaded: bool, below_score: float | None = None
) -> tuple[str, list[Any]]:
    where = """
        (is_liked = 1 OR is_saved_album = 1)
        AND user_deleted = 0
        AND youtube_url IS NOT NULL
    """
    parameters: list[Any] = []
    if below_score is not None:
        where += " AND youtube_score < ?"
        parameters.append(below_score)
    if not include_downloaded:
        where += """
            AND COALESCE(download_status, '') != 'downloaded'
            AND download_path IS NULL
        """
    return where, parameters


def reset_preview(
    connection: sqlite3.Connection,
    include_downloaded: bool,
    below_score: float | None = None,
) -> dict[str, int]:
    where, parameters = reset_scope(include_downloaded, below_score)
    row = connection.execute(
        f"""
        SELECT
            COUNT(*) AS resettable,
            SUM(CASE WHEN match_status LIKE 'approved_%' THEN 1 ELSE 0 END)
                AS approved,
            SUM(CASE WHEN youtube_url IS NOT NULL THEN 1 ELSE 0 END)
                AS selected_sources
        FROM tracks
        WHERE {where}
        """,
        parameters,
    ).fetchone()
    protected_where, protected_parameters = reset_scope(True, below_score)
    protected = connection.execute(
        f"""
        SELECT COUNT(*)
        FROM tracks
        WHERE {protected_where}
          AND (
              COALESCE(download_status, '') = 'downloaded'
              OR download_path IS NOT NULL
          )
        """,
        protected_parameters,
    ).fetchone()[0]
    return {
        "resettable": int(row["resettable"] or 0),
        "approved": int(row["approved"] or 0),
        "selected_sources": int(row["selected_sources"] or 0),
        "protected_downloaded": 0 if include_downloaded else int(protected or 0),
    }


def backup_database(
    connection: sqlite3.Connection, db_path: Path
) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_dir = db_path.parent / "backups" / timestamp
    backup_dir.mkdir(parents=True, exist_ok=False)
    backup_path = backup_dir / db_path.name
    connection.commit()
    destination = sqlite3.connect(backup_path)
    try:
        connection.backup(destination)
        destination.commit()
    finally:
        destination.close()
    return backup_path


def reset_current_matches(
    connection: sqlite3.Connection,
    *,
    include_downloaded: bool,
    below_score: float | None = None,
    log_path: Path | None = None,
) -> dict[str, int]:
    counts = reset_preview(connection, include_downloaded, below_score)
    if not counts["resettable"]:
        return counts
    where, parameters = reset_scope(include_downloaded, below_score)
    connection.execute(
        f"""
        DELETE FROM youtube_candidates
        WHERE spotify_id IN (
            SELECT spotify_id FROM tracks WHERE {where}
        )
        """,
        parameters,
    )
    connection.execute(
        f"""
        UPDATE tracks
        SET youtube_url = NULL,
            youtube_video_id = NULL,
            youtube_title = NULL,
            youtube_channel = NULL,
            youtube_channel_verified = 0,
            youtube_duration_seconds = NULL,
            youtube_score = NULL,
            youtube_score_notes = NULL,
            youtube_score_version = ?,
            match_status = 'not_searched',
            match_attempts = 0,
            last_match_attempt_at = NULL,
            match_error = NULL,
            match_error_code = NULL,
            match_next_retry_at = NULL,
            reviewed_at = NULL,
            download_status = CASE
                WHEN download_status = 'downloaded' THEN download_status
                ELSE 'not_downloaded'
            END,
            download_error = NULL,
            download_error_code = NULL,
            download_next_retry_at = NULL,
            updated_at = ?
        WHERE {where}
        """,
        [SCORING_VERSION, utc_now(), *parameters],
    )
    record_event(
        connection,
        stage="matching",
        event="all_matches_reset",
        status="completed",
        details={
            **counts,
            "include_downloaded": include_downloaded,
            "below_score": below_score,
            "audit_history_preserved": True,
            "audio_files_deleted": False,
        },
        log_path=log_path,
    )
    return counts


def print_preview(counts: dict[str, int]) -> None:
    print(f"Matches that would be reset: {counts['resettable']:,}")
    print(f"Approved rows included:       {counts['approved']:,}")
    print(f"Selected YouTube sources:     {counts['selected_sources']:,}")
    print(f"Downloaded rows protected:   {counts['protected_downloaded']:,}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.below_score is not None and not 0 <= args.below_score <= 100:
            raise AppError("--below-score must be between 0 and 100.")
        with connect_db(args.db) as connection:
            counts = reset_preview(
                connection, args.include_downloaded, args.below_score
            )
            if args.below_score is not None:
                print(f"Score filter: strictly below {args.below_score:g}")
            print_preview(counts)
            if not args.apply:
                print(
                    "\nPreview only; nothing changed. Run again with --apply "
                    "to reset these matches."
                )
                return 0
            if not counts["resettable"]:
                print("\nNo current matches need resetting.")
                return 0
            backup_path = backup_database(connection, args.db)
            reset_current_matches(
                connection,
                include_downloaded=args.include_downloaded,
                below_score=args.below_score,
                log_path=args.db.parent / "activity.jsonl",
            )
            export_catalog(connection, args.csv, args.xlsx)
        print(f"\nReset {counts['resettable']:,} match checkpoint(s).")
        print(f"Database backup: {backup_path}")
        print("Downloaded files and Apple Music data were not deleted.")
        return 0
    except (AppError, OSError, sqlite3.Error, KeyboardInterrupt) as exc:
        print(f"\nError: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
