from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from app.allocation import Entry, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    session          INTEGER NOT NULL,
    matric           TEXT    NOT NULL,
    name             TEXT    DEFAULT '',
    confirmed        INTEGER DEFAULT 0,
    allocated_session INTEGER,
    status           TEXT    DEFAULT 'waiting',
    table_no         INTEGER,
    group_no         INTEGER,
    subject_id       TEXT    DEFAULT '',
    arrival_seq      INTEGER DEFAULT 0,
    arrival_time     TEXT    DEFAULT '',
    method           TEXT    DEFAULT 'manual',
    in_roster        INTEGER DEFAULT 1,
    confidence       REAL,
    note             TEXT    DEFAULT '',
    PRIMARY KEY (session, matric)
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    session  INTEGER,
    at       TEXT,
    action   TEXT,
    payload  TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    session INTEGER,
    key     TEXT,
    value   TEXT,
    PRIMARY KEY (session, key)
);
CREATE TABLE IF NOT EXISTS sheet_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session    INTEGER,
    matric     TEXT,
    payload    TEXT,
    created_at TEXT,
    attempts   INTEGER DEFAULT 0,
    sent_at    TEXT,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session);
CREATE INDEX IF NOT EXISTS idx_queue_pending  ON sheet_queue(sent_at);
"""

_ENTRY_COLUMNS = ("matric", "name", "confirmed", "allocated_session", "status",
                  "table_no", "group_no", "subject_id", "arrival_seq",
                  "arrival_time", "method", "in_roster", "confidence", "note")


class Store:

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def save_entry(self, session: int, e: Entry) -> None:
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO entries (session, matric, name, confirmed, "
                "allocated_session, status, table_no, group_no, subject_id, arrival_seq, "
                "arrival_time, method, in_roster, confidence, note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (session, e.matric, e.name, int(e.confirmed), e.allocated_session,
                 e.status.value, e.table, e.group, e.subject_id, e.arrival_seq,
                 e.arrival_time, e.method, int(e.in_roster), e.confidence, e.note))
            self.db.commit()

    def save_entries(self, session: int, entries: list[Entry]) -> None:
        with self._lock:
            for e in entries:
                self.save_entry(session, e)

    def delete_entry(self, session: int, matric: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM entries WHERE session=? AND matric=?",
                            (session, matric))
            self.db.commit()

    def load_entries(self, session: int) -> list[Entry]:
        with self._lock:
            rows = list(self.db.execute(
                "SELECT * FROM entries WHERE session=? ORDER BY arrival_seq",
                (session,)))
        return [Entry(matric=r["matric"], name=r["name"], confirmed=bool(r["confirmed"]),
                      allocated_session=r["allocated_session"],
                      status=Status(r["status"]), table=r["table_no"],
                      group=r["group_no"], subject_id=r["subject_id"] or "",
                      arrival_seq=r["arrival_seq"], arrival_time=r["arrival_time"] or "",
                      method=r["method"] or "manual", in_roster=bool(r["in_roster"]),
                      confidence=r["confidence"], note=r["note"] or "")
                for r in rows]

    def clear_session(self, session: int) -> None:
        with self._lock:
            for table in ("entries", "events", "meta", "sheet_queue"):
                self.db.execute(f"DELETE FROM {table} WHERE session=?", (session,))
            self.db.commit()

    def log_events(self, session: int, events: list[dict]) -> None:
        with self._lock:
            self.db.executemany(
                "INSERT INTO events (session, at, action, payload) VALUES (?,?,?,?)",
                [(session, ev.get("at", ""), ev.get("action", ""),
                  json.dumps({k: v for k, v in ev.items() if k not in ("at", "action")}))
                 for ev in events])
            self.db.commit()

    def load_events(self, session: int) -> list[dict]:
        with self._lock:
            rows = list(self.db.execute(
                "SELECT at, action, payload FROM events WHERE session=? ORDER BY id",
                (session,)))
        return [{"at": r["at"], "action": r["action"], **json.loads(r["payload"] or "{}")}
                for r in rows]

    def set_meta(self, session: int, key: str, value) -> None:
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO meta (session, key, value) VALUES (?,?,?)",
                (session, key, json.dumps(value)))
            self.db.commit()

    def get_meta(self, session: int, key: str, default=None):
        with self._lock:
            row = self.db.execute("SELECT value FROM meta WHERE session=? AND key=?",
                                  (session, key)).fetchone()
        return json.loads(row["value"]) if row else default

    def enqueue_sheet(self, session: int, matric: str, payload: dict, at: str) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO sheet_queue (session, matric, payload, created_at) "
                "VALUES (?,?,?,?)", (session, matric, json.dumps(payload), at))
            self.db.commit()

    def pending_sheet_jobs(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.db.execute(
                "SELECT * FROM sheet_queue WHERE sent_at IS NULL ORDER BY id LIMIT ?",
                (limit,)))

    def mark_sheet_sent(self, job_id: int, at: str) -> None:
        with self._lock:
            self.db.execute("UPDATE sheet_queue SET sent_at=? WHERE id=?",
                            (at, job_id))
            self.db.commit()

    def mark_sheet_failed(self, job_id: int, error: str) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE sheet_queue SET attempts=attempts+1, last_error=? WHERE id=?",
                (error[:500], job_id))
            self.db.commit()

    def reset_attempts(self) -> int:
        with self._lock:
            cur = self.db.execute(
                "UPDATE sheet_queue SET attempts=0, last_error=NULL WHERE sent_at IS NULL")
            self.db.commit()
            return cur.rowcount

    def queue_depth(self) -> int:
        with self._lock:
            row = self.db.execute(
                "SELECT COUNT(*) AS n FROM sheet_queue WHERE sent_at IS NULL").fetchone()
        return int(row["n"])
