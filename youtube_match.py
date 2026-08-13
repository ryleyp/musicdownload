#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import re
import sqlite3
import unicodedata
import uuid
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from common import (
    AppError,
    DEFAULT_CSV_PATH,
    DEFAULT_DB_PATH,
    DEFAULT_XLSX_PATH,
    connect_db,
    dependency_help,
    export_catalog,
    utc_now,
)
from activity_log import record_event
from failures import classify_failure, next_retry_time
from matching_rules import (
    ALTERED_VERSION_TERMS,
    ALTERED_VERSION_PATTERNS,
    ARTIST_MATCH_POINTS,
    ARTIST_MISMATCH_PENALTY,
    ALLOWED_VIDEO_PHRASES,
    AUTO_APPROVAL_MAX_DURATION_DIFFERENCE_SECONDS,
    DEFAULT_AUTO_APPROVAL_SCORE,
    DEFAULT_CANDIDATE_HYDRATION_COUNT,
    DURATION_HARD_REJECT_SECONDS,
    DURATION_MISSING_PENALTY,
    DURATION_SCORE_BANDS,
    HARD_REJECT_PHRASES,
    LOW_TITLE_COVERAGE_PENALTY,
    MIN_TITLE_SIMILARITY,
    MIN_TITLE_TOKEN_COVERAGE,
    REQUIRED_VERSION_TERMS,
    SCORING_VERSION,
    SOURCE_BONUSES,
    TITLE_SIMILARITY_POINTS,
    TITLE_TOKEN_COVERAGE_POINTS,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find and rank clean YouTube sources for Spotify tracks."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV_PATH)
    parser.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX_PATH)
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--spotify-id",
        action="append",
        default=[],
        metavar="ID",
        help=(
            "Match one Spotify track ID regardless of its current status. "
            "Repeat this option to target several tracks."
        ),
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Search tracks that already have candidates again.",
    )
    parser.add_argument(
        "--auto-approve",
        type=float,
        metavar="SCORE",
        help=(
            "Approve matches at or above SCORE only when all safety gates pass "
            f"(recommended: {DEFAULT_AUTO_APPROVAL_SCORE:g})."
        ),
    )
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="Retry tracks whose previous YouTube searches failed.",
    )
    parser.add_argument(
        "--retry-unmatched",
        action="store_true",
        help="Search tracks that previously returned no candidates.",
    )
    parser.add_argument(
        "--retry-due",
        action="store_true",
        help="Retry only failed searches whose scheduled retry time has arrived.",
    )
    parser.add_argument(
        "--hydrate-count",
        type=int,
        default=DEFAULT_CANDIDATE_HYDRATION_COUNT,
        help=(
            "Fetch full metadata for this many shortlisted results per track. "
            f"Default: {DEFAULT_CANDIDATE_HYDRATION_COUNT}."
        ),
    )
    return parser.parse_args()


