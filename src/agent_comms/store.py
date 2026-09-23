"""Local state under `~/.comms/`.

The seat home is host-mounted, so this survives container recreation (contract
§2a, R6). Append-only JSONL for messages, a small JSON file for queue position,
so a partially-written file costs one line rather than the history.
"""

from __future__ import annotations

import fcntl
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Mention:
    """One message addressed to this seat."""

    id: int
    sender: str
    channel: str
    topic: str
    content: str
    timestamp: int
    permalink: str
    read: bool = False
    #: Why this message was stored — mention, topic, or direct message. Shown in
    #: the inbox so a seat can tell an explicit summons from a topic it owns.
    reason: str = "mentioned"
    #: Has this message actually reached the agent? Distinct from `read`:
    #: `read` is the human/agent having looked at it, `delivered` is the client
    #: having got it into a session. A message queued while the seat was dormant
    #: stays undelivered until the seat wakes.
    delivered: bool = False
    #: How many delivery attempts this message has had. Bounded retry: a message
    #: that fails for a reason no retry can change must stop being retried, or the
    #: queue spins forever and buries everything else. Found by testing against a
    #: real seat — an oversized body answers `failed` at exit 10, which reads as
    #: retryable and would fail identically every time.
    attempts: int = 0
    #: Is the sender one this seat accepts direction from (ADR-0009 §9)?
    #: An unauthorised message is still stored and shown — the agent must be able
    #: to report it — but it is never presented as an instruction.
    authorised: bool = True
    #: Set when a message was retired instead of delivered — too old, or too
    #: many attempts. Non-empty means `delivered` is bookkeeping, not a fact
    #: about an agent having seen it. Separate fields because conflating them
    #: is exactly what made the 1.0.0 store ambiguous to migrate.
    retired: str = ""

    @property
    def when(self) -> str:
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc).isoformat(timespec="seconds")


@dataclass
class DaemonState:
    """Whether a daemon is up for this seat, and how recently it polled.

    `running` is the flock answer. `last_tick` is the heartbeat written every
    loop turn; a running daemon with an old tick is a different fault from no
    daemon at all, so they are two fields rather than one verdict.
    """

    running: bool
    pid: int | None = None
    last_tick: datetime | None = None
    detail: str = ""

    #: Zulip's own heartbeat turns the loop over about every 54s (measured), so
    #: a tick older than this means the loop is not turning even though a
    #: process holds the lock — wedged, not merely quiet.
    STALE_AFTER_SECS = 300

    @property
    def silent_for(self) -> float | None:
        if self.last_tick is None:
            return None
        return (datetime.now(tz=timezone.utc) - self.last_tick).total_seconds()

    @property
    def stale(self) -> bool:
        gap = self.silent_for
        return self.running and gap is not None and gap > self.STALE_AFTER_SECS

    def summary(self) -> str:
        """One line, written for whoever is asking why nothing arrived."""
        gap = self.silent_for
        ago = "never polled" if gap is None else f"last polled {_ago(gap)} ago"
        if not self.running:
            return (
                f"NOT RUNNING — nothing is watching the hub, so messages sent to this "
                f"seat are being lost, not queued ({ago}). Start it: comms daemon --restart"
            )
        if self.stale:
            return (
                f"running (pid {self.pid}) but WEDGED — {ago}, and Zulip's heartbeat "
                "should turn the loop over about every minute. Replace it: "
                "comms daemon --restart"
            )
        return f"running (pid {self.pid}), {ago}"


