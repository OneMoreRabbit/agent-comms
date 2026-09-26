"""The message store — SQLite, one honest state per message (comms-design §3).

This is the part the September flood was about. Every rule here was bought:

- **Marking happens in the SAME transaction as the attempt.** The window between
  "sent" and "recorded" *was* the flood: four failed wakes left 62 messages
  permanently unmarked, and every later pass re-delivered them.
- **Age is checked before EVERY attempt**, not only the first. The design says
  before the first; that leaves any message which got one attempt unbounded,
  because nothing fixes a retry interval. A stale message must not reach a
  session *even once* — which is the rule's own stated intent, served strictly
  better by checking every time. Raised in our review; built to the intent.
- **States only move forward.** Nothing un-delivers.
- **Retired is not deleted.** The store is the evidence for the next incident.
- **Wake is not a message state.** A failed wake never re-queues a delivered
  message; it is a different fact in a different table.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: The eight states of comms-design §3, plus `retrieved` — which the design
#: spells `delivered` for a held message the agent came and got. Two different
#: facts under one word would make "what reached a session" unanswerable, so
#: they are two words here and the difference is flagged in our review.
RECEIVED = "received"
REFUSED = "refused"
QUEUED = "queued"
HELD = "held"
DELIVERED = "delivered"
RETRIEVED = "retrieved"
ABANDONED = "abandoned"
EXPIRED = "expired"
SUMMARISED = "summarised"

#: Forward-only. A transition not in this map is a bug, and is refused rather
#: than written — a store that will move backwards cannot be trusted as evidence.
FORWARD: dict[str, frozenset[str]] = {
    RECEIVED: frozenset({REFUSED, QUEUED, HELD, EXPIRED}),
    REFUSED: frozenset(),
    QUEUED: frozenset({DELIVERED, QUEUED, ABANDONED, EXPIRED}),
    HELD: frozenset({RETRIEVED, EXPIRED}),
    DELIVERED: frozenset(),
    RETRIEVED: frozenset(),
    ABANDONED: frozenset({SUMMARISED}),
    EXPIRED: frozenset({SUMMARISED}),
    SUMMARISED: frozenset(),
}

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_AGE = timedelta(hours=24)
#: The operator's cap: how many old messages one pass may deliver, so a backlog
#: never arrives as a hundred live turns. The rest stay queued and the agent is
#: told once. Newest first — the newest are the likeliest to still be current.
DEFAULT_MAX_PER_PASS = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    seq         INTEGER NOT NULL,
    hub_id      TEXT    UNIQUE,
    sender      TEXT    NOT NULL,
    agent       TEXT    NOT NULL DEFAULT '',
    sender_fqn  TEXT    NOT NULL DEFAULT '',
    subject     TEXT    NOT NULL DEFAULT '',
    body        TEXT    NOT NULL,
    received_at TEXT    NOT NULL,
    state       TEXT    NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    permalink   TEXT    NOT NULL DEFAULT '',
    channel     TEXT    NOT NULL DEFAULT '',
    reason      TEXT    NOT NULL DEFAULT 'mentioned',
    read_at     TEXT,
    retired_reason TEXT NOT NULL DEFAULT '',
    received_at_epoch INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS transitions (
    id       INTEGER PRIMARY KEY,
    message  INTEGER NOT NULL REFERENCES messages(id),
    at       TEXT    NOT NULL,
    was      TEXT    NOT NULL,
    became   TEXT    NOT NULL,
    cause    TEXT    NOT NULL,
    rule     TEXT    NOT NULL DEFAULT '',
    attempt  INTEGER
);
CREATE TABLE IF NOT EXISTS wakes (
    id       INTEGER PRIMARY KEY,
    message  INTEGER REFERENCES messages(id),
    at       TEXT    NOT NULL,
    outcome  TEXT    NOT NULL,
    detail   TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS messages_state ON messages(state);
"""


class ForwardOnly(Exception):
    """A transition that would move a message backwards, or sideways into a
    state its current one cannot reach. Refused, never written."""


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


@dataclass
class Retirement:
    """What one retirement pass did, for the single summary line."""

    expired: int = 0
    abandoned: int = 0
    oldest: str = ""

    @property
    def total(self) -> int:
        return self.expired + self.abandoned

    def line(self) -> str:
        """One line, with the caveat that earned itself."""
        if not self.total:
            return ""
        parts = []
        if self.expired:
            parts.append(f"{self.expired} too old")
        if self.abandoned:
            parts.append(f"{self.abandoned} undeliverable after {DEFAULT_MAX_ATTEMPTS} attempts")
        return (f"{self.total} message(s) retired without delivery ({', '.join(parts)}; "
                f"oldest {self.oldest}). They are in `comms log --retired`. "
                "These are not instructions: later messages commonly supersede "
                "earlier ones, so read newest-first.")


