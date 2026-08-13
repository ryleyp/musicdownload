#!/usr/bin/env python3
from __future__ import annotations

import argparse
import mimetypes
import os
import random
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]

from common import (
    AppError,
    DEFAULT_DB_PATH,
    PROJECT_DIR,
    connect_db,
    dependency_help,
    utc_now,
)
from activity_log import record_event
from failures import classify_failure, next_retry_time
from matching_rules import (
    DOWNLOADED_FILE_MAX_DURATION_DIFFERENCE_SECONDS,
    SCORING_VERSION,
    STORED_SCORE_COMPATIBLE_SINCE,
)
from youtube_match import automatic_approval_eligible, candidate_score


DEFAULT_OUTPUT = PROJECT_DIR / "downloads"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download the next batch of approved YouTube sources as tagged "
            "MP3 files, resuming from the SQLite checkpoint."
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--spotify-id",
        action="append",
        default=[],
        metavar="ID",
        help="Download only this Spotify track ID. May be repeated.",
    )
    parser.add_argument(
        "--batch-size",
        "--limit",
        dest="batch_size",
        type=int,
        default=100,
        help=(
            "Number of unfinished tracks to process this run. Default: 100. "
            "--limit remains available as an alias."
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process every unfinished approved track instead of one batch.",
    )
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="Include tracks that failed on an earlier run.",
    )
    parser.add_argument(
        "--errors-only",
        action="store_true",
        help=(
            "Process only tracks currently marked as failed. Use with --all "
            "to retry every eligible failure."
        ),
    )
    parser.add_argument(
        "--retry-due",
        action="store_true",
        help="Include failed downloads whose scheduled retry time has arrived.",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        metavar="SCORE",
        help=(
            "Download existing suggested or approved matches at or above SCORE. "
            "Stored YouTube metadata is re-scored locally with the current "
            "rules; no --refresh search is required. Safety gates still apply."
        ),
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show checkpoint totals without downloading anything.",
    )
    parser.add_argument(
        "--redownload",
        action="store_true",
        help="Download again even when the database says a track completed.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be downloaded without changing files.",
    )
    parser.add_argument(
        "--po-token-provider",
        action="store_true",
        help=(
            "Use the yt-dlp mweb client with an installed local PO-token "
            "provider. No browser cookies are used."
        ),
    )
    parser.add_argument(
        "--sleep-min-seconds",
        type=float,
        default=7.0,
        help="Minimum delay before each download attempt. Default: 7.",
    )
    parser.add_argument(
        "--sleep-max-seconds",
        type=float,
        default=12.0,
        help="Maximum randomized delay before each attempt. Default: 12.",
    )
    parser.add_argument(
        "--max-consecutive-auth-errors",
        type=int,
        default=3,
        help=(
            "Stop the run after this many consecutive YouTube authentication "
            "or bot-check failures. Default: 3."
        ),
    )
    parser.add_argument(
        "--auth-cooldown-min-seconds",
        type=float,
        default=0.0,
        help=(
            "After a YouTube bot/authentication check, persist and wait this "
            "many seconds before continuing. Later consecutive checks double "
            "the wait up to --auth-cooldown-max-seconds. Default: 0 (off)."
        ),
    )
    parser.add_argument(
        "--auth-cooldown-max-seconds",
        type=float,
        default=3600.0,
        help="Maximum adaptive authentication cooldown. Default: 3600.",
    )
    parser.add_argument(
        "--auth-retries-per-track",
        type=int,
        default=3,
        help=(
            "Requeue a track this many times after authentication cooldowns. "
            "Default: 3."
        ),
    )
    return parser.parse_args()


YOUTUBE_COOLDOWN_KEY = "youtube_download_cooldown_until"


