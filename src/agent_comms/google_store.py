"""Google Chat records, isolated from integer hub message identifiers."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
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
    reason: str = ""
    attempts: int = 0


@dataclass(frozen=True)
class GoogleOutbound:
    id: int
    message_id: str
    kind: str
    payload: dict
    request_id: str
    state: str


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
                CREATE UNIQUE INDEX IF NOT EXISTS google_outbound_one_kind
                    ON google_outbound(message_id, kind);
            """)
            # 2.9.3 stores already have the table. Add only Google-owned columns;
            # the hub's messages and cursor are never migrated by this module.
            columns = {row[1] for row in db.execute("PRAGMA table_info(google_messages)")}
            if "reason" not in columns:
                db.execute("ALTER TABLE google_messages ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
            if "attempts" not in columns:
                db.execute("ALTER TABLE google_messages ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")

    def _open(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _message(row: sqlite3.Row) -> GoogleMessage:
        return GoogleMessage(f"gc-{row['id']}", row["agent"], row["source_name"],
                             row["space"], row["event_time"], row["state"],
                             json.loads(row["payload"]), row["reason"], row["attempts"])

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

    def transition(self, local_id: str, *, from_state: str, to_state: str,
                   reason: str = "", attempt: bool = False) -> GoogleMessage | None:
        """Compare-and-set a Google state so duplicate workers cannot both advance it."""
        if not re.fullmatch(r"gc-[0-9]+", local_id):
            raise ValueError("invalid Google message id")
        with self._open() as db:
            changed = db.execute("""UPDATE google_messages
                SET state=?, reason=?, attempts=attempts+?
                WHERE id=? AND state=?""",
                (to_state, reason, int(attempt), int(local_id[3:]), from_state)).rowcount
            if not changed:
                return None
            row = db.execute("SELECT * FROM google_messages WHERE id=?",
                             (int(local_id[3:]),)).fetchone()
            return self._message(row)

    @staticmethod
    def _outbound(row: sqlite3.Row) -> GoogleOutbound:
        return GoogleOutbound(row["id"], f"gc-{row['message_id']}", row["kind"],
                              json.loads(row["payload"]), row["request_id"], row["state"])

    def queue_reply(self, row: GoogleMessage, text: str, request_id: str) -> GoogleOutbound:
        """Durably fix one reply and requestId before any Google API call."""
        if row.state not in {"delivered", "held", "retrieved"}:
            raise ValueError(f"{row.id}: cannot reply to a {row.state} message")
        payload = {"space": row.space, "text": text}
        encoded = json.dumps(payload, sort_keys=True)
        with self._open() as db:
            db.execute("""INSERT OR IGNORE INTO google_outbound
                (message_id,kind,payload,request_id,created_at)
                VALUES (?,'reply',?,?,?)""", (int(row.id[3:]), encoded, request_id,
                                              datetime.now(timezone.utc).isoformat()))
            stored = db.execute("""SELECT * FROM google_outbound
                WHERE message_id=? AND kind='reply'""", (int(row.id[3:]),)).fetchone()
            if stored["payload"] != encoded:
                raise ValueError(f"{row.id}: a different reply is already recorded")
            return self._outbound(stored)

    def mark_sent(self, outbound_id: int) -> None:
        with self._open() as db:
            db.execute("""UPDATE google_outbound SET state='sent', sent_at=?
                WHERE id=? AND state='pending'""",
                (datetime.now(timezone.utc).isoformat(), outbound_id))

    def pending_replies(self) -> list[GoogleOutbound]:
        with self._open() as db:
            return [self._outbound(row) for row in db.execute(
                "SELECT * FROM google_outbound WHERE kind='reply' AND state='pending' ORDER BY id")]
