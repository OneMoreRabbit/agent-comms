"""Local state under `~/.comms/`.

The seat home is host-mounted, so this survives container recreation (contract
§2a, R6). Append-only JSONL for messages, a small JSON file for queue position,
so a partially-written file costs one line rather than the history.
"""

from __future__ import annotations

import json
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

    @property
    def when(self) -> str:
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.messages = root / "messages.jsonl"
        self.state = root / "state.json"
        self.log = root / "events.log"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

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

    def mark_read(self, message_id: int) -> bool:
        rows = self.all()
        found = False
        for m in rows:
            if m.id == message_id:
                m.read, found = True, True
        if found:
            self.ensure()
            tmp = self.messages.with_suffix(".jsonl.tmp")
            tmp.write_text(
                "".join(json.dumps(asdict(m), ensure_ascii=False) + "\n" for m in rows),
                encoding="utf-8",
            )
            tmp.replace(self.messages)
        return found

    # -- queue position ----------------------------------------------------

    def save_position(self, queue_id: str, last_event_id: int) -> None:
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