def set_youtube_cooldown(connection: Any, seconds: float) -> str:
    until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    value = until.replace(microsecond=0).isoformat()
    connection.execute(
        """
        INSERT INTO workflow_state (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (YOUTUBE_COOLDOWN_KEY, value, utc_now()),
    )
    connection.commit()
    return value


def clear_youtube_cooldown(connection: Any) -> None:
    connection.execute(
        "DELETE FROM workflow_state WHERE key = ?", (YOUTUBE_COOLDOWN_KEY,)
    )
    connection.commit()


def wait_for_youtube_cooldown(connection: Any) -> float:
    row = connection.execute(
        "SELECT value FROM workflow_state WHERE key = ?",
        (YOUTUBE_COOLDOWN_KEY,),
    ).fetchone()
    if row is None:
        return 0.0
    try:
        until = datetime.fromisoformat(row["value"])
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        seconds = max(0.0, (until - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds > 0:
        print(
            f"YouTube cooldown is active until {row['value']} "
            f"({seconds / 60:.1f} minutes remaining)."
        )
        time.sleep(seconds)
    clear_youtube_cooldown(connection)
    return seconds


def clean_component(value: str | None, fallback: str) -> str:
    text = re.sub(r"[\x00-\x1f/:*?\"<>|]+", "-", value or "")
    text = re.sub(r"\s+", " ", text).strip(" .-")
    return (text or fallback)[:140]


def final_path(output: Path, track: Any) -> Path:
    artist = clean_component(track["primary_artist"], "Unknown Artist")
    album = clean_component(track["album"], "Unknown Album")
    year = track["release_year"]
    album_folder = f"{year} - {album}" if year else album
    disc_number = int(track["disc_number"] or 1)
    track_number = int(track["track_number"] or 0)
    number = (
        f"{disc_number}-{track_number:02d}"
        if disc_number > 1
        else f"{track_number:02d}"
    )
    title = clean_component(track["title"], track["spotify_id"])
    candidate = output / artist / album_folder / f"{number} - {title}.mp3"
    if not candidate.exists():
        return candidate
    try:
        from mutagen.id3 import ID3

        tags = ID3(candidate)
        ids = tags.getall("TXXX")
        for frame in ids:
            if frame.desc == "SPOTIFY_ID" and track["spotify_id"] in frame.text:
                return candidate
    except Exception:
        pass
    return candidate.with_name(
        f"{number} - {title} [{track['spotify_id'][:8]}].mp3"
    )


def check_dependencies() -> None:
    if shutil.which("yt-dlp") is None:
        raise AppError(
            "yt-dlp was not found. Activate the virtual environment and run "
            "python -m pip install -r requirements.txt."
        )
    if shutil.which("ffmpeg") is None:
        raise AppError("ffmpeg was not found. On a Mac, run: brew install ffmpeg")
    try:
        import mutagen  # noqa: F401
    except ImportError as exc:
        raise AppError(dependency_help("mutagen")) from exc
    if requests is None:
        raise AppError(dependency_help("requests"))


def selected_candidate(track: Any) -> dict[str, Any]:
    return {
        "title": track["youtube_title"],
        "channel": track["youtube_channel"],
        "duration": track["youtube_duration_seconds"],
        "channel_is_verified": bool(track["youtube_channel_verified"]),
    }


def score_existing_match(
    track: Any, threshold: float
) -> tuple[float, str, bool, str]:
    candidate = selected_candidate(track)
    local_score, notes, hard_reject = candidate_score(track, candidate)
    stored_score = track["youtube_score"]
    current_stored_score = (
        STORED_SCORE_COMPATIBLE_SINCE
        <= track["youtube_score_version"]
        <= SCORING_VERSION
        and isinstance(stored_score, (int, float))
    )
    score = float(stored_score) if current_stored_score else local_score
    if current_stored_score:
        notes.append(
            f"using compatible hydrated policy-v"
            f"{track['youtube_score_version']} score "
            f"{score:.1f}"
        )
    eligible, reason = automatic_approval_eligible(
        track, candidate, score, hard_reject, threshold
    )
    return score, "; ".join(notes), eligible, reason


def select_download_tracks(
    connection: Any,
    *,
    min_score: float | None,
    retry_errors: bool,
    redownload: bool,
    batch_size: int,
    process_all: bool,
    retry_due: bool = False,
    errors_only: bool = False,
    spotify_ids: list[str] | tuple[str, ...] = (),
) -> tuple[list[Any], dict[str, tuple[float, str, str]]]:
    if min_score is None:
        where = (
            "(is_liked = 1 OR is_saved_album = 1) "
            "AND match_status LIKE 'approved_%'"
        )
    else:
        where = """
            (is_liked = 1 OR is_saved_album = 1)
            AND youtube_url IS NOT NULL
            AND (
                match_status = 'suggested'
                OR match_status LIKE 'approved_%'
            )
        """
    where += " AND user_deleted = 0"
    parameters: list[Any] = []
    if spotify_ids:
        placeholders = ", ".join("?" for _ in spotify_ids)
        where += f" AND spotify_id IN ({placeholders})"
        parameters.extend(spotify_ids)
    if not redownload:
        if errors_only:
            statuses = ["error"]
        else:
            statuses = ["not_downloaded", "downloading"]
        if retry_errors and not errors_only:
            statuses.append("error")
        elif retry_due and not errors_only:
            statuses.append("error")
        placeholders = ", ".join("?" for _ in statuses)
        where += f" AND download_status IN ({placeholders})"
        parameters.extend(statuses)
        if retry_due and not retry_errors:
            where += (
                " AND (download_status != 'error' "
                "OR download_next_retry_at IS NULL "
                "OR download_next_retry_at <= ?)"
            )
            parameters.append(utc_now())
    rows = connection.execute(
        f"""
        SELECT *
        FROM tracks
        WHERE {where}
        ORDER BY
            CASE WHEN download_status = 'error' THEN 1 ELSE 0 END,
            primary_artist COLLATE NOCASE,
            album COLLATE NOCASE,
            disc_number,
            track_number
        """,
        parameters,
    ).fetchall()
    assessments: dict[str, tuple[float, str, str]] = {}
    if min_score is not None:
        eligible_rows = []
        for row in rows:
            score, notes, eligible, reason = score_existing_match(row, min_score)
            if eligible:
                eligible_rows.append(row)
                assessments[row["spotify_id"]] = (score, notes, reason)
        rows = eligible_rows
    if not process_all:
        rows = rows[:batch_size]
    return rows, assessments


def print_status(connection: Any) -> dict[str, int]:
    row = connection.execute(
        """
        SELECT
            COUNT(*) AS approved,
            SUM(CASE WHEN download_status = 'downloaded' THEN 1 ELSE 0 END)
                AS downloaded,
            SUM(CASE
                    WHEN download_status IN ('not_downloaded', 'downloading')
                    THEN 1 ELSE 0
                END) AS pending,
            SUM(CASE WHEN download_status = 'error' THEN 1 ELSE 0 END)
                AS errors
        FROM tracks
        WHERE (is_liked = 1 OR is_saved_album = 1)
          AND user_deleted = 0
          AND match_status LIKE 'approved_%'
        """
    ).fetchone()
    counts = {
        "approved": int(row["approved"] or 0),
        "downloaded": int(row["downloaded"] or 0),
        "pending": int(row["pending"] or 0),
        "errors": int(row["errors"] or 0),
    }
    print(
        "Checkpoint: "
        f"{counts['downloaded']:,} downloaded, "
        f"{counts['pending']:,} pending, "
        f"{counts['errors']:,} failed, "
        f"{counts['approved']:,} approved total."
    )
    return counts


def file_has_spotify_id(path: Path, spotify_id: str) -> bool:
    if not path.exists():
        return False
    try:
        from mutagen.id3 import ID3

        for frame in ID3(path).getall("TXXX"):
            if frame.desc == "SPOTIFY_ID" and spotify_id in frame.text:
                return True
    except Exception:
        return False
    return False


class DownloadRuntimeMismatch(AppError):
    def __init__(
        self,
        message: str,
        actual_seconds: float,
        difference_seconds: float | None,
    ):
        super().__init__(message)
        self.actual_seconds = actual_seconds
        self.difference_seconds = difference_seconds


def validate_downloaded_runtime(
    path: Path,
    track: Any,
    tolerance: float = DOWNLOADED_FILE_MAX_DURATION_DIFFERENCE_SECONDS,
) -> tuple[float, float]:
    try:
        from mutagen.mp3 import MP3
    except ImportError as exc:
        raise AppError(dependency_help("mutagen")) from exc
    try:
        actual = float(MP3(path).info.length)
    except Exception as exc:
        raise AppError(f"Could not read downloaded MP3 runtime: {exc}") from exc
    if track["duration_ms"] is None:
        raise DownloadRuntimeMismatch(
            "Downloaded runtime cannot be approved because Spotify runtime is missing.",
            actual,
            None,
        )
    spotify_seconds = float(track["duration_ms"]) / 1000.0
    difference = abs(actual - spotify_seconds)
    if difference > tolerance:
        raise DownloadRuntimeMismatch(
            "Downloaded runtime mismatch: "
            f"Spotify {spotify_seconds:.3f}s, MP3 {actual:.3f}s, "
            f"difference {difference:.3f}s exceeds {tolerance:.3f}s.",
            actual,
            difference,
        )
    return actual, difference


def yt_dlp_download_command(
    track: Any,
    temporary_dir: Path,
    use_po_token_provider: bool = False,
) -> list[str]:
    temporary_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(temporary_dir / f"{track['spotify_id']}.%(ext)s")
    command = [
        "yt-dlp",
        "--ignore-config",
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "--force-overwrites",
        "-f",
        "bestaudio/best",
        "-x",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "0",
        "--no-embed-metadata",
        "--no-embed-thumbnail",
        "--print",
        "after_move:filepath",
        "-o",
        output_template,
    ]
    if use_po_token_provider:
        command.extend(
            [
                "--extractor-args",
                "youtube:player_client=mweb",
            ]
        )
    command.append(track["youtube_url"])
    return command


def download_audio(
    track: Any,
    temporary_dir: Path,
    use_po_token_provider: bool = False,
) -> Path:
    command = yt_dlp_download_command(
        track,
        temporary_dir,
        use_po_token_provider=use_po_token_provider,
    )
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "Unknown yt-dlp error").strip()
        folded = message.casefold()
        if "sign in" in folded or "bot" in folded or "cookies" in folded:
            message += (
                "\nYouTube requested authentication or detected automation. "
                "Update yt-dlp first; do not add account cookies unless you "
                "understand the account risk."
            )
        elif "403" in folded or "429" in folded:
            message += (
                "\nYouTube temporarily rejected the request. Update yt-dlp and "
                "retry this failed track later with --retry-errors."
            )
        raise AppError(message[-1000:])
    printed_paths = [
        Path(line.strip())
        for line in result.stdout.splitlines()
        if line.strip().endswith(".mp3")
    ]
    if printed_paths and printed_paths[-1].exists():
        return printed_paths[-1]
    expected = temporary_dir / f"{track['spotify_id']}.mp3"
    if expected.exists():
        return expected
    raise AppError("yt-dlp finished but the MP3 file could not be located.")


def fetch_artwork(track: Any, cache_dir: Path) -> tuple[bytes, str] | None:
    if requests is None:
        raise AppError(dependency_help("requests"))
    url = track["cover_url"]
    if not url:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    album_id = track["album_id"] or track["spotify_id"]
    cache_file = cache_dir / f"{album_id}.art"
    mime_file = cache_dir / f"{album_id}.mime"
    if cache_file.exists():
        mime = (
            mime_file.read_text(encoding="utf-8").strip()
            if mime_file.exists()
            else "image/jpeg"
        )
        return cache_file.read_bytes(), mime
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    mime = response.headers.get("Content-Type", "").split(";")[0]
    if not mime.startswith("image/"):
        mime = mimetypes.guess_type(url)[0] or "image/jpeg"
    cache_file.write_bytes(response.content)
    mime_file.write_text(mime, encoding="utf-8")
    return response.content, mime


def tag_mp3(path: Path, track: Any, artwork: tuple[bytes, str] | None) -> None:
    from mutagen.id3 import (
        APIC,
        COMM,
        ID3,
        TALB,
        TCON,
        TDRC,
        TIT2,
        TPE1,
        TPE2,
        TPOS,
        TRCK,
        TCMP,
        TSRC,
        TXXX,
    )
    from mutagen.mp3 import MP3

    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    else:
        # ffmpeg/yt-dlp may leave an empty ID3 container. Reuse it instead of
        # calling add_tags(), which raises "an ID3 tag already exists".
        audio.tags.clear()
    tags = audio.tags
    assert tags is not None
    tags.add(TIT2(encoding=3, text=[track["title"]]))
    tags.add(TPE1(encoding=3, text=[track["artists"]]))
    tags.add(
        TPE2(
            encoding=3,
            text=[track["album_artist"] or track["primary_artist"]],
        )
    )
    tags.add(TALB(encoding=3, text=[track["album"] or ""]))
    if track["release_date"]:
        tags.add(TDRC(encoding=3, text=[track["release_date"]]))
    if track["genres"]:
        tags.add(TCON(encoding=3, text=[track["genres"]]))
    if track["track_number"]:
        track_text = str(track["track_number"])
        if track["total_tracks"]:
            track_text += f"/{track['total_tracks']}"
        tags.add(TRCK(encoding=3, text=[track_text]))
    if track["disc_number"]:
        tags.add(TPOS(encoding=3, text=[str(track["disc_number"])]))
    tags.add(TCMP(encoding=3, text=["1"]))
    if track["isrc"]:
        tags.add(TSRC(encoding=3, text=[track["isrc"]]))
    tags.add(
        COMM(
            encoding=3,
            lang="eng",
            desc="Sources",
            text=[
                f"Spotify: {track['spotify_url'] or ''}\n"
                f"YouTube: {track['youtube_url'] or ''}"
            ],
        )
    )
    tags.add(TXXX(encoding=3, desc="SPOTIFY_ID", text=[track["spotify_id"]]))
    tags.add(
        TXXX(
            encoding=3,
            desc="YOUTUBE_ID",
            text=[track["youtube_video_id"] or ""],
        )
    )
    tags.add(
        TXXX(
            encoding=3,
            desc="EXPLICIT",
            text=["1" if track["explicit"] else "0"],
        )
    )
    if artwork:
        image_bytes, mime = artwork
        tags.add(
            APIC(
                encoding=3,
                mime=mime,
                type=3,
                desc="Cover",
                data=image_bytes,
            )
        )
    audio.save(v2_version=3)


def main() -> int:
    args = parse_args()
    try:
        if args.batch_size < 1:
            raise AppError("--batch-size must be at least 1.")
        if args.sleep_min_seconds < 0 or args.sleep_max_seconds < 0:
            raise AppError("Download sleep delays cannot be negative.")
        if args.sleep_max_seconds < args.sleep_min_seconds:
            raise AppError(
                "--sleep-max-seconds must be greater than or equal to "
                "--sleep-min-seconds."
            )
        if args.max_consecutive_auth_errors < 1:
            raise AppError("--max-consecutive-auth-errors must be at least 1.")
        if args.auth_cooldown_min_seconds < 0:
            raise AppError("--auth-cooldown-min-seconds cannot be negative.")
        if args.auth_cooldown_max_seconds < args.auth_cooldown_min_seconds:
            raise AppError(
                "--auth-cooldown-max-seconds must be greater than or equal "
                "to --auth-cooldown-min-seconds."
            )
        if args.auth_retries_per_track < 0:
            raise AppError("--auth-retries-per-track cannot be negative.")
        if args.min_score is not None and not 0 <= args.min_score <= 100:
            raise AppError("--min-score must be between 0 and 100.")
        if not args.dry_run and not args.status:
            check_dependencies()
        with connect_db(args.db) as connection:
            if args.status:
                print_status(connection)
                return 0

            if not args.dry_run:
                wait_for_youtube_cooldown(connection)

            if not args.dry_run:
                connection.execute(
                    """
                    UPDATE tracks
                    SET download_status = 'not_downloaded'
                    WHERE download_status = 'downloading'
                    """
                )
                connection.commit()

            print_status(connection)
            tracks, assessments = select_download_tracks(
                connection,
                min_score=args.min_score,
                retry_errors=args.retry_errors,
                redownload=args.redownload,
                batch_size=args.batch_size,
                process_all=args.all,
                retry_due=args.retry_due,
                errors_only=args.errors_only,
                spotify_ids=args.spotify_id,
            )
            if not tracks:
                counts = print_status(connection)
                if counts["errors"] and not args.retry_errors:
                    print(
                        "No pending tracks remain. Run with --retry-errors to "
                        "try failed tracks again."
                    )
                else:
                    print("No approved tracks are waiting to download.")
                return 0
            print(
                f"Processing this batch: {len(tracks):,} track(s). "
                "The checkpoint is saved after every track."
            )
            consecutive_auth_errors = 0
            auth_retries: dict[str, int] = {}
            for index, track in enumerate(tracks, start=1):
                destination = final_path(args.output, track)
                print(
                    f"[{index:,}/{len(tracks):,}] "
                    f"{track['primary_artist']} - {track['title']}"
                )
                print(f"  {track['youtube_url']}")
                print(f"  -> {destination}")
                if args.min_score is not None:
                    score, _notes, reason = assessments[track["spotify_id"]]
                    print(f"  score {score:.1f}: {reason}")
                if args.dry_run:
                    continue
                delay = random.uniform(
                    args.sleep_min_seconds,
                    args.sleep_max_seconds,
                )
                if delay > 0:
                    print(f"  Waiting {delay:.1f}s before contacting YouTube.")
                    time.sleep(delay)
                attempt_time = utc_now()
                downloaded_runtime: float | None = None
                downloaded_difference: float | None = None
                if args.min_score is not None:
                    score, score_notes, reason = assessments[track["spotify_id"]]
                    connection.execute(
                        """
                        UPDATE tracks
                        SET youtube_score = ?,
                            youtube_score_notes = ?,
                            youtube_score_version = ?,
                            match_status = CASE
                                WHEN match_status = 'suggested'
                                THEN 'approved_score'
                                ELSE match_status
                            END,
                            reviewed_at = CASE
                                WHEN match_status = 'suggested' THEN ?
                                ELSE reviewed_at
                            END
                        WHERE spotify_id = ?
                        """,
                        (
                            score,
                            f"{score_notes}; --min-score approval: {reason}",
                            SCORING_VERSION,
                            attempt_time,
                            track["spotify_id"],
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO match_assessments (
                            run_id, spotify_id, youtube_video_id, youtube_url,
                            youtube_title, youtube_channel,
                            youtube_duration_seconds, score, hard_reject,
                            automatic_approval_eligible, approval_threshold,
                            decision, reason, score_notes, scoring_version,
                            created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            uuid.uuid4().hex,
                            track["spotify_id"],
                            track["youtube_video_id"],
                            track["youtube_url"],
                            track["youtube_title"],
                            track["youtube_channel"],
                            track["youtube_duration_seconds"],
                            score,
                            args.min_score,
                            "approved_score",
                            reason,
                            score_notes,
                            SCORING_VERSION,
                            attempt_time,
                        ),
                    )
                connection.execute(
                    """
                    UPDATE tracks
                    SET download_status = 'downloading',
                        download_attempts = download_attempts + 1,
                        last_download_attempt_at = ?,
                        download_error = NULL,
                        download_error_code = NULL,
                        download_next_retry_at = NULL,
                        updated_at = ?
                    WHERE spotify_id = ?
                    """,
                    (attempt_time, attempt_time, track["spotify_id"]),
                )
                record_event(
                    connection,
                    stage="download",
                    event="attempt_started",
                    status="downloading",
                    spotify_id=track["spotify_id"],
                    details={
                        "youtube_url": track["youtube_url"],
                        "destination": str(destination),
                        "attempt": int(track["download_attempts"] or 0) + 1,
                    },
                    log_path=args.db.parent / "activity.jsonl",
                )
                connection.commit()
                try:
                    if file_has_spotify_id(destination, track["spotify_id"]):
                        print("  Existing tagged file found. Validating it.")
                        downloaded_runtime, downloaded_difference = (
                            validate_downloaded_runtime(destination, track)
                        )
                    else:
                        temporary = download_audio(
                            track,
                            args.output / ".partial",
                            use_po_token_provider=args.po_token_provider,
                        )
                        downloaded_runtime, downloaded_difference = (
                            validate_downloaded_runtime(temporary, track)
                        )
                        try:
                            artwork = fetch_artwork(
                                track, args.output / ".artwork"
                            )
                        except requests.RequestException as exc:
                            print(f"  Artwork warning: {exc}")
                            artwork = None
                        tag_mp3(temporary, track, artwork)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(temporary, destination)
                    completed_time = utc_now()
                    connection.execute(
                        """
                        UPDATE tracks
                        SET download_status = 'downloaded',
                            download_path = ?,
                            download_error = NULL,
                            download_error_code = NULL,
                            download_next_retry_at = NULL,
                            downloaded_duration_seconds = ?,
                            downloaded_duration_difference_seconds = ?,
                            downloaded_at = ?,
                            updated_at = ?
                        WHERE spotify_id = ?
                        """,
                        (
                            str(destination),
                            downloaded_runtime,
                            downloaded_difference,
                            completed_time,
                            completed_time,
                            track["spotify_id"],
                        ),
                    )
                    record_event(
                        connection,
                        stage="download",
                        event="attempt_completed",
                        status="downloaded",
                        spotify_id=track["spotify_id"],
                        details={
                            "path": str(destination),
                            "runtime_seconds": downloaded_runtime,
                            "runtime_difference_seconds": downloaded_difference,
                        },
                        log_path=args.db.parent / "activity.jsonl",
                    )
                    connection.commit()
                    consecutive_auth_errors = 0
                    auth_retries.pop(track["spotify_id"], None)
                except Exception as exc:
                    if isinstance(exc, DownloadRuntimeMismatch):
                        downloaded_runtime = exc.actual_seconds
                        downloaded_difference = exc.difference_seconds
                    message = str(exc)[:1000]
                    print(f"  Error: {message}")
                    attempts = int(
                        connection.execute(
                            "SELECT download_attempts FROM tracks "
                            "WHERE spotify_id = ?",
                            (track["spotify_id"],),
                        ).fetchone()[0]
                    )
                    code, retryable = classify_failure(message, "download")
                    if code == "download_authentication":
                        consecutive_auth_errors += 1
                    else:
                        consecutive_auth_errors = 0
                    retry_at = next_retry_time(attempts, code, retryable)
                    cooldown_seconds = 0.0
                    if (
                        code == "download_authentication"
                        and args.auth_cooldown_min_seconds > 0
                        and consecutive_auth_errors
                        < args.max_consecutive_auth_errors
                    ):
                        cooldown_seconds = min(
                            args.auth_cooldown_max_seconds,
                            args.auth_cooldown_min_seconds
                            * (2 ** max(0, consecutive_auth_errors - 1)),
                        )
                        retry_at = set_youtube_cooldown(
                            connection, cooldown_seconds
                        )
                    connection.execute(
                        """
                        UPDATE tracks
                        SET download_status = 'error',
                            download_error = ?,
                            download_error_code = ?,
                            download_next_retry_at = ?,
                            downloaded_duration_seconds = ?,
                            downloaded_duration_difference_seconds = ?,
                            updated_at = ?
                        WHERE spotify_id = ?
                        """,
                        (
                            message,
                            code,
                            retry_at,
                            downloaded_runtime,
                            downloaded_difference,
                            utc_now(),
                            track["spotify_id"],
                        ),
                    )
                    record_event(
                        connection,
                        stage="download",
                        event="attempt_failed",
                        status=code,
                        spotify_id=track["spotify_id"],
                        details={
                            "error": message,
                            "retryable": retryable,
                            "next_retry_at": retry_at,
                        },
                        log_path=args.db.parent / "activity.jsonl",
                    )
                    connection.commit()
                    if (
                        consecutive_auth_errors
                        >= args.max_consecutive_auth_errors
                    ):
                        raise AppError(
                            "Stopping after "
                            f"{consecutive_auth_errors} consecutive YouTube "
                            "authentication/bot-check errors. Progress is "
                            "saved; fix authentication before retrying."
                        )
                    if cooldown_seconds > 0:
                        retry_number = auth_retries.get(track["spotify_id"], 0)
                        if retry_number < args.auth_retries_per_track:
                            auth_retries[track["spotify_id"]] = retry_number + 1
                            tracks.append(track)
                            print(
                                "  Requeued this track after the cooldown "
                                f"(authentication retry {retry_number + 1}/"
                                f"{args.auth_retries_per_track})."
                            )
                        else:
                            print(
                                "  This track reached its authentication "
                                "retry limit; it remains failed for a later run."
                            )
                        wait_for_youtube_cooldown(connection)
            if not args.dry_run:
                print_status(connection)
                remaining = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM tracks
                    WHERE (is_liked = 1 OR is_saved_album = 1)
                      AND user_deleted = 0
                      AND match_status LIKE 'approved_%'
                      AND download_status IN ('not_downloaded', 'downloading')
                    """
                ).fetchone()[0]
                if remaining:
                    print(
                        f"Batch complete. Run the same command again for the "
                        f"next {min(args.batch_size, remaining):,} track(s)."
                    )
                else:
                    print("Batch complete. No pending approved tracks remain.")
        return 0
    except (AppError, KeyboardInterrupt) as exc:
        print(f"\nError: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