def normalize(value: str | None) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(character for character in value if not unicodedata.combining(character))
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"\bofficial\b", " ", value)
    value = re.sub(r"\baudio\b", " ", value)
    value = re.sub(r"\blyric(?:s)?\b", " ", value)
    value = re.sub(r"\bvideo\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def raw_normalize(value: str | None) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(character for character in value if not unicodedata.combining(character))
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def core_title(value: str | None) -> str:
    """Remove version descriptors so they cannot masquerade as song identity."""
    text = normalize(value)
    normalized_terms = sorted(
        {normalize(term) for term in ALTERED_VERSION_TERMS},
        key=len,
        reverse=True,
    )
    for term in normalized_terms:
        text = re.sub(rf"\b{re.escape(term)}\b", " ", text)
    text = re.sub(r"\b(?:19|20)\d{2}\b", " ", text)
    text = re.sub(r"\bversion\b", " ", text)
    return " ".join(text.split())


def contains_phrase(value: str, phrase: str) -> bool:
    return f" {phrase} " in f" {value} "


def track_value(
    track: sqlite3.Row | dict[str, Any], key: str, default: Any = None
) -> Any:
    try:
        value = track[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def version_analysis(
    track: sqlite3.Row | dict[str, Any], candidate_title: str | None
) -> tuple[list[str], list[str]]:
    spotify_parts = [
        str(track_value(track, "title", "")),
        str(track_value(track, "album", "")),
        str(track_value(track, "artists", "")),
    ]
    if bool(track_value(track, "explicit", 0)):
        spotify_parts.append("explicit")
    spotify_version_text = raw_normalize(" ".join(spotify_parts))
    youtube_version_text = raw_normalize(candidate_title)
    unexpected = sorted(
        term
        for term in ALTERED_VERSION_TERMS
        if contains_phrase(youtube_version_text, term)
        and not contains_phrase(spotify_version_text, term)
    )
    confirmed = sorted(
        term
        for term in ALTERED_VERSION_TERMS
        if contains_phrase(youtube_version_text, term)
        and contains_phrase(spotify_version_text, term)
    )
    return unexpected, confirmed


def missing_required_versions(
    track: sqlite3.Row | dict[str, Any], candidate: dict[str, Any]
) -> list[str]:
    spotify_text = raw_normalize(str(track_value(track, "title", "")))
    youtube_text = raw_normalize(
        " ".join(
            str(candidate.get(key) or "")
            for key in ("title", "track", "album")
        )
    )
    return sorted(
        term
        for term in REQUIRED_VERSION_TERMS
        if contains_phrase(spotify_text, term)
        and not contains_phrase(youtube_text, term)
    )


def featured_artist_analysis(
    track: sqlite3.Row | dict[str, Any], candidate_title: str | None
) -> tuple[str | None, bool]:
    youtube_text = raw_normalize(candidate_title)
    match = re.search(r"\b(?:feat|ft|featuring)\s+(.+)$", youtube_text)
    if not match:
        return None, False
    featured = re.split(
        r"\b(?:official|audio|video|lyric|lyrics|remaster|remastered)\b",
        match.group(1),
        maxsplit=1,
    )[0].strip()
    tokens = {
        token
        for token in featured.split()
        if len(token) > 1 and token not in {"and", "with"}
    }
    spotify_artists = raw_normalize(track_value(track, "artists", ""))
    if not tokens:
        return featured or None, False
    coverage = sum(token in spotify_artists.split() for token in tokens) / len(tokens)
    return featured, coverage < 0.75


def duration_difference_seconds(
    track: sqlite3.Row | dict[str, Any], candidate: dict[str, Any]
) -> int | None:
    duration_ms = track["duration_ms"]
    candidate_seconds = candidate.get("duration")
    if duration_ms is None or not isinstance(candidate_seconds, (int, float)):
        return None
    if not math.isfinite(float(candidate_seconds)):
        return None
    return abs(round(int(duration_ms) / 1000) - round(float(candidate_seconds)))


def source_signals(
    track: sqlite3.Row | dict[str, Any], candidate: dict[str, Any]
) -> dict[str, bool]:
    raw_title = raw_normalize(candidate.get("title"))
    raw_channel = raw_normalize(
        candidate.get("channel") or candidate.get("uploader") or ""
    )
    target_artist = raw_normalize(track["primary_artist"])
    topic_name = re.sub(r"(?:^|\s)(?:-|–|—)?\s*topic$", "", raw_channel).strip()
    is_topic = raw_channel.endswith(" topic") and bool(topic_name)
    topic_artist_match = is_topic and (
        target_artist == topic_name
        or target_artist in topic_name
        or topic_name in target_artist
        or SequenceMatcher(None, target_artist, topic_name).ratio() >= 0.88
    )
    artist_in_title = bool(target_artist and target_artist in raw_title)
    compact_artist = target_artist.replace(" ", "")
    compact_channel = raw_channel.replace(" ", "")
    artist_in_channel = bool(
        target_artist
        and (
            target_artist in raw_channel
            or (raw_channel and raw_channel in target_artist)
            or (
                len(compact_artist) >= 4
                and compact_artist in compact_channel
            )
        )
    )
    verified = bool(
        candidate.get("channel_is_verified")
        or candidate.get("uploader_is_verified")
    )
    official_identity = compact_channel
    for suffix in ("official", "music", "vevo", "tv"):
        if official_identity.endswith(suffix):
            official_identity = official_identity[: -len(suffix)]
            break
    official_channel_match = bool(
        compact_artist
        and (
            compact_channel == compact_artist
            or official_identity == compact_artist
        )
    )
    description = raw_normalize(candidate.get("description"))
    return {
        "artist_confirmed": (
            artist_in_title or artist_in_channel or topic_artist_match
        ),
        "artist_in_channel": artist_in_channel or topic_artist_match,
        "official_channel_match": official_channel_match or topic_artist_match,
        "artist_topic": topic_artist_match,
        "official_audio": contains_phrase(raw_title, "official audio"),
        "official_lyric_video": (
            contains_phrase(raw_title, "official lyric video")
            or contains_phrase(raw_title, "official lyrics video")
        ),
        "audio": contains_phrase(raw_title, "audio"),
        "lyric_video": (
            contains_phrase(raw_title, "lyric video")
            or contains_phrase(raw_title, "lyrics video")
        ),
        "verified_artist": verified and (artist_in_channel or topic_artist_match),
        "provided_to_youtube": "provided to youtube by" in description,
    }


def source_tier(
    track: sqlite3.Row | dict[str, Any], candidate: dict[str, Any]
) -> int:
    signals = source_signals(track, candidate)
    if signals["artist_topic"] or (
        signals["provided_to_youtube"] and signals["artist_in_channel"]
    ):
        return 5
    if signals["verified_artist"]:
        return 4
    if signals["official_channel_match"] and (
        signals["official_audio"] or signals["official_lyric_video"]
    ):
        return 4
    if signals["official_channel_match"]:
        return 3
    if signals["artist_in_channel"]:
        return 2
    return 0


def candidate_score(
    track: sqlite3.Row | dict[str, Any], candidate: dict[str, Any]
) -> tuple[float, list[str], bool]:
    target_title = core_title(track["title"])
    target_artist = normalize(track["primary_artist"])
    candidate_title = core_title(candidate.get("title"))
    candidate_channel = normalize(
        candidate.get("channel") or candidate.get("uploader") or ""
    )
    raw_title = raw_normalize(candidate.get("title"))
    raw_channel = raw_normalize(
        candidate.get("channel") or candidate.get("uploader") or ""
    )

    combined_target = f"{target_artist} {target_title}".strip()
    title_similarity = max(
        SequenceMatcher(None, candidate_title, target_title).ratio(),
        SequenceMatcher(None, candidate_title, combined_target).ratio(),
    )
    score = title_similarity * TITLE_SIMILARITY_POINTS
    notes = [f"title {title_similarity:.0%}"]
    hard_reject = False

    title_tokens = {token for token in target_title.split() if len(token) > 1}
    candidate_tokens = set(candidate_title.split())
    token_coverage = (
        len(title_tokens & candidate_tokens) / len(title_tokens)
        if title_tokens
        else 0
    )
    score += token_coverage * TITLE_TOKEN_COVERAGE_POINTS
    notes.append(f"tokens {token_coverage:.0%}")

    signals = source_signals(track, candidate)
    if signals["artist_confirmed"]:
        score += ARTIST_MATCH_POINTS
        notes.append(f"artist confirmed +{ARTIST_MATCH_POINTS}")
    else:
        score -= ARTIST_MISMATCH_PENALTY
        notes.append(f"artist not confirmed -{ARTIST_MISMATCH_PENALTY}")

    if signals["artist_topic"]:
        bonus = SOURCE_BONUSES["artist_topic"]
        score += bonus
        notes.append(f"artist Topic channel +{bonus}")
    if signals["official_audio"]:
        bonus = SOURCE_BONUSES["official_audio"]
        score += bonus
        notes.append(f"official audio +{bonus}")
    elif signals["audio"]:
        bonus = SOURCE_BONUSES["audio"]
        score += bonus
        notes.append(f"audio +{bonus}")
    if signals["official_lyric_video"]:
        bonus = SOURCE_BONUSES["official_lyric_video"]
        score += bonus
        notes.append(f"official lyric video +{bonus}")
    elif signals["lyric_video"]:
        bonus = SOURCE_BONUSES["lyric_video"]
        score += bonus
        notes.append(f"lyric video +{bonus}")
    if signals["verified_artist"]:
        bonus = SOURCE_BONUSES["verified_artist"]
        score += bonus
        notes.append(f"verified artist channel +{bonus}")
    if (
        signals["official_audio"] or signals["official_lyric_video"]
    ) and not (
        signals["artist_topic"]
        or signals["verified_artist"]
        or signals["official_channel_match"]
    ):
        score -= 25
        notes.append("official label on unrelated channel -25")
    if signals["provided_to_youtube"]:
        score += 8
        notes.append("Provided to YouTube metadata +8")

    metadata_track = core_title(candidate.get("track"))
    if metadata_track:
        metadata_similarity = SequenceMatcher(
            None, metadata_track, target_title
        ).ratio()
        if metadata_similarity >= 0.9:
            score += 10
            notes.append("YouTube track metadata match +10")
        elif metadata_similarity < 0.5:
            score -= 45
            hard_reject = True
            notes.append("YouTube track metadata mismatch -45")
    metadata_artist = normalize(candidate.get("artist"))
    if metadata_artist:
        if target_artist in metadata_artist or metadata_artist in target_artist:
            score += 8
            notes.append("YouTube artist metadata match +8")
        else:
            score -= 30
            hard_reject = True
            notes.append("YouTube artist metadata mismatch -30")
    target_album = normalize(track_value(track, "album", ""))
    metadata_album = normalize(candidate.get("album"))
    if target_album and metadata_album:
        album_similarity = SequenceMatcher(
            None, metadata_album, target_album
        ).ratio()
        if album_similarity >= 0.85:
            score += 6
            notes.append("YouTube album metadata match +6")

    for phrase in HARD_REJECT_PHRASES:
        if contains_phrase(raw_title, phrase):
            score -= 90
            hard_reject = True
            notes.append(f"{phrase} -90")

    if contains_phrase(raw_title, "video") and not any(
        contains_phrase(raw_title, phrase) for phrase in ALLOWED_VIDEO_PHRASES
    ):
        score -= 90
        hard_reject = True
        notes.append("non-lyric video label -90")

    if candidate.get("live_status") in {"is_live", "is_upcoming"}:
        score -= 90
        hard_reject = True
        notes.append("live stream -90")
    if candidate.get("availability") in {
        "private",
        "premium_only",
        "subscriber_only",
        "needs_auth",
    }:
        score -= 90
        hard_reject = True
        notes.append(f"unavailable source ({candidate['availability']}) -90")

    unexpected_versions, confirmed_versions = version_analysis(
        track, candidate.get("title")
    )
    for term in unexpected_versions:
        penalty = 70
        score -= penalty
        notes.append(f"unexpected {term} -{penalty}")
        hard_reject = True
    for term in confirmed_versions:
        notes.append(f"Spotify confirms version: {term}")
    spotify_version_text = raw_normalize(
        " ".join(
            (
                str(track_value(track, "title", "")),
                str(track_value(track, "album", "")),
            )
        )
    )
    for label, pattern in ALTERED_VERSION_PATTERNS:
        if re.search(pattern, raw_title) and not re.search(
            pattern, spotify_version_text
        ):
            score -= 90
            hard_reject = True
            notes.append(f"unexpected {label} -90")
    for term in missing_required_versions(track, candidate):
        score -= 90
        hard_reject = True
        notes.append(f"YouTube does not confirm required {term} version -90")
    featured_artist, featured_mismatch = featured_artist_analysis(
        track, candidate.get("title")
    )
    if featured_artist:
        if featured_mismatch:
            score -= 70
            hard_reject = True
            notes.append(f"unexpected featured artist {featured_artist} -70")
        else:
            notes.append(f"Spotify confirms featured artist: {featured_artist}")

    difference = duration_difference_seconds(track, candidate)
    if difference is not None:
        adjustment = next(
            points
            for maximum, points in DURATION_SCORE_BANDS
            if difference <= maximum
        )
        score += adjustment
        sign = "+" if adjustment >= 0 else ""
        notes.append(f"duration difference {difference}s {sign}{adjustment}")
        if difference > DURATION_HARD_REJECT_SECONDS:
            hard_reject = True
    else:
        score -= DURATION_MISSING_PENALTY
        notes.append(f"duration unavailable -{DURATION_MISSING_PENALTY}")

    if token_coverage < MIN_TITLE_TOKEN_COVERAGE:
        score -= LOW_TITLE_COVERAGE_PENALTY
        notes.append(f"low title coverage -{LOW_TITLE_COVERAGE_PENALTY}")
    if title_similarity < MIN_TITLE_SIMILARITY:
        hard_reject = True
        notes.append("title similarity below safety minimum")
    return round(max(0.0, min(100.0, score)), 2), notes, hard_reject


def automatic_approval_eligible(
    track: sqlite3.Row | dict[str, Any],
    candidate: dict[str, Any],
    score: float,
    hard_reject: bool,
    threshold: float,
) -> tuple[bool, str]:
    if hard_reject:
        return False, "hard-rejected by matching policy"
    difference = duration_difference_seconds(track, candidate)
    if difference is None:
        return False, "YouTube runtime is unavailable"
    if difference > AUTO_APPROVAL_MAX_DURATION_DIFFERENCE_SECONDS:
        return (
            False,
            f"runtime differs by {difference}s "
            f"(maximum {AUTO_APPROVAL_MAX_DURATION_DIFFERENCE_SECONDS}s)",
        )
    signals = source_signals(track, candidate)
    trusted_source = (
        signals["artist_topic"]
        or signals["verified_artist"]
        or (
            signals["official_channel_match"]
            and (
                signals["official_audio"]
                or signals["official_lyric_video"]
            )
        )
    )
    if not signals["artist_confirmed"]:
        return False, "artist is not confirmed"
    if not trusted_source:
        return (
            False,
            "source is not an artist Topic channel, an artist-channel official "
            "audio/lyric upload, or a verified artist channel",
        )
    if score < threshold:
        return False, f"score {score:.1f} is below {threshold:.1f}"
    return True, f"score {score:.1f}, trusted source, runtime difference {difference}s"


def hydrate_candidate(
    ydl: Any, candidate: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    try:
        full = ydl.extract_info(candidate["webpage_url"], download=False)
    except Exception as exc:
        return candidate, str(exc).strip().replace("\n", " ")[:300]
    if not full:
        return candidate, "YouTube returned no full metadata"
    merged = dict(candidate)
    for key in (
        "id",
        "webpage_url",
        "title",
        "channel",
        "uploader",
        "duration",
        "channel_is_verified",
        "uploader_is_verified",
        "availability",
        "live_status",
        "description",
        "track",
        "artist",
        "album",
        "release_year",
    ):
        if full.get(key) is not None:
            merged[key] = full[key]
    return merged, None


def candidate_record(
    track: sqlite3.Row | dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    score, notes, hard_reject = candidate_score(track, candidate)
    tier = source_tier(track, candidate)
    notes.append(f"source tier {tier}")
    return {
        "youtube_video_id": str(candidate["id"]),
        "youtube_url": candidate["webpage_url"],
        "youtube_title": candidate.get("title") or "",
        "youtube_channel": candidate.get("channel")
        or candidate.get("uploader")
        or "",
        "youtube_duration_seconds": (
            int(candidate["duration"])
            if isinstance(candidate.get("duration"), (int, float))
            and math.isfinite(candidate["duration"])
            else None
        ),
        "youtube_channel_verified": bool(
            candidate.get("channel_is_verified")
            or candidate.get("uploader_is_verified")
        ),
        "score": score,
        "score_notes": "; ".join(notes),
        "hard_reject": 1 if hard_reject else 0,
        "source_tier": tier,
    }


def search_candidates(
    track: sqlite3.Row, hydrate_count: int = DEFAULT_CANDIDATE_HYDRATION_COUNT
) -> list[dict[str, Any]]:
    try:
        import yt_dlp
    except ImportError as exc:
        raise AppError(dependency_help("yt-dlp")) from exc

    artist = track["primary_artist"]
    title = track["title"]
    album = track["album"]
    queries = [
        f"ytsearch8:{artist} {title} official audio",
        f"ytsearch8:{artist} {title} official lyric video",
        f"ytsearch8:{artist} {title} {album} Topic",
    ]
    ydl_options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noplaylist": True,
        "socket_timeout": 20,
    }
    deduplicated: dict[str, dict[str, Any]] = {}
    successful_queries = 0
    search_errors: list[str] = []
    with yt_dlp.YoutubeDL(ydl_options) as ydl:
        for query in queries:
            try:
                result = ydl.extract_info(query, download=False)
            except Exception as exc:
                message = str(exc).strip().replace("\n", " ")[:240]
                search_errors.append(message)
                print(f"\n  Search warning: {message}")
                continue
            successful_queries += 1
            for entry in (result or {}).get("entries") or []:
                if not entry or not entry.get("id"):
                    continue
                video_id = str(entry["id"])
                if video_id in deduplicated:
                    continue
                source_url = entry.get("webpage_url") or entry.get("url") or ""
                if not str(source_url).startswith(("http://", "https://")):
                    source_url = f"https://www.youtube.com/watch?v={video_id}"
                entry["webpage_url"] = source_url
                deduplicated[video_id] = entry
        preliminary = sorted(
            deduplicated.values(),
            key=lambda item: (
                source_tier(track, item),
                candidate_score(track, item)[0],
            ),
            reverse=True,
        )
        for candidate in preliminary[:hydrate_count]:
            hydrated, error = hydrate_candidate(ydl, candidate)
            deduplicated[str(candidate["id"])] = hydrated
            if error:
                print(
                    "  Metadata warning for "
                    f"{candidate.get('title', candidate['id'])}: {error}"
                )
    if successful_queries == 0:
        detail = search_errors[-1] if search_errors else "no response from YouTube"
        raise AppError(
            "All YouTube searches failed. Progress was saved; use "
            f"--retry-errors later. Last error: {detail}"
        )

    ranked = []
    for candidate in deduplicated.values():
        ranked.append(candidate_record(track, candidate))
    ranked.sort(
        key=lambda item: (
            item["hard_reject"],
            -item["source_tier"],
            -item["score"],
        )
    )
    for rank, item in enumerate(ranked, start=1):
        item["rank"] = rank
    return ranked[:12]


def save_candidates(
    connection: sqlite3.Connection,
    track: sqlite3.Row,
    candidates: list[dict[str, Any]],
    auto_approve: float | None,
    log_path: Path | None = None,
) -> None:
    spotify_id = track["spotify_id"]
    run_id = uuid.uuid4().hex
    created_at = utc_now()
    connection.execute(
        "DELETE FROM youtube_candidates WHERE spotify_id = ?", (spotify_id,)
    )
    connection.executemany(
        """
        INSERT INTO youtube_candidates (
            spotify_id, youtube_video_id, rank, youtube_url, youtube_title,
            youtube_channel, youtube_channel_verified,
            youtube_duration_seconds, source_tier,
            score, score_notes, hard_reject
        ) VALUES (
            :spotify_id, :youtube_video_id, :rank, :youtube_url, :youtube_title,
            :youtube_channel, :youtube_channel_verified,
            :youtube_duration_seconds, :source_tier,
            :score, :score_notes, :hard_reject
        )
        """,
        [
            dict(
                item,
                spotify_id=spotify_id,
                source_tier=item.get("source_tier", 0),
            )
            for item in candidates
        ],
    )
    safe = [item for item in candidates if not item["hard_reject"]]
    best = safe[0] if safe else None
    if not best:
        connection.execute(
            """
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
                match_status = 'unmatched',
                match_error = NULL,
                match_error_code = NULL,
                match_next_retry_at = NULL
            WHERE spotify_id = ?
            """,
            (SCORING_VERSION, spotify_id),
        )
        for item in candidates:
            connection.execute(
                """
                INSERT INTO match_assessments (
                    run_id, spotify_id, youtube_video_id, youtube_url,
                    youtube_title, youtube_channel, youtube_duration_seconds,
                    score, hard_reject, automatic_approval_eligible,
                    approval_threshold, decision, reason, score_notes,
                    scoring_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    spotify_id,
                    item["youtube_video_id"],
                    item["youtube_url"],
                    item["youtube_title"],
                    item["youtube_channel"],
                    item["youtube_duration_seconds"],
                    item["score"],
                    item["hard_reject"],
                    auto_approve,
                    "rejected" if item["hard_reject"] else "unmatched",
                    "No candidate passed the safety policy",
                    item["score_notes"],
                    SCORING_VERSION,
                    created_at,
                ),
            )
        record_event(
            connection,
            stage="matching",
            event="search_completed",
            status="unmatched",
            spotify_id=spotify_id,
            details={"candidate_count": len(candidates), "run_id": run_id},
            log_path=log_path,
        )
        return
    status = "suggested"
    approval_threshold = (
        auto_approve
        if auto_approve is not None
        else DEFAULT_AUTO_APPROVAL_SCORE
    )
    selected_reason = "Best non-rejected candidate; manual review required"
    selected_eligible = False
    if auto_approve is not None:
        candidate = {
            "title": best["youtube_title"],
            "channel": best["youtube_channel"],
            "duration": best["youtube_duration_seconds"],
            "channel_is_verified": best["youtube_channel_verified"],
        }
        eligible, reason = automatic_approval_eligible(
            track, candidate, best["score"], bool(best["hard_reject"]), auto_approve
        )
        best["score_notes"] += f"; auto-approval: {reason}"
        selected_reason = reason
        selected_eligible = eligible
        if eligible:
            status = "approved_auto"
    previous = connection.execute(
        "SELECT youtube_url FROM tracks WHERE spotify_id = ?", (spotify_id,)
    ).fetchone()
    source_changed = bool(
        previous
        and previous["youtube_url"]
        and previous["youtube_url"] != best["youtube_url"]
    )
    connection.execute(
        """
        UPDATE tracks
        SET youtube_url = ?,
            youtube_video_id = ?,
            youtube_title = ?,
            youtube_channel = ?,
            youtube_channel_verified = ?,
            youtube_duration_seconds = ?,
            youtube_score = ?,
            youtube_score_notes = ?,
            youtube_score_version = ?,
            match_status = ?,
            match_error = NULL,
            match_error_code = NULL,
            match_next_retry_at = NULL,
            download_status = CASE
                WHEN ? THEN 'not_downloaded'
                ELSE download_status
            END,
            download_path = CASE WHEN ? THEN NULL ELSE download_path END,
            download_error = CASE WHEN ? THEN NULL ELSE download_error END,
            download_error_code = CASE
                WHEN ? THEN NULL ELSE download_error_code
            END,
            download_next_retry_at = CASE
                WHEN ? THEN NULL ELSE download_next_retry_at
            END,
            downloaded_at = CASE WHEN ? THEN NULL ELSE downloaded_at END,
            downloaded_duration_seconds = CASE
                WHEN ? THEN NULL ELSE downloaded_duration_seconds
            END,
            downloaded_duration_difference_seconds = CASE
                WHEN ? THEN NULL
                ELSE downloaded_duration_difference_seconds
            END,
            apple_music_status = CASE
                WHEN ? THEN 'not_checked'
                ELSE apple_music_status
            END,
            apple_music_preferred_id = CASE
                WHEN ? THEN NULL
                ELSE apple_music_preferred_id
            END,
            apple_music_replaced_ids = CASE
                WHEN ? THEN NULL
                ELSE apple_music_replaced_ids
            END,
            apple_music_updated_at = CASE
                WHEN ? THEN NULL
                ELSE apple_music_updated_at
            END
        WHERE spotify_id = ?
        """,
        (
            best["youtube_url"],
            best["youtube_video_id"],
            best["youtube_title"],
            best["youtube_channel"],
            best["youtube_channel_verified"],
            best["youtube_duration_seconds"],
            best["score"],
            best["score_notes"],
            SCORING_VERSION,
            status,
            source_changed,
            source_changed,
            source_changed,
            source_changed,
            source_changed,
            source_changed,
            source_changed,
            source_changed,
            source_changed,
            source_changed,
            source_changed,
            source_changed,
            spotify_id,
        ),
    )
    for item in candidates:
        is_selected = item["youtube_video_id"] == best["youtube_video_id"]
        candidate_data = {
            "title": item["youtube_title"],
            "channel": item["youtube_channel"],
            "duration": item["youtube_duration_seconds"],
            "channel_is_verified": item["youtube_channel_verified"],
        }
        eligible, reason = automatic_approval_eligible(
            track,
            candidate_data,
            item["score"],
            bool(item["hard_reject"]),
            approval_threshold,
        )
        decision = (
            status
            if is_selected
            else "rejected"
            if item["hard_reject"]
            else "not_selected"
        )
        connection.execute(
            """
            INSERT INTO match_assessments (
                run_id, spotify_id, youtube_video_id, youtube_url,
                youtube_title, youtube_channel, youtube_duration_seconds,
                score, hard_reject, automatic_approval_eligible,
                approval_threshold, decision, reason, score_notes,
                scoring_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                spotify_id,
                item["youtube_video_id"],
                item["youtube_url"],
                item["youtube_title"],
                item["youtube_channel"],
                item["youtube_duration_seconds"],
                item["score"],
                item["hard_reject"],
                1 if eligible else 0,
                approval_threshold,
                decision,
                selected_reason if is_selected else reason,
                item["score_notes"],
                SCORING_VERSION,
                created_at,
            ),
        )
    record_event(
        connection,
        stage="matching",
        event="candidate_selected",
        status=status,
        spotify_id=spotify_id,
        details={
            "run_id": run_id,
            "youtube_video_id": best["youtube_video_id"],
            "score": best["score"],
            "automatic_approval_eligible": selected_eligible,
            "reason": selected_reason,
        },
        log_path=log_path,
    )


def main() -> int:
    args = parse_args()
    try:
        with connect_db(args.db) as connection:
            if args.auto_approve is not None and not 0 <= args.auto_approve <= 100:
                raise AppError("--auto-approve must be between 0 and 100.")
            if args.hydrate_count < 0:
                raise AppError("--hydrate-count cannot be negative.")
            params: list[Any] = []
            if args.spotify_id:
                placeholders = ", ".join("?" for _ in args.spotify_id)
                where = (
                    "(is_liked = 1 OR is_saved_album = 1) "
                    f"AND spotify_id IN ({placeholders})"
                )
                params.extend(args.spotify_id)
            else:
                statuses = ["not_searched"]
                if args.retry_errors:
                    statuses.append("match_error")
                elif args.retry_due:
                    statuses.append("match_error")
                if args.retry_unmatched:
                    statuses.append("unmatched")
                where = "(is_liked = 1 OR is_saved_album = 1)"
                if not args.refresh:
                    placeholders = ", ".join("?" for _ in statuses)
                    where += f" AND match_status IN ({placeholders})"
                    params.extend(statuses)
                    if args.retry_due and not args.retry_errors:
                        where += (
                            " AND (match_status != 'match_error' "
                            "OR match_next_retry_at IS NULL "
                            "OR match_next_retry_at <= ?)"
                        )
                        params.append(utc_now())
            sql = f"""
                SELECT *
                FROM tracks
                WHERE {where}
                ORDER BY added_at DESC, primary_artist, title
            """
            if args.limit is not None:
                sql += " LIMIT ?"
                params.append(args.limit)
            tracks = connection.execute(sql, params).fetchall()
            if not tracks:
                print("No tracks need YouTube matching.")
            for index, track in enumerate(tracks, start=1):
                print(
                    f"[{index:,}/{len(tracks):,}] "
                    f"{track['primary_artist']} - {track['title']}"
                )
                attempt_time = utc_now()
                connection.execute(
                    """
                    UPDATE tracks
                    SET match_attempts = match_attempts + 1,
                        last_match_attempt_at = ?,
                        match_error = NULL
                    WHERE spotify_id = ?
                    """,
                    (attempt_time, track["spotify_id"]),
                )
                connection.commit()
                try:
                    candidates = search_candidates(track, args.hydrate_count)
                    save_candidates(
                        connection,
                        track,
                        candidates,
                        args.auto_approve,
                        args.db.parent / "activity.jsonl",
                    )
                    connection.commit()
                except AppError as exc:
                    attempts = int(track["match_attempts"] or 0) + 1
                    code, retryable = classify_failure(str(exc), "youtube")
                    retry_at = next_retry_time(attempts, code, retryable)
                    connection.execute(
                        """
                        UPDATE tracks
                        SET match_status = 'match_error',
                            match_error = ?,
                            match_error_code = ?,
                            match_next_retry_at = ?
                        WHERE spotify_id = ?
                        """,
                        (
                            str(exc)[:1000],
                            code,
                            retry_at,
                            track["spotify_id"],
                        ),
                    )
                    record_event(
                        connection,
                        stage="matching",
                        event="search_failed",
                        status=code,
                        spotify_id=track["spotify_id"],
                        details={
                            "error": str(exc)[:1000],
                            "retryable": retryable,
                            "next_retry_at": retry_at,
                        },
                        log_path=args.db.parent / "activity.jsonl",
                    )
                    connection.commit()
                    print(f"  Error: {exc}")
                    continue
                if candidates:
                    best = next(
                        (item for item in candidates if not item["hard_reject"]),
                        candidates[0],
                    )
                    print(
                        f"  {best['score']:.1f} (source tier "
                        f"{best['source_tier']}): "
                        f"{best['youtube_title']} [{best['youtube_channel']}]"
                    )
                else:
                    print("  No candidates found.")
            export_catalog(connection, args.csv, args.xlsx)
        print(f"Updated review workbook: {args.xlsx}")
        return 0
    except (AppError, KeyboardInterrupt) as exc:
        print(f"\nError: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
