"""Google Chat records, isolated from integer hub message identifiers."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    created_at: str
    failure_reason: str = ""
    upload_attempts: int = 0
    upload_in_progress: bool = False


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
            outbound_columns = {row[1] for row in db.execute("PRAGMA table_info(google_outbound)")}
            if "failure_reason" not in outbound_columns:
                db.execute("ALTER TABLE google_outbound ADD COLUMN failure_reason TEXT NOT NULL DEFAULT ''")
            if "upload_attempts" not in outbound_columns:
                db.execute("ALTER TABLE google_outbound ADD COLUMN upload_attempts INTEGER NOT NULL DEFAULT 0")
            if "upload_in_progress" not in outbound_columns:
                db.execute("ALTER TABLE google_outbound ADD COLUMN upload_in_progress INTEGER NOT NULL DEFAULT 0")

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

    def set_files(self, row: GoogleMessage, files: list[dict]) -> GoogleMessage | None:
        """Record fully downloaded files before a seat turn can name them."""
        if row.state != "queued" or not row.payload.get("has_attachments"):
            raise ValueError("files require a queued message with attachments")
        payload = {**row.payload, "files": files}
        with self._open() as db:
            changed = db.execute("""UPDATE google_messages SET payload=?
                WHERE id=? AND state='queued' AND payload=?""",
                (json.dumps(payload, sort_keys=True), int(row.id[3:]),
                 json.dumps(row.payload, sort_keys=True))).rowcount
            if not changed:
                return None
            updated = db.execute("SELECT * FROM google_messages WHERE id=?",
                                 (int(row.id[3:]),)).fetchone()
            return self._message(updated)

    def transition(self, local_id: str, *, from_state: str, to_state: str,
                   reason: str = "", attempt: bool = False,
                   notice: str | None = None,
                   notice_kind: str | None = None) -> GoogleMessage | None:
        """Compare-and-set a Google state so duplicate workers cannot both advance it."""
        if not re.fullmatch(r"gc-[0-9]+", local_id):
            raise ValueError("invalid Google message id")
        with self._open() as db:
            original_route = None
            if notice:
                route = db.execute("SELECT space FROM google_messages WHERE id=? AND state=?",
                                   (int(local_id[3:]), from_state)).fetchone()
                original_route = route["space"] if route else None
            if to_state == "refused":
                existing = db.execute("SELECT payload,space FROM google_messages WHERE id=? AND state=?",
                                      (int(local_id[3:]), from_state)).fetchone()
                if existing is None:
                    return None
                original = json.loads(existing["payload"])
                identity = {key: original[key] for key in ("sender_account_id", "sender_address")
                            if isinstance(original.get(key), str) and original[key]}
                changed = db.execute("""UPDATE google_messages
                    SET state=?, reason=?, attempts=attempts+?, payload=?, space=''
                    WHERE id=? AND state=?""",
                    (to_state, reason, int(attempt), json.dumps(identity, sort_keys=True),
                     int(local_id[3:]), from_state)).rowcount
            else:
                changed = db.execute("""UPDATE google_messages
                    SET state=?, reason=?, attempts=attempts+?
                    WHERE id=? AND state=?""",
                    (to_state, reason, int(attempt), int(local_id[3:]), from_state)).rowcount
            if not changed:
                return None
            if notice and original_route and re.fullmatch(r"spaces/[A-Za-z0-9_-]+", original_route):
                db.execute("""INSERT OR IGNORE INTO google_outbound
                    (message_id,kind,payload,request_id,created_at)
                    VALUES (?,?,?,?,?)""",
                    (int(local_id[3:]), notice_kind or "notice-" + to_state,
                     json.dumps({"space": original_route, "text": notice}, sort_keys=True),
                     str(uuid.uuid4()), datetime.now(timezone.utc).isoformat()))
            row = db.execute("SELECT * FROM google_messages WHERE id=?",
                             (int(local_id[3:]),)).fetchone()
            return self._message(row)

    @staticmethod
    def _outbound(row: sqlite3.Row) -> GoogleOutbound:
        return GoogleOutbound(row["id"], f"gc-{row['message_id']}", row["kind"],
                              json.loads(row["payload"]), row["request_id"], row["state"],
                              row["created_at"], row["failure_reason"],
                              row["upload_attempts"], bool(row["upload_in_progress"]))

    def queue_reply(self, row: GoogleMessage, text: str, request_id: str,
                    file_path: str | None = None,
                    source_text: str | None = None,
                    generated_answer: bool = False) -> GoogleOutbound:
        """Durably fix one reply and requestId before any Google API call."""
        if row.state not in {"delivered", "held", "retrieved", "uncertain"}:
            raise ValueError(f"{row.id}: cannot reply to a {row.state} message")
        payload = {"space": row.space, "text": text}
        if source_text is not None and source_text != text:
            payload["source_text"] = source_text
        if file_path is not None:
            payload["source_text"] = source_text if source_text is not None else text
            payload["file_path"] = file_path
        if generated_answer:
            payload["generated_answer"] = True
        encoded = json.dumps(payload, sort_keys=True)
        with self._open() as db:
            if row.state == "uncertain":
                db.execute("""UPDATE google_messages SET state='delivered',
                    reason='agent reply confirms uncertain handoff'
                    WHERE id=? AND state='uncertain'""", (int(row.id[3:]),))
            existing = db.execute("SELECT * FROM google_outbound WHERE message_id=? AND kind='reply'",
                                  (int(row.id[3:]),)).fetchone()
            if existing is not None and existing["state"] == "failed":
                previous = json.loads(existing["payload"])
                if previous.get("file_path") and not previous.get("drive_link"):
                    db.execute("""UPDATE google_outbound SET payload=?, request_id=?, state='pending',
                        created_at=?, failure_reason='', upload_attempts=0, upload_in_progress=0
                        WHERE id=? AND state='failed'""",
                        (encoded, request_id, datetime.now(timezone.utc).isoformat(), existing["id"]))
            db.execute("""INSERT OR IGNORE INTO google_outbound
                (message_id,kind,payload,request_id,created_at)
                VALUES (?,'reply',?,?,?)""", (int(row.id[3:]), encoded, request_id,
                                              datetime.now(timezone.utc).isoformat()))
            stored = db.execute("""SELECT * FROM google_outbound
                WHERE message_id=? AND kind='reply'""", (int(row.id[3:]),)).fetchone()
            if stored["payload"] != encoded:
                raise ValueError(f"{row.id}: a different reply is already recorded")
            return self._outbound(stored)

    def reply_for_message(self, message_id: str) -> GoogleOutbound | None:
        if not re.fullmatch(r"gc-[0-9]+", message_id):
            return None
        with self._open() as db:
            row = db.execute("""SELECT * FROM google_outbound
                WHERE message_id=? AND kind='reply'""", (int(message_id[3:]),)).fetchone()
            return self._outbound(row) if row else None

    def queue_notice(self, row: GoogleMessage, kind: str, text: str) -> GoogleOutbound | None:
        """Store one fixed notice per message and condition before any post."""
        if not re.fullmatch(r"notice-[a-z-]+", kind):
            raise ValueError("invalid Google notice kind")
        if not re.fullmatch(r"spaces/[A-Za-z0-9_-]+", row.space):
            return None
        with self._open() as db:
            db.execute("""INSERT OR IGNORE INTO google_outbound
                (message_id,kind,payload,request_id,created_at)
                VALUES (?,?,?,?,?)""",
                (int(row.id[3:]), kind,
                 json.dumps({"space": row.space, "text": text}, sort_keys=True),
                 str(uuid.uuid4()), datetime.now(timezone.utc).isoformat()))
            stored = db.execute("SELECT * FROM google_outbound WHERE message_id=? AND kind=?",
                                (int(row.id[3:]), kind)).fetchone()
            return self._outbound(stored)

    def set_outbound_link(self, outbound: GoogleOutbound, link: str) -> GoogleOutbound | None:
        """Fix the Drive link before Chat can receive the reply."""
        if outbound.state != "pending" or not outbound.payload.get("file_path"):
            raise ValueError("no pending file reply")
        payload = {**outbound.payload, "drive_link": link}
        wire_text = outbound.payload["text"]
        payload["text"] = f"{wire_text}\n\nFile: {link}" if wire_text else f"File: {link}"
        with self._open() as db:
            changed = db.execute("""UPDATE google_outbound SET payload=?, upload_in_progress=0
                WHERE id=? AND state='pending' AND payload=?""",
                (json.dumps(payload, sort_keys=True), outbound.id,
                 json.dumps(outbound.payload, sort_keys=True))).rowcount
            if not changed:
                return None
            row = db.execute("SELECT * FROM google_outbound WHERE id=?", (outbound.id,)).fetchone()
            return self._outbound(row)

    def mark_sent(self, outbound_id: int) -> None:
        with self._open() as db:
            db.execute("""UPDATE google_outbound SET state='sent', sent_at=?
                WHERE id=? AND state='pending'""",
                (datetime.now(timezone.utc).isoformat(), outbound_id))

    def mark_failed(self, outbound_id: int, reason: str) -> None:
        with self._open() as db:
            db.execute("""UPDATE google_outbound SET state='failed', failure_reason=?
                , upload_in_progress=0 WHERE id=? AND state='pending'""", (reason, outbound_id))

    def begin_upload(self, outbound_id: int) -> GoogleOutbound | None:
        """Persist an attempt before upload; an interrupted attempt is never replayed."""
        with self._open() as db:
            changed = db.execute("""UPDATE google_outbound SET upload_attempts=upload_attempts+1,
                upload_in_progress=1 WHERE id=? AND state='pending' AND upload_in_progress=0
                AND upload_attempts<3""", (outbound_id,)).rowcount
            row = db.execute("SELECT * FROM google_outbound WHERE id=?", (outbound_id,)).fetchone()
            return self._outbound(row) if changed else None

    def upload_failed(self, outbound_id: int, reason: str) -> GoogleOutbound:
        with self._open() as db:
            db.execute("""UPDATE google_outbound SET upload_in_progress=0,
                failure_reason=?, state=CASE WHEN upload_attempts>=3 THEN 'failed' ELSE state END
                WHERE id=? AND state='pending'""", (reason, outbound_id))
            row = db.execute("SELECT * FROM google_outbound WHERE id=?", (outbound_id,)).fetchone()
            return self._outbound(row)

    def get_outbound(self, outbound_id: int) -> GoogleOutbound | None:
        with self._open() as db:
            row = db.execute("SELECT * FROM google_outbound WHERE id=?", (outbound_id,)).fetchone()
            return self._outbound(row) if row else None

    def expire_pending_replies(self, *, now: datetime | None = None) -> list[str]:
        cutoff = ((now or datetime.now(timezone.utc)) - timedelta(hours=24)).isoformat()
        with self._open() as db:
            ids = [f"gc-{row['message_id']}" for row in db.execute(
                "SELECT message_id FROM google_outbound WHERE kind='reply' "
                "AND state='pending' AND created_at<=?", (cutoff,))]
            db.execute("""UPDATE google_outbound SET state='failed',
                failure_reason='reply pending for 24 hours'
                WHERE kind='reply' AND state='pending' AND created_at<=?""", (cutoff,))
            return ids

    def replies(self) -> list[GoogleOutbound]:
        with self._open() as db:
            return [self._outbound(row) for row in db.execute(
                "SELECT * FROM google_outbound WHERE kind='reply' ORDER BY id")]

    def pending_replies(self) -> list[GoogleOutbound]:
        with self._open() as db:
            return [self._outbound(row) for row in db.execute(
                "SELECT * FROM google_outbound WHERE kind='reply' AND state='pending' ORDER BY id")]

    def notices(self) -> list[GoogleOutbound]:
        with self._open() as db:
            return [self._outbound(row) for row in db.execute(
                "SELECT * FROM google_outbound WHERE kind LIKE 'notice-%' ORDER BY id")]

    def pending_notices(self) -> list[GoogleOutbound]:
        with self._open() as db:
            return [self._outbound(row) for row in db.execute(
                "SELECT * FROM google_outbound WHERE kind LIKE 'notice-%' "
                "AND state='pending' ORDER BY id")]
