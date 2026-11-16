"""Google Chat records, isolated from integer hub message identifiers.

The first build persists events but never delivers them. Later builds can add
admission and delivery without changing the deployed hub tables or cursor.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GoogleMessage:
    id: str
    agent: str
    source_name: str
    space: str
    event_time: str
    state: str
    payload: dict


class GoogleStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._open() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS google_messages (
                    id INTEGER PRIMARY KEY,
                    agent TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    space TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'received',
                    payload TEXT NOT NULL,
                    UNIQUE(agent, source_name)
                );
                CREATE INDEX IF NOT EXISTS google_messages_state
                    ON google_messages(state);
                CREATE TABLE IF NOT EXISTS google_outbound (
                    id INTEGER PRIMARY KEY,
                    message_id INTEGER NOT NULL REFERENCES google_messages(id),
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    request_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    sent_at TEXT
                );
            """)

    def _open(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _message(row: sqlite3.Row) -> GoogleMessage:
        return GoogleMessage(f"gc-{row['id']}", row["agent"], row["source_name"],
                             row["space"], row["event_time"], row["state"],
                             json.loads(row["payload"]))

    def receive(self, agent: str, source_name: str, space: str,
                event_time: str, payload: dict) -> tuple[GoogleMessage, bool]:
        """Commit before acknowledging Pub/Sub; duplicate source names are inert."""
        if not agent or not source_name or not space or not event_time:
            raise ValueError("Google event needs agent, message name, space and event time")
        encoded = json.dumps(payload, sort_keys=True)
        with self._open() as db:
            cur = db.execute("""INSERT OR IGNORE INTO google_messages
                (agent,source_name,space,event_time,payload) VALUES (?,?,?,?,?)""",
                (agent, source_name, space, event_time, encoded))
            row = db.execute("SELECT * FROM google_messages WHERE agent=? AND source_name=?",
                             (agent, source_name)).fetchone()
            return self._message(row), cur.rowcount == 1

    def get(self, local_id: str) -> GoogleMessage | None:
        if not re.fullmatch(r"gc-[0-9]+", local_id):
            return None
        with self._open() as db:
            row = db.execute("SELECT * FROM google_messages WHERE id=?", (int(local_id[3:]),)).fetchone()
            return self._message(row) if row else None

    def list(self, agent: str | None = None) -> list[GoogleMessage]:
        query = "SELECT * FROM google_messages"
        args = ()
        if agent:
            query += " WHERE agent=?"
            args = (agent,)
        query += " ORDER BY id DESC"
        with self._open() as db:
            return [self._message(row) for row in db.execute(query, args)]