def _ago(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.messages = root / "messages.jsonl"
        self.state = root / "state.json"
        self.log = root / "events.log"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def daemon_state(self) -> "DaemonState":
        """Is a daemon actually running for this seat, and when did it last poll?

        Asks the **lock**, not the lock file: `flock` is released by the OS
        however a process ends, so a lock we can take means nobody holds it —
        while the file itself outlives any daemon and says nothing. Reading the
        file for a PID was the trap here; the file on this seat sat there for
        four days across several dead daemons.

        The probe is read-write-open + non-blocking flock, released immediately.
        It never truncates: the running daemon's PID must survive being looked at.
        """
        lock_path = self.root / "daemon.lock"
        pid, running = None, True
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            return DaemonState(running=False, pid=None, last_tick=None,
                               detail="the state directory is not readable")
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                running = True  # somebody holds it: a daemon is up
            else:
                running = False
                fcntl.flock(fd, fcntl.LOCK_UN)
            try:
                raw = os.pread(fd, 32, 0).decode("utf-8", "replace").strip()
                pid = int(raw) if raw.isdigit() else None
            except (OSError, ValueError):
                pid = None
        finally:
            os.close(fd)

        last_tick = None
        position = self.load_position() or {}
        stamp = position.get("saved_at")
        if stamp:
            try:
                last_tick = datetime.fromisoformat(stamp)
            except ValueError:
                last_tick = None

        detail = ""
        if running and pid is None:
            # The lock file is empty but somebody holds the lock. The kernel
            # still knows who, so ask it (see `lock_holder_pid`).
            pid = self.lock_holder_pid()
            if pid is not None:
                detail = "pid read from the kernel's lock table; the lock file is empty"

        return DaemonState(running=running, pid=pid if running else None,
                           last_tick=last_tick, detail=detail)

    def lock_holder_pid(self) -> int | None:
        """Ask the OS which process holds the daemon lock.

        The lock file's contents are a **copy** the daemon wrote about itself;
        the kernel is the owner of the fact. When the copy is missing — an
        empty lock file — the answer is still knowable, so derive it from the
        owner rather than telling a person to go hunting.

        This exists because the alternative people reach for is a pattern
        match, and a pattern match here is dangerous: a seat's tmux server was
        started as `tmux new -ds comms ... comms daemon`, so its OWN argv
        contains `comms daemon`. `pkill -f "comms daemon"` matches the server
        and takes every session inside it. That cost thirteen seats their
        sessions on 2026-09-21. An inode is exact; a pattern is a guess.

        Returns None where the answer cannot be had (no `/proc/locks`, no
        matching entry) — never a guess.
        """
        lock_path = self.root / "daemon.lock"
        try:
            st = os.stat(lock_path)
            lines = Path("/proc/locks").read_text().splitlines()
        except OSError:
            return None

        exact = "%02x:%02x:%d" % (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
        by_inode = None
        for line in lines:
            parts = line.split()
            # ... FLOCK ADVISORY WRITE <pid> <maj:min:inode> <start> <end>
            if len(parts) < 6 or parts[1] != "FLOCK":
                continue
            try:
                pid = int(parts[4])
            except ValueError:
                continue
            where = parts[5]
            if where == exact:
                return pid
            if where.rsplit(":", 1)[-1] == str(st.st_ino):
                # Same inode, device spelled differently — a mount namespace
                # reports its own numbers. Keep it as the fallback rather than
                # the answer, so an exact match always wins.
                by_inode = pid
        return by_inode

    def acquire_daemon_lock(self):
        """Take the single-daemon lock, or raise `DaemonAlreadyRunning`.

        An advisory `flock` on a file in the state directory. The OS releases it
        when the process ends however it ends, so a killed daemon leaves no stale
        lock to clear by hand — which a PID file would.
        """
        from .errors import DaemonAlreadyRunning

        self.ensure()
        lock_path = self.root / "daemon.lock"
        # **Open without truncating.** `open("w")` truncates before the flock is
        # attempted, so a LOSING contender destroys the WINNER's pid on its way
        # out — the running daemon keeps the lock and loses its own identity.
        # Measured on this seat 2026-09-17: a second daemon started by the estate's
        # install left a 0-byte lock, `status` reported "pid None", and
        # `daemon --stop` could not signal what it could not name. Take the lock
        # first; truncate and write only once it is ours.
        handle = lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise DaemonAlreadyRunning(
                f"another comms daemon already holds {lock_path}. Two daemons on one bot "
                "means two event queues, so every mention would be stored and handed to "
                "notify_command twice. Replace it in one step: comms daemon --restart"
            ) from None
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        return handle

    # -- messages ----------------------------------------------------------

    def append(self, mention: Mention) -> None:
        self.ensure()
        with self.messages.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(mention), ensure_ascii=False) + "\n")

    def all(self) -> list[Mention]:
        if not self.messages.exists():
            return []
        out: list[Mention] = []
        for line in self.messages.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(Mention(**json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                # One corrupt line must not hide the rest of the history.
                continue
        return out

    def unread(self) -> list[Mention]:
        return [m for m in self.all() if not m.read]

    def undelivered(self) -> list[Mention]:
        """Messages that never reached a session, oldest first.

        These are the ones waiting for the seat to wake. Order is preserved: a
        conversation delivered out of order is worse than one delivered late.
        """
        return [m for m in self.all() if not m.delivered]

    def record_attempt(self, message_id: int) -> int:
        """Count one delivery attempt, and return the new total."""
        total = 0
        rows = self.all()
        for m in rows:
            if m.id == message_id:
                m.attempts += 1
                total = m.attempts
        self._rewrite(rows)
        return total

    def mark_delivered(self, message_id: int) -> bool:
        rows = self.all()
        found = False
        for m in rows:
            if m.id == message_id:
                m.delivered, found = True, True
        if found:
            self._rewrite(rows)
        return found

    def mark_retired(self, message_id: int, reason: str) -> bool:
        """Retire a message WITHOUT delivering it. Kept, never deleted.

        `delivered` is set so no pass picks it up again, and `retired` records
        that it was never actually put in front of anyone — the two facts are
        separate because conflating them is what made 1.0.0's store unreadable
        (a refused message carried delivered:true, so 15 refusals would have
        imported as successes).
        """
        rows = self.all()
        for row in rows:
            if row.id == message_id:
                row.delivered = True
                row.retired = reason
                self._rewrite(rows)
                return True
        return False

    def mark_read(self, message_id: int) -> bool:
        rows = self.all()
        found = False
        for m in rows:
            if m.id == message_id:
                m.read, found = True, True
        if found:
            self._rewrite(rows)
        return found

    def _rewrite(self, rows: list[Mention]) -> None:
        self.ensure()
        tmp = self.messages.with_suffix(".jsonl.tmp")
        tmp.write_text(
            "".join(json.dumps(asdict(m), ensure_ascii=False) + "\n" for m in rows),
            encoding="utf-8",
        )
        tmp.replace(self.messages)

    # -- queue position ----------------------------------------------------

    def record_build(self, version: str) -> None:
        """Stamp which build this daemon is. Read by `doctor`, by a LATER CLI.

        The one piece of state here that is not removable: a seat mid-upgrade
        genuinely has two versions on it, because the running daemon is a
        process and the CLI is whatever is on disk now. That condition cannot
        be deleted, so it is reported instead — which is the honest half of
        catalogue 0.56, not a substitute for the other half.
        """
        (self.root / "daemon.build").write_text(version, encoding="utf-8")

    def daemon_build(self) -> str:
        """The build the RUNNING daemon started as, or empty if it never said."""
        try:
            return (self.root / "daemon.build").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def save_position(self, queue_id: str, last_event_id: int) -> None:
        """Record where the queue is, and that we were alive to record it.

        `saved_at` doubles as the daemon's heartbeat. It is written every tick —
        Zulip's heartbeat turns the loop over about every 54s even on a silent
        channel — so a stale value is itself the evidence that nothing is
        polling. That is what makes "down since when" answerable, which it was
        not when this seat's daemon died unnoticed on 2026-09-10.
        """
        self.ensure()
        self.state.write_text(
            json.dumps(
                {
                    "queue_id": queue_id,
                    "last_event_id": last_event_id,
                    "saved_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
                }
            ),
            encoding="utf-8",
        )

    def last_message_id(self) -> int:
        """The highest Zulip message id this seat has actually handled.

        **The durable receive marker, and the only one that survives a queue.**
        `last_event_id` is queue-local: it resets whenever a queue is discarded,
        so it cannot say where to resume from — which is exactly when resuming
        matters. A message id is permanent and channel-wide.

        Read from the message log rather than kept as a separate counter, so it
        cannot disagree with what was stored. 0 when nothing has arrived, which
        makes a first run a full catch-up rather than a special case.
        """
        best = 0
        if not self.messages.exists():
            return best
        for line in self.messages.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                mid = json.loads(line).get("id")
            except json.JSONDecodeError:
                continue
            if isinstance(mid, int) and mid > best:
                best = mid
        return best

    def load_position(self) -> dict | None:
        if not self.state.exists():
            return None
        try:
            return json.loads(self.state.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    # -- sleeping state ----------------------------------------------------

    def sleeping(self) -> bool:
        """Have we already told senders this seat has no agent running?"""
        return (self.root / "sleeping").exists()

    def set_sleeping(self, value: bool) -> None:
        self.ensure()
        marker = self.root / "sleeping"
        if value:
            marker.touch()
        elif marker.exists():
            marker.unlink()

    def unreachable(self) -> bool:
        return (self.root / "unreachable").exists()

    def set_unreachable(self, value: bool) -> None:
        self.ensure()
        marker = self.root / "unreachable"
        if value:
            marker.touch()
        elif marker.exists():
            marker.unlink()

    # -- the audit line ----------------------------------------------------

    def record(self, level: str, message: str) -> None:
        """Append to the durable event log.

        Warnings from contract §3 land here as well as on stderr: a daemon's
        stderr is nobody's inbox, and a warning that only ever appeared in a
        terminal nobody was watching is the silence the section exists to stop.
        """
        self.ensure()
        stamp = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
        with self.log.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {level.upper()} {message}\n")
