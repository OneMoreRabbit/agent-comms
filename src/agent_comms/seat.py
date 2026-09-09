"""The seat surface — `devagent-seat-contract` 0.5.

One question, one owner, one answer: **can this seat be spoken to?** The seat
answers it; we do not. Everything this client used to infer — pane scans,
process trees, readiness heuristics, codex loaded-thread guesses — is gone,
replaced by `seat status --json` (ADR-0011).

The hard rule that keeps the seam real: **we never second-guess the verdict.**
If it says addressable and delivery fails, that is a bug reported to
`agent-skeleton`, not one worked around here. A quiet local fallback would
rebuild the four inference layers behind a nicer facade.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field

#: Exit codes from `seat status`, per contract 0.5.
ADDRESSABLE = 0
NOT_ADDRESSABLE = 10
BROKEN = 20
UNDETERMINED = 30


@dataclass
class SeatStatus:
    """What the seat says about itself."""

    verdict: str
    reason: str = ""
    runtime: str = ""
    #: Live but not attending. `None` means the seat did not say.
    awake: bool | None = None
    #: Where to send: a tmux target for claude, the thread id for codex.
    target: str = ""
    pinned: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def addressable(self) -> bool:
        return self.verdict == "addressable"

    @property
    def deliverable(self) -> bool:
        """Addressable *and* attending.

        Operator ruling, 2026-09-09: hold delivery until wake. A sleeping
        session is deliverable in principle and not now, so a message for it
        waits — which is what this client already does for a dormant seat.
        """
        return self.addressable and self.awake is not False

    def hold_reason(self) -> str:
        """Why a message is being held, in the seat's own words where it has any."""
        if self.addressable and self.awake is False:
            return (
                f"the {self.runtime or 'agent'} session is asleep — addressable but not "
                "attending. Held until it wakes; waking is the operator's (ADR-0009 §7e)."
            )
        detail = self.reason or "no reason given"
        return f"the seat reports {self.verdict}: {detail}"


class SeatUnavailable(Exception):
    """This seat has no `seat` command, so nothing can be confirmed.

    Not a fallback trigger. A seat that cannot answer is a seat we do not
    deliver into — the message is held and says why, exactly as it is for a
    seat that answers "not addressable".
    """


def seat_available() -> bool:
    return shutil.which("seat") is not None


def status(timeout: int = 20) -> SeatStatus:
    """Ask the seat whether it can be spoken to.

    Anything we cannot read cleanly is `undetermined`, which the contract is
    explicit must be treated exactly as `not-addressable`. Every failure in the
    catalogue began as something undetermined and treated as fine.
    """
    if not seat_available():
        raise SeatUnavailable(
            "this seat has no `seat` command, so it cannot say whether it can receive "
            "a message. It needs the agent-skeleton image carrying "
            "devagent-seat-contract 0.5. Holding the message until it does."
        )

    try:
        result = subprocess.run(
            ["seat", "status", "--json"],
            text=True, capture_output=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SeatStatus(verdict="undetermined", reason=f"`seat status` did not run: {exc}")

    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return SeatStatus(
            verdict="undetermined",
            reason=(
                "`seat status --json` did not return readable JSON "
                f"(exit {result.returncode}): "
                f"{(result.stderr or result.stdout or '').strip()[:200]}"
            ),
        )

    verdict = payload.get("verdict") or "undetermined"
    awake = payload.get("awake")
    return SeatStatus(
        verdict=verdict,
        reason=payload.get("reason") or "",
        runtime=payload.get("runtime") or "",
        awake=awake if isinstance(awake, bool) else None,
        target=payload.get("target") or "",
        pinned=payload.get("pinned"),
        raw=payload,
    )
