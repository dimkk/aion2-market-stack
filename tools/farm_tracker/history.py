"""Durable local farm-session history and idempotent sync receipts."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import threading
from typing import Any

from .contracts import build_export


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS farm_sessions(
  session_id TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK(status IN ('running','stopped','interrupted')),
  started_at_ms INTEGER NOT NULL,
  stopped_at_ms INTEGER,
  last_kina_at_ms INTEGER NOT NULL,
  observed_direct_kina INTEGER NOT NULL,
  event_count INTEGER NOT NULL,
  activity_kind TEXT,
  activity_label TEXT,
  loot_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS farm_events(
  event_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES farm_sessions(session_id) ON DELETE CASCADE,
  observed_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS farm_events_session_idx ON farm_events(session_id,observed_at_ms,event_id);
CREATE TABLE IF NOT EXISTS farm_seen_events(
  event_id TEXT PRIMARY KEY,
  observed_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS farm_sync_receipts(
  payload_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL UNIQUE REFERENCES farm_sessions(session_id) ON DELETE CASCADE,
  receipt_id TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT
);
"""


class HistoryStore:
    def __init__(self, path: Path, now_ms: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                "UPDATE farm_sessions SET status='interrupted',stopped_at_ms=COALESCE(stopped_at_ms,?) WHERE status='running'",
                (now_ms,),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        loot = json.loads(row["loot_json"])
        activity = None
        if row["activity_kind"] is not None:
            activity = {"kind": row["activity_kind"], "label": row["activity_label"]}
        stopped = row["stopped_at_ms"]
        loot_value = sum(int(item["estimated_value"] or 0) for item in loot)
        direct = int(row["observed_direct_kina"])
        return {
            "session_id": row["session_id"],
            "status": row["status"],
            "activity": activity,
            "started_at_ms": int(row["started_at_ms"]),
            "stopped_at_ms": int(stopped) if stopped is not None else None,
            "last_kina_at_ms": int(row["last_kina_at_ms"]),
            "observed_direct_kina": direct,
            "estimated_loot_value": loot_value,
            "estimated_total_value": direct + loot_value,
            "event_count": int(row["event_count"]),
            "loot": loot,
        }

    def persist(self, session: dict[str, Any], event_id: str | None, observed_at_ms: int | None) -> bool:
        activity = session.get("activity") or {}
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if event_id is not None:
                exists = connection.execute(
                    "SELECT 1 FROM farm_seen_events WHERE event_id=?", (event_id,)
                ).fetchone()
                if exists is not None:
                    connection.rollback()
                    return False
            connection.execute(
                """
                INSERT INTO farm_sessions(session_id,status,started_at_ms,stopped_at_ms,last_kina_at_ms,
                  observed_direct_kina,event_count,activity_kind,activity_label,loot_json)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET
                  status=excluded.status,stopped_at_ms=excluded.stopped_at_ms,
                  last_kina_at_ms=excluded.last_kina_at_ms,
                  observed_direct_kina=excluded.observed_direct_kina,event_count=excluded.event_count,
                  activity_kind=excluded.activity_kind,activity_label=excluded.activity_label,
                  loot_json=excluded.loot_json
                """,
                (
                    session["session_id"], session["status"], session["started_at_ms"],
                    session.get("stopped_at_ms"), session["last_kina_at_ms"],
                    session["observed_direct_kina"], session["event_count"],
                    activity.get("kind"), activity.get("label"),
                    json.dumps(session["loot"], ensure_ascii=False, separators=(",", ":")),
                ),
            )
            if event_id is not None:
                connection.execute(
                    "INSERT INTO farm_seen_events(event_id,observed_at_ms) VALUES(?,?)",
                    (event_id, observed_at_ms),
                )
                connection.execute(
                    "INSERT INTO farm_events(event_id,session_id,observed_at_ms) VALUES(?,?,?)",
                    (event_id, session["session_id"], observed_at_ms),
                )
            connection.commit()
            return True

    def has_event(self, event_id: str) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM farm_seen_events WHERE event_id=?", (event_id,)
            ).fetchone()
        return row is not None

    def remember_event(self, event_id: str, observed_at_ms: int) -> bool:
        with self._lock, self._connect() as connection:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO farm_seen_events(event_id,observed_at_ms) VALUES(?,?)",
                (event_id, observed_at_ms),
            ).rowcount
        return inserted == 1

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM farm_sessions ORDER BY started_at_ms DESC,session_id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row(row) for row in rows]

    def get(self, session_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM farm_sessions WHERE session_id=?", (session_id,)).fetchone()
        return self._row(row) if row is not None else None

    def export(self, session_id: str) -> dict[str, Any]:
        session = self.get(session_id)
        if session is None:
            raise ValueError("history_session_not_found")
        if session["status"] != "stopped" or session["stopped_at_ms"] is None:
            raise ValueError("history_session_not_exportable")
        return build_export(session)

    def sync_receipt(self, payload_id: str) -> str | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT receipt_id FROM farm_sync_receipts WHERE payload_id=?", (payload_id,)
            ).fetchone()
        return str(row[0]) if row is not None and row[0] is not None else None

    def record_sync(self, session_id: str, payload_id: str, receipt_id: str | None, error: str | None) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO farm_sync_receipts(payload_id,session_id,receipt_id,attempts,last_error)
                VALUES(?,?,?,?,?)
                ON CONFLICT(payload_id) DO UPDATE SET
                  receipt_id=excluded.receipt_id,attempts=farm_sync_receipts.attempts+1,last_error=excluded.last_error
                """,
                (payload_id, session_id, receipt_id, 1, error),
            )
