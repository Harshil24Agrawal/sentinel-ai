"""SQLite store: incidents, incident events, notifications and replay frames (audit trail)."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from app.models import Incident, Severity

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    service TEXT NOT NULL,
    group_id TEXT NOT NULL,
    state TEXT NOT NULL,
    current_severity INTEGER NOT NULL,
    max_severity INTEGER NOT NULL,
    opened_at REAL NOT NULL,
    resolved_at REAL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incidents_state ON incidents(state);
CREATE TABLE IF NOT EXISTS incident_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    severity INTEGER,
    snapshot TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_incident ON incident_events(incident_id);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    incident_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    t REAL NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_frames_group ON frames(group_id, t);
"""


class Store:
    """Thin wrapper over stdlib sqlite3. Everything written here is already redacted.

    One connection shared behind a lock: the pipeline writes from the event loop while REST
    handlers may read from worker threads.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self._lock:
            self._db.execute(sql, params)
            self._db.commit()

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute(sql, params).fetchall())

    # ---- incidents --------------------------------------------------------------------

    def upsert_incident(self, incident: Incident) -> None:
        self._execute(
            "INSERT INTO incidents (id, service, group_id, state, current_severity, max_severity,"
            " opened_at, resolved_at, data) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET service=excluded.service, group_id=excluded.group_id,"
            " state=excluded.state, current_severity=excluded.current_severity,"
            " max_severity=excluded.max_severity, opened_at=excluded.opened_at,"
            " resolved_at=excluded.resolved_at, data=excluded.data",
            (
                incident.id,
                incident.service,
                incident.group_id,
                incident.state.value,
                int(incident.current_severity),
                int(incident.max_severity),
                incident.opened_at,
                incident.resolved_at,
                incident.model_dump_json(),
            ),
        )

    def get_incident(self, incident_id: str) -> Incident | None:
        rows = self._query("SELECT data FROM incidents WHERE id = ?", (incident_id,))
        return Incident.model_validate_json(rows[0]["data"]) if rows else None

    def list_incidents(self, state: str | None = None) -> list[Incident]:
        """Newest first; ``state`` is ``OPEN``/``RESOLVED`` (case-insensitive) or None for all."""
        if state and state.lower() != "all":
            rows = self._query(
                "SELECT data FROM incidents WHERE state = ? ORDER BY opened_at DESC",
                (state.upper(),),
            )
        else:
            rows = self._query("SELECT data FROM incidents ORDER BY opened_at DESC")
        return [Incident.model_validate_json(r["data"]) for r in rows]

    # ---- audit trail ------------------------------------------------------------------

    def add_event(
        self,
        incident_id: str,
        ts: float,
        kind: str,
        severity: Severity | None,
        snapshot: Incident | dict[str, Any],
    ) -> None:
        body = (
            snapshot.model_dump_json() if isinstance(snapshot, Incident) else json.dumps(snapshot)
        )
        self._execute(
            "INSERT INTO incident_events (incident_id, ts, kind, severity, snapshot)"
            " VALUES (?,?,?,?,?)",
            (incident_id, ts, kind, None if severity is None else int(severity), body),
        )

    def events(self, incident_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT ts, kind, severity, snapshot FROM incident_events"
            " WHERE incident_id = ? ORDER BY id",
            (incident_id,),
        )
        return [
            {
                "ts": r["ts"],
                "kind": r["kind"],
                "severity": None if r["severity"] is None else Severity(r["severity"]),
                "snapshot": json.loads(r["snapshot"]),
            }
            for r in rows
        ]

    def add_notification(
        self, ts: float, incident_id: str, channel: str, status: str, payload: dict[str, Any]
    ) -> None:
        self._execute(
            "INSERT INTO notifications (ts, incident_id, channel, status, payload)"
            " VALUES (?,?,?,?,?)",
            (ts, incident_id, channel, status, json.dumps(payload)),
        )

    def notifications(self, incident_id: str | None = None) -> list[dict[str, Any]]:
        if incident_id is None:
            rows = self._query("SELECT * FROM notifications ORDER BY id")
        else:
            rows = self._query(
                "SELECT * FROM notifications WHERE incident_id = ? ORDER BY id", (incident_id,)
            )
        return [
            {
                "ts": r["ts"],
                "incident_id": r["incident_id"],
                "channel": r["channel"],
                "status": r["status"],
                "payload": json.loads(r["payload"]),
            }
            for r in rows
        ]

    # ---- replay -----------------------------------------------------------------------

    def add_frame(self, group_id: str, t: float, data: dict[str, Any]) -> None:
        self._execute(
            "INSERT INTO frames (group_id, t, data) VALUES (?,?,?)",
            (group_id, t, json.dumps(data)),
        )

    def frames(self, group_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT t, data FROM frames WHERE group_id = ? ORDER BY t, id", (group_id,)
        )
        return [{"t": r["t"], "data": json.loads(r["data"])} for r in rows]
