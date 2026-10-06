"""Command-level append-only audit log."""

import asyncio
import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any
from pydantic import BaseModel

from gateway.config import settings


class AuditEntry(BaseModel):
    timestamp: str
    request_id: str
    client_id: str
    device_id: str
    operation_id: str
    instruction_id: str
    params: dict[str, Any]
    plan_id: str | None
    status: str  # "accepted" | "rejected"
    error: str | None = None
    error_code: str | None = None


class AuditLog:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = str(db_path or settings.idempotency_db_path)
        self._lock = asyncio.Lock()
        self._entries_memory: list[dict[str, Any]] = []
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    instruction_id TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    plan_id TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    error_code TEXT
                )
                """
            )
            conn.commit()

    async def log(
        self,
        request_id: str,
        client_id: str,
        device_id: str,
        operation_id: str,
        instruction_id: str,
        params: dict[str, Any],
        plan_id: str | None,
        status: str,
        error: str | None = None,
        error_code: str | None = None,
    ) -> None:
        """
        Record a command execution in append-only audit log.
        CRITICAL: Never log secrets, bearer tokens, or approval tokens!
        Only log sanitized parameters and token plan_id.
        """
        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
        # Clean params to ensure no accidental token keys are logged
        clean_params = {
            k: v
            for k, v in params.items()
            if "token" not in k.lower() and "secret" not in k.lower() and "auth" not in k.lower()
        }

        entry = AuditEntry(
            timestamp=now_str,
            request_id=request_id,
            client_id=client_id,
            device_id=device_id,
            operation_id=operation_id,
            instruction_id=instruction_id,
            params=clean_params,
            plan_id=plan_id,
            status=status,
            error=error,
            error_code=error_code,
        )

        entry_dict = entry.model_dump()
        async with self._lock:
            self._entries_memory.append(entry_dict)
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO audit_logs (
                        timestamp, request_id, client_id, device_id, operation_id,
                        instruction_id, params_json, plan_id, status, error, error_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now_str,
                        request_id,
                        client_id,
                        device_id,
                        operation_id,
                        instruction_id,
                        json.dumps(clean_params, ensure_ascii=False),
                        plan_id,
                        status,
                        error,
                        error_code,
                    ),
                )
                conn.commit()

    async def get_entries(self, limit: int = 100) -> list[dict[str, Any]]:
        """Retrieve recent audit log entries."""
        async with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT timestamp, request_id, client_id, device_id, operation_id,
                           instruction_id, params_json, plan_id, status, error, error_code
                    FROM audit_logs ORDER BY id DESC LIMIT ?
                    """,
                    (limit,),
                )
                rows = cursor.fetchall()
                results = []
                for r in rows:
                    results.append(
                        {
                            "timestamp": r["timestamp"],
                            "request_id": r["request_id"],
                            "client_id": r["client_id"],
                            "device_id": r["device_id"],
                            "operation_id": r["operation_id"],
                            "instruction_id": r["instruction_id"],
                            "params": json.loads(r["params_json"]),
                            "plan_id": r["plan_id"],
                            "status": r["status"],
                            "error": r["error"],
                            "error_code": r["error_code"],
                        }
                    )
                return results

    async def clear(self) -> None:
        """Clear audit records (used in tests and reset)."""
        async with self._lock:
            self._entries_memory.clear()
            with self._get_connection() as conn:
                conn.execute("DELETE FROM audit_logs")
                conn.commit()


audit_log = AuditLog()
