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
    subject     TEXT    NOT NULL DEFAULT '',
    body        TEXT    NOT NULL,
    received_at TEXT    NOT NULL,
    state       TEXT    NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    permalink   TEXT    NOT NULL DEFAULT ''
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
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    # -- writing ------------------------------------------------------------

    def receive(self, *, hub_id: str, sender: str, body: str, agent: str = "",
                subject: str = "", permalink: str = "", received_at: str | None = None) -> int:
        """Store one message. Idempotent on `hub_id`.

        `seq` is monotonic and local. It is a fact about the order WE saw things
        in, which is the only ordering we can vouch for.
        """
        now = received_at or _now()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO messages"
            " (seq, hub_id, sender, agent, subject, body, received_at, state)"
            " VALUES ((SELECT COALESCE(MAX(seq),0)+1 FROM messages),?,?,?,?,?,?,?)",
            (hub_id, sender, agent, subject, body, now, RECEIVED))
        if cur.rowcount == 0:
            return int(self.db.execute(
                "SELECT id FROM messages WHERE hub_id=?", (hub_id,)).fetchone()["id"])
        message = int(cur.lastrowid)
        self._log(message, "", RECEIVED, "arrived from the hub")
        return message

    def _log(self, message: int, was: str, became: str, cause: str,
             rule: str = "", attempt: int | None = None) -> None:
        self.db.execute(
            "INSERT INTO transitions (message, at, was, became, cause, rule, attempt)"
            " VALUES (?,?,?,?,?,?,?)", (message, _now(), was, became, cause, rule, attempt))

    def move(self, message: int, to: str, cause: str, *, rule: str = "",
             attempt: int | None = None) -> None:
        """One forward transition, with its reason. Refuses to go backwards."""
        row = self.db.execute("SELECT state FROM messages WHERE id=?", (message,)).fetchone()
        if row is None:
            raise ForwardOnly(f"no message {message}")
        was = row["state"]
        if to not in FORWARD[was]:
            raise ForwardOnly(
                f"message {message} is `{was}` and cannot become `{to}`. States only move "
                "forward — a store that moves backwards cannot be trusted as evidence, "
                "and this store is what the last incident was reconstructed from.")
        self.db.execute("UPDATE messages SET state=? WHERE id=?", (to, message))
        self._log(message, was, to, cause, rule, attempt)

    def record_attempt(self, message: int, *, delivered: bool, detail: str) -> None:
        """**The attempt and its outcome, in ONE transaction.**

        This method is the fix for the September flood. There is no window in
        which a message has been handed to the seat and not yet recorded,
        because the counter, the state and the transition row all land together
        or none of them do.
        """
        with self.db:                                   # BEGIN ... COMMIT
            self.db.execute("BEGIN")
            row = self.db.execute(
                "SELECT state, attempts FROM messages WHERE id=?", (message,)).fetchone()
            if row is None:
                raise ForwardOnly(f"no message {message}")
            attempts = int(row["attempts"]) + 1
            self.db.execute("UPDATE messages SET attempts=? WHERE id=?", (attempts, message))
            if delivered:
                self.move(message, DELIVERED, detail, attempt=attempts)
            else:
                self._log(message, row["state"], row["state"],
                          f"attempt {attempts} failed: {detail}", attempt=attempts)
                if attempts >= self.max_attempts:
                    self.move(message, ABANDONED,
                              f"{attempts} attempts reached", rule="max_attempts",
                              attempt=attempts)

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
