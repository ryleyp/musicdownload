from __future__ import annotations

from datetime import datetime, timedelta, timezone


def classify_failure(message: str, stage: str) -> tuple[str, bool]:
    text = (message or "").casefold()
    rules = (
        ("runtime", ("runtime mismatch", "duration mismatch")),
        ("rate_limited", ("429", "rate limit", "too many requests")),
        ("authentication", ("sign in", "cookies", "bot", "login")),
        ("unavailable", ("unavailable", "private video", "removed", "404")),
        ("forbidden", ("403", "forbidden")),
        ("timeout", ("timed out", "timeout")),
        ("network", ("network", "connection", "dns", "temporary failure")),
        ("dependency", ("not found", "not installed", "ffmpeg")),
        ("permission", ("not authorized", "permission", "-1743")),
        ("tagging", ("id3", "mutagen", "tag")),
        ("artwork", ("artwork", "image")),
    )
    for code, phrases in rules:
        if any(phrase in text for phrase in phrases):
            retryable = code not in {"runtime", "unavailable", "dependency", "permission"}
            return f"{stage}_{code}", retryable
    return f"{stage}_unknown", True


def retry_delay_seconds(attempts: int, code: str) -> int:
    base = 900 if "rate_limited" in code else 60
    return min(86400, base * (2 ** max(0, min(attempts - 1, 10))))


def next_retry_time(attempts: int, code: str, retryable: bool) -> str | None:
    if not retryable:
        return None
    moment = datetime.now(timezone.utc) + timedelta(
        seconds=retry_delay_seconds(attempts, code)
    )
    return moment.replace(microsecond=0).isoformat()