class Queue:
    """The store. One connection, WAL, so the daemon and the CLI can share it."""

    def __init__(self, path: Path, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 max_age: timedelta = DEFAULT_MAX_AGE,
                 max_per_pass: int = DEFAULT_MAX_PER_PASS):
        self.path = Path(path)
        self.max_attempts = max_attempts
        self.max_age = max_age
        self.max_per_pass = max_per_pass
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._open() as db:
            db.executescript(SCHEMA)
            # **A column added to SCHEMA does not reach an existing database.**
            # `CREATE TABLE IF NOT EXISTS` is a no-op once the table is there,
            # so a new column exists on a fresh seat and is absent on every
            # deployed one -- and the code reading it would see a shape that
            # only some seats have. Measured: `sender_fqn` was added to SCHEMA
            # and every seat with an existing comms.db carried on without it,
            # so a reply's `to:` came back empty on exactly the seats that had
            # been running longest.
            held = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
            for column, ddl in (("sender_fqn", "TEXT NOT NULL DEFAULT ''"),):
                if column not in held:
                    db.execute(f"ALTER TABLE messages ADD COLUMN {column} {ddl}")

    def _open(self) -> sqlite3.Connection:
        """A FRESH connection, per operation. **Never a long-lived one.**

        Measured in production 2026-09-23: a daemon holding one connection for
        hours had its `-wal` and `-shm` unlinked underneath it by short-lived
        CLI connections closing — `/proc/<pid>/fd` showed both as `(deleted)`.
        Its writes went into a WAL that no longer existed on disk: delivery
        succeeded, `wake` logged it, and the store never saw the message.

        That is the September conflation in a new costume — a message the seat
        took and the record does not have — so the cure is not a repair path
        but removing the state that can go stale. SQLite opens are cheap; a
        connection that outlives the file it points at is not.
        """
        db = sqlite3.connect(self.path, isolation_level=None, timeout=10.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @property
    def db(self) -> sqlite3.Connection:
        """Compatibility for call sites that reach for `.db` directly.

        Each access is its own connection and is closed by garbage collection.
        Anything doing more than one statement should use `_open()` in a
        `with` block so the whole unit shares one.
        """
        return self._open()

    # -- writing ------------------------------------------------------------

    def receive(self, *, hub_id: str, sender: str, body: str, agent: str = "",
                sender_fqn: str = "", subject: str = "", permalink: str = "",
                received_at: str | None = None) -> int:
        """Store one message. Idempotent on `hub_id`.

        `seq` is monotonic and local. It is a fact about the order WE saw things
        in, which is the only ordering we can vouch for.
        """
        now = received_at or _now()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO messages"
            " (seq, hub_id, sender, agent, sender_fqn, subject, body, received_at,"
            "  state, permalink)"
            " VALUES ((SELECT COALESCE(MAX(seq),0)+1 FROM messages),?,?,?,?,?,?,?,?,?)",
            (hub_id, sender, agent, sender_fqn, subject, body, now, RECEIVED, permalink))
        if cur.rowcount == 0:
            return int(self.db.execute(
                "SELECT id FROM messages WHERE hub_id=?", (hub_id,)).fetchone()["id"])
        message = int(cur.lastrowid)
        self._log(message, "", RECEIVED, "arrived from the hub")
        return message

    def _log(self, message: int, was: str, became: str, cause: str,
             rule: str = "", attempt: int | None = None, db=None) -> None:
        (db or self.db).execute(
            "INSERT INTO transitions (message, at, was, became, cause, rule, attempt)"
            " VALUES (?,?,?,?,?,?,?)", (message, _now(), was, became, cause, rule, attempt))

    def move(self, message: int, to: str, cause: str, *, rule: str = "",
             attempt: int | None = None, db=None) -> None:
        """One forward transition, with its reason. Refuses to go backwards.

        `db` carries the CALLER'S connection when it is already inside a
        transaction. Opening a second one there would block on the write lock
        this call is itself holding — a deadlock against yourself, which is the
        price of per-operation connections and is paid here rather than by
        every caller.
        """
        conn = db or self.db
        row = conn.execute("SELECT state FROM messages WHERE id=?", (message,)).fetchone()
        if row is None:
            raise ForwardOnly(f"no message {message}")
        was = row["state"]
        if to not in FORWARD[was]:
            raise ForwardOnly(
                f"message {message} is `{was}` and cannot become `{to}`. States only move "
                "forward — a store that moves backwards cannot be trusted as evidence, "
                "and this store is what the last incident was reconstructed from.")
        conn.execute("UPDATE messages SET state=? WHERE id=?", (to, message))
        self._log(message, was, to, cause, rule, attempt, db=conn)

    def record_attempt(self, message: int, *, delivered: bool, detail: str,
                       consumes_attempt: bool = True) -> int:
        """**The attempt and its outcome, in ONE transaction.**

        This method is the fix for the September flood. There is no window in
        which a message has been handed to the seat and not yet recorded,
        because the counter, the state and the transition row all land together
        or none of them do.

        Returns the attempt count after this one, so a caller can see the bound
        it has reached without asking again and getting a second answer.
        """
        with self._open() as db:                        # one unit of work
            db.execute("BEGIN")
            row = db.execute(
                "SELECT state, attempts FROM messages WHERE id=?", (message,)).fetchone()
            if row is None:
                raise ForwardOnly(f"no message {message}")
            # A REFUSAL THAT NEEDS A PERSON DOES NOT CONSUME AN ATTEMPT.
            # Measured on agent-skeleton's seat 2026-09-22: a seat upgraded
            # before its config file existed answered `broken` (exit 20) to
            # every delivery, and the mail that arrived in that window was
            # never re-delivered after a person fixed it. Stranded.
            #
            # `broken` and `conflicted` are not the message's fault and no
            # retry could have succeeded, so burning the budget on them is what
            # strands the mail: three broken windows and a message is abandoned
            # having never had a real attempt. It stays `queued` instead, and
            # the pass after the fix delivers it. The age bound still retires
            # it, so it cannot wait forever.
            attempts = int(row["attempts"]) + (1 if consumes_attempt else 0)
            if consumes_attempt:
                db.execute("UPDATE messages SET attempts=? WHERE id=?",
                           (attempts, message))
            if delivered:
                self.move(message, DELIVERED, detail, attempt=attempts, db=db)
            else:
                self._log(message, row["state"], row["state"],
                          (f"attempt {attempts} failed: {detail}" if consumes_attempt else
                           f"not attemptable: {detail} — a person must act; "
                           "no attempt consumed, the message stays queued"),
                          attempt=attempts if consumes_attempt else None, db=db)
                if consumes_attempt and attempts >= self.max_attempts:
                    self.move(message, ABANDONED,
                              f"{attempts} attempts reached", rule="max_attempts",
                              attempt=attempts, db=db)
        return attempts

    def record_wake(self, message: int | None, outcome: str, detail: str = "") -> None:
        """A wake is not a message state. Recorded here and nowhere else.

        A failed wake must never re-queue a delivered message — that conflation
        is what seeded 62 permanently-unmarked records from four bad moments.
        """
        self.db.execute("INSERT INTO wakes (message, at, outcome, detail) VALUES (?,?,?,?)",
                        (message, _now(), outcome, detail))

    # -- reading ------------------------------------------------------------

    def _too_old(self, received_at: str) -> bool:
        try:
            age = datetime.now(tz=timezone.utc) - datetime.fromisoformat(received_at)
        except ValueError:
            return False
        return age > self.max_age

    def due(self) -> list[sqlite3.Row]:
        """What the next pass should deliver — newest first, capped.

        Two rules meet here:

        - **Age before EVERY attempt.** Anything past the bound is retired by
          `retire()` before this is asked, so nothing stale is ever returned.
        - **The operator's cap.** At most `max_per_pass`, newest first, so a
          backlog never arrives as a hundred live turns. The rest stay queued
          and the agent is told once that they exist.
        """
        rows = self.db.execute(
            "SELECT * FROM messages WHERE state=? ORDER BY seq DESC LIMIT ?",
            (QUEUED, self.max_per_pass)).fetchall()
        return [r for r in rows if not self._too_old(r["received_at"])]

    def backlog(self) -> int:
        """How many are queued beyond what this pass will take."""
        total = self.db.execute(
            "SELECT COUNT(*) c FROM messages WHERE state=?", (QUEUED,)).fetchone()["c"]
        return max(0, int(total) - self.max_per_pass)

    def retire(self) -> Retirement:
        """Retire everything past the age bound, BEFORE any attempt is made.

        Checked on every pass, not only before the first attempt: a message that
        failed once must not become unbounded, and the rule's own reason — a
        stale message must not reach a session even once — demands it.
        """
        out = Retirement()
        rows = self.db.execute(
            "SELECT id, received_at, state FROM messages WHERE state IN (?,?,?)",
            (RECEIVED, QUEUED, HELD)).fetchall()
        for row in rows:
            if not self._too_old(row["received_at"]):
                continue
            self.move(row["id"], EXPIRED,
                      f"older than {self.max_age}", rule="max_age")
            out.expired += 1
            out.oldest = min(out.oldest or row["received_at"], row["received_at"])
        abandoned = self.db.execute(
            "SELECT id, received_at FROM messages WHERE state=?", (ABANDONED,)).fetchall()
        out.abandoned = len(abandoned)
        for row in abandoned:
            out.oldest = min(out.oldest or row["received_at"], row["received_at"])
        return out

    def state_of(self, message: int) -> str:
        return str(self.db.execute(
            "SELECT state FROM messages WHERE id=?", (message,)).fetchone()["state"])

    def history(self, message: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM transitions WHERE message=? ORDER BY id", (message,)).fetchall()

    def counts(self) -> dict[str, int]:
        """For `comms stats --json`: counts by state, for the estate to poll."""
        return {r["state"]: int(r["n"]) for r in self.db.execute(
            "SELECT state, COUNT(*) n FROM messages GROUP BY state")}


# -- the daemon's store, SQLite underneath -----------------------------------

class MessageStore(Queue):
    """The message store the daemon and CLI use. SQLite, Mention-shaped.

    **One store, not two.** This is the migration: the JSONL file stops being
    the store of record and becomes what it always was underneath — a backup —
    while every read and write goes through the states in §3.

    It keeps the `Store` message API deliberately. Seventeen call sites read
    and write messages; rewriting all of them in one act, days after an
    incident, is how a migration becomes a second incident. The interface is
    the seam, so the interface is what stays still.

    Daemon liveness, queue position and the event log are NOT here. They are
    different facts in different files and were never part of the message
    store; moving them would be scope, not migration.
    """

    #: The JSONL store carried daemon liveness, queue position and the event
    #: log alongside messages. Those are different facts in different files and
    #: were never part of the message store, so they stay where they are and
    #: this delegates rather than reimplementing them. Moving them would be
    #: scope, not migration.
    def _side(self):
        from .store import Store
        return Store(self.path.parent)

    def ensure(self):
        return self._side().ensure()

    def daemon_state(self):
        return self._side().daemon_state()

    def acquire_daemon_lock(self):
        return self._side().acquire_daemon_lock()

    def lock_holder_pid(self):
        return self._side().lock_holder_pid()

    def save_position(self, queue_id, last_event_id):
        return self._side().save_position(queue_id, last_event_id)

    def load_position(self):
        return self._side().load_position()

    def last_message_id(self) -> int:
        row = self.db.execute("SELECT MAX(CAST(hub_id AS INTEGER)) m FROM messages").fetchone()
        return int(row["m"] or 0)

    def record(self, level, message):
        return self._side().record(level, message)

    def record_build(self, version):
        return self._side().record_build(version)

    def daemon_build(self):
        return self._side().daemon_build()

    def sleeping(self):
        return self._side().sleeping()

    def set_sleeping(self, value):
        return self._side().set_sleeping(value)

    def unreachable(self):
        return self._side().unreachable()

    def set_unreachable(self, value):
        return self._side().set_unreachable(value)

    def _to_mention(self, row) -> "Mention":
        from .store import Mention

        state = row["state"]
        return Mention(
            id=row["hub_id"] and int(row["hub_id"]) or row["id"],
            sender=row["sender"], channel=row["channel"], topic=row["subject"],
            content=row["body"], timestamp=int(row["received_at_epoch"] or 0),
            permalink=row["permalink"], read=bool(row["read_at"]),
            agent=row["agent"] or "",
            # **Persisted, because a reply needs it.** Computed on arrival and
            # not stored, it was lost the moment anything read from the store:
            # a reply's `to:` is the FQN the original sender declared, and it
            # came back empty. Measured on the seats 2026-09-26.
            sender_fqn=(row["sender_fqn"] if "sender_fqn" in row.keys() else "") or "",
            reason=row["reason"] or "mentioned",
            delivered=state in (DELIVERED, RETRIEVED, REFUSED, EXPIRED, ABANDONED),
            attempts=int(row["attempts"]),
            authorised=state != REFUSED,
            retired=row["retired_reason"] or "",
        )

    def _rows(self):
        return self.db.execute("SELECT * FROM messages ORDER BY seq").fetchall()

    def append(self, mention, held: bool = False) -> None:
        """Store one arrival. Idempotent on the hub id, as `receive` is.

        `held` means the addressed agent's declared delivery mode is `hold`:
        accepted and stored, never injected, the agent asks for it. It lands in
        **HELD**, not QUEUED, and HELD is the only state `RETRIEVED` can be
        reached from.

        **This state existed and nothing wrote it.** `HELD` was in the schema
        and in the transition map from the start, three places read it, and no
        code ever moved a message into it -- so a `delivery: hold` message sat
        in QUEUED, where `RETRIEVED` is unreachable, and the age bound expired
        it however faithfully the agent had read it. Measured 2026-09-26.
        """
        mid = self.receive(hub_id=str(mention.id), sender=mention.sender,
                           body=mention.content, subject=mention.topic,
                           agent=getattr(mention, "agent", "") or "",
                           sender_fqn=getattr(mention, "sender_fqn", "") or "",
                           permalink=mention.permalink,
                           received_at=datetime.fromtimestamp(
                               mention.timestamp, tz=timezone.utc).isoformat(timespec="seconds"))
        self.db.execute("UPDATE messages SET channel=?, reason=?, received_at_epoch=? WHERE id=?",
                        (mention.channel, mention.reason, mention.timestamp, mid))
        if self.state_of(mid) != RECEIVED:
            return
        if not mention.authorised:
            self.move(mid, REFUSED, "sender not permitted", rule="directory")
        elif mention.retired:
            # Arrives already retired — an import, or a caller that has decided.
            # Honour it rather than queueing something nobody intends to deliver.
            self.move(mid, EXPIRED, mention.retired, rule="retired")
            self.db.execute("UPDATE messages SET retired_reason=? WHERE id=?",
                            (mention.retired, mid))
        elif held:
            self.move(mid, HELD, "delivery: hold — the agent asks for it", rule="delivery")
        elif mention.delivered:
            self.move(mid, QUEUED, "permitted")
            self.record_attempt(mid, delivered=True, detail="already delivered on arrival")
        else:
            self.move(mid, QUEUED, "permitted")

    def all(self) -> list:
        return [self._to_mention(r) for r in self._rows()]

    def unread(self) -> list:
        return [m for m in self.all() if not m.read]

    def undelivered(self) -> list:
        return [m for m in self.all() if not m.delivered]

    def _find(self, hub_id: int):
        return self.db.execute("SELECT * FROM messages WHERE hub_id=?",
                               (str(hub_id),)).fetchone()

    def attempt_by_hub_id(self, hub_id: int, detail: str) -> int:
        """Record a failed attempt for a HUB id, and return the count after it.

        `MessageStore` speaks hub ids to its callers and row ids to the store,
        so the translation happens here -- once, explicitly. A shim that did
        this translation ALSO skipped the bound: it added one to the counter
        and never abandoned anything, so `max_attempts` governed nothing on the
        live path and a message reached its eighth attempt against a cap of
        three (UC-05, 2026-09-25). Translating and enforcing are different
        jobs; this one only translates.
        """
        row = self._find(hub_id)
        if row is None:
            return 0
        return self.record_attempt(row["id"], delivered=False, detail=detail)

    def mark_delivered(self, message_id: int) -> bool:
        row = self._find(message_id)
        if row is None:
            return False
        if row["state"] in (QUEUED, RECEIVED):
            self.record_attempt(row["id"], delivered=True, detail="delivered")
        elif row["state"] == HELD:
            self.move(row["id"], RETRIEVED, "the agent read it")
        return True

    def mark_read(self, message_id: int) -> bool:
        """The agent looked at it. For a HELD message that is the delivery.

        `read` and `delivered` are different facts and stay separate -- but a
        `delivery: hold` message is never injected, so the agent READING it is
        the only way it ever reaches the agent. That is what moves HELD to
        RETRIEVED, and it is why RETRIEVED is not DELIVERED: nothing was put in
        front of anyone.
        """
        row = self._find(message_id)
        if row is None:
            return False
        self.db.execute("UPDATE messages SET read_at=? WHERE id=?", (_now(), row["id"]))
        if row["state"] == HELD:
            self.move(row["id"], RETRIEVED, "the agent asked for it", rule="delivery")
        return True

    def mark_retired(self, message_id: int, reason: str) -> bool:
        row = self._find(message_id)
        if row is None:
            return False
        if row["state"] in (RECEIVED, QUEUED, HELD):
            self.move(row["id"], EXPIRED, reason, rule="retired")
        self.db.execute("UPDATE messages SET retired_reason=? WHERE id=?", (reason, row["id"]))
        return True

