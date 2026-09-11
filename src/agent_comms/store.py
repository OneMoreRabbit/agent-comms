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
    #: Is the sender one this seat accepts direction from (ADR-0009 §9)?
    #: An unauthorised message is still stored and shown — the agent must be able
    #: to report it — but it is never presented as an instruction.
    authorised: bool = True

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

        return DaemonState(running=running, pid=pid if running else None, last_tick=last_tick)

    def acquire_daemon_lock(self):
        """Take the single-daemon lock, or raise `DaemonAlreadyRunning`.

        An advisory `flock` on a file in the state directory. The OS releases it
        when the process ends however it ends, so a killed daemon leaves no stale
        lock to clear by hand — which a PID file would.
        """
        from .errors import DaemonAlreadyRunning

        self.ensure()
        lock_path = self.root / "daemon.lock"
        handle = lock_path.open("w", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise DaemonAlreadyRunning(
                f"another comms daemon already holds {lock_path}. Two daemons on one bot "
                "means two event queues, so every mention would be stored and handed to "
                "notify_command twice. Replace it in one step: comms daemon --restart"
            ) from None
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

    def mark_delivered(self, message_id: int) -> bool:
        rows = self.all()
        found = False
        for m in rows:
            if m.id == message_id:
                m.delivered, found = True, True
        if found:
            self._rewrite(rows)
        return found

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

    def sleeping_waiting(self) -> bool:
        """Have we already told senders their messages are waiting to be read?

        Distinct from `sleeping`: that one means nothing was delivered at all.
        This one means delivery worked and nothing is running to read it — a
        pinned codex thread with no session. Two different things to be told,
        and each is said once.
        """
        return (self.root / "waiting").exists()

    def set_sleeping_waiting(self, value: bool) -> None:
        self.ensure()
        marker = self.root / "waiting"
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
