from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from common import utc_now


def record_event(
    connection: sqlite3.Connection,
    *,
    stage: str,
    event: str,
    status: str,
    spotify_id: str | None = None,
    details: dict[str, Any] | None = None,
    log_path: Path | None = None,
) -> None:
    timestamp = utc_now()
    payload = details or {}
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    connection.execute(
        """
        INSERT INTO workflow_events (
            spotify_id, stage, event, status, details_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (spotify_id, stage, event, status, serialized, timestamp),
    )
    if log_path is None:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "timestamp": timestamp,
        "stage": stage,
        "event": event,
        "status": status,
        "spotify_id": spotify_id,
        "details": payload,
    }
    encoded = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    descriptor = os.open(
        log_path,
        os.O_APPEND | os.O_CREAT | os.O_WRONLY,
        0o600,
    )
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
