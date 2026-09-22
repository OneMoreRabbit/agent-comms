"""One-shot import of the 1.0.0 JSONL store into the 2.0 SQLite store.

**The JSONL is kept.** It is the backup it already was, and the evidence for
the September incident was reconstructed from it.

Two facts in the old store do not map cleanly, and this importer refuses to
guess at either. Both were measured on this seat's real store, 118 messages:

1. **`authorised: False` and `delivered: True` occur together — on all 15 of
   them.** 1.0.0 stored a refused sender's message and showed it in the inbox
   so the agent could report it (ADR-0009 §1a), and marked it delivered because
   it had been *handled*. Under the new states those are two different words.
   The permission event is the one that is unambiguous, so it wins: the message
   imports as `refused`, and the transition row records that the source also
   said delivered. **Nothing is lost and nothing is invented.**

2. **`delivered: False` is not one fact.** It mixes never-attempted with
   delivered-but-never-recorded — the flood's own signature, where a wake that
   queued left a message unmarked forever. The JSONL cannot tell them apart, so
   they import as `expired` with cause `migration`. An honest overcount of
   expiry is safe; an optimistic `delivered` writes September into the new
   store on its first day.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .queue import EXPIRED, QUEUED, RECEIVED, REFUSED, Queue


@dataclass
class Imported:
    """What the import did, printed so a person can reconcile it."""

    read: int = 0
    delivered: int = 0
    refused: int = 0
    expired: int = 0
    skipped: list[str] = field(default_factory=list)

    @property
    def written(self) -> int:
        return self.delivered + self.refused + self.expired

    def report(self, source: Path, into: Path) -> str:
        lines = [
            f"read      {self.read} message(s) from {source}",
            f"written   {self.written} into {into}",
            f"  delivered  {self.delivered}",
            f"  refused    {self.refused}   (sender not permitted at the time)",
            f"  expired    {self.expired}   (undelivered; cause: migration)",
        ]
        if self.skipped:
            lines.append(f"  skipped    {len(self.skipped)}   {self.skipped[:3]}")
        lines.append(f"RECONCILE: {self.read} read, {self.written} written, "
                     f"{self.read - self.written - len(self.skipped)} unaccounted for")
        lines.append(f"The JSONL is untouched at {source} and remains the backup.")
        return "\n".join(lines)


def import_jsonl(source: Path, queue: Queue) -> Imported:
    """Read the 1.0.0 store into `queue`. Idempotent on the hub message id."""
    out = Imported()
    for line in Path(source).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            old = json.loads(line)
        except json.JSONDecodeError:
            out.skipped.append(line[:60])
            continue
        out.read += 1

        hub_id = str(old.get("id", ""))
        when = old.get("timestamp")
        received_at = (datetime.fromtimestamp(when, tz=timezone.utc).isoformat(timespec="seconds")
                       if isinstance(when, (int, float)) else None)

        message = queue.receive(
            hub_id=hub_id, sender=str(old.get("sender") or "unknown"),
            body=str(old.get("content") or ""), subject=str(old.get("topic") or ""),
            permalink=str(old.get("permalink") or ""), received_at=received_at)

        if queue.state_of(message) != RECEIVED:
            continue                                  # already imported

        if old.get("authorised") is False:
            # The permission event wins — see this module's docstring.
            also = " (the 1.0.0 record also said delivered)" if old.get("delivered") else ""
            queue.move(message, REFUSED,
                       f"migration: sender was not permitted at the time{also}",
                       rule="authorised=false")
            out.refused += 1
        elif old.get("delivered"):
            queue.move(message, QUEUED, "migration: was delivered under 1.0.0")
            queue.record_attempt(message, delivered=True,
                                 detail="migration: 1.0.0 recorded this as delivered")
            out.delivered += 1
        else:
            queue.move(message, EXPIRED,
                       "migration: undelivered under 1.0.0, and the JSONL cannot say "
                       "whether it was never attempted or delivered-but-unrecorded",
                       rule="migration")
            out.expired += 1

    return out
