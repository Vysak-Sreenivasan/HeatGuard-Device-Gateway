"""Idempotency store backed by SQLite with 24-hour TTL."""

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from gateway.config import settings


class IdempotencyStore:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = str(db_path or settings.idempotency_db_path)
        self._lock = asyncio.Lock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS idempotency_records (
                    key TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_expires_at ON idempotency_records(expires_at)"
            )
            conn.commit()

    async def get(self, key: str) -> dict[str, Any] | None:
        """Retrieve stored result if key exists and has not expired."""
        now = int(time.time())
        async with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT result_json, expires_at FROM idempotency_records WHERE key = ?",
                    (key,),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                if row["expires_at"] < now:
                    # Expired, clean up
                    cursor.execute("DELETE FROM idempotency_records WHERE key = ?", (key,))
                    conn.commit()
                    return None
                return json.loads(row["result_json"])

    async def set(
        self, key: str, result: dict[str, Any], ttl_seconds: int = 86400
    ) -> None:
        """Store result with TTL (default 24 hours)."""
        now = int(time.time())
        expires_at = now + ttl_seconds
        result_json = json.dumps(result, ensure_ascii=False)
        async with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO idempotency_records (key, result_json, created_at, expires_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        result_json = excluded.result_json,
                        created_at = excluded.created_at,
                        expires_at = excluded.expires_at
                    """,
                    (key, result_json, now, expires_at),
                )
                conn.commit()

    async def clear(self) -> None:
        """Clear all stored records (used in tests and resets)."""
        async with self._lock:
            with self._get_connection() as conn:
                conn.execute("DELETE FROM idempotency_records")
                conn.commit()


idempotency_store = IdempotencyStore()
