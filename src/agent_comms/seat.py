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

#: Exit codes from `seat awake`.
AWAKE_YES = 0
AWAKE_NO = 1
AWAKE_UNKNOWN = 2


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

        **`awake` here comes from `seat awake`, not from `seat status`'s field.**
        They are two commands answering two questions, and only the dedicated one
        asks the runtime. Reading it off `status` was our mistake: on codex that
        field is set by a branch that never reaches the app-server query, so a
        live thread read asleep and every codex seat looked undeliverable.
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


@dataclass
class Awake:
    """What `seat awake` says. Exit 0 yes, 1 no, 2 cannot tell."""

    state: bool | None
    reason: str = ""

    @property
    def holds(self) -> bool:
        """Anything but a clear yes holds.

        "Cannot tell" is treated as "no", for the reason the contract gives for
        `undetermined`: every failure in the catalogue began as something nobody
        could determine and was treated as fine.
        """
        return self.state is not True


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


def awake(timeout: int = 20) -> Awake:
    """Ask the seat whether its agent is awake.

    A separate command from `status` because it is a separate question, and the
    only one that asks the runtime directly — `claude agents` for claude,
    `seat-codex-query` (the app-server's loaded-thread list) for codex.
    """
    if not seat_available():
        raise SeatUnavailable(
            "this seat has no `seat` command, so it cannot say whether its agent is "
            "awake. It needs the agent-skeleton image carrying "
            "devagent-seat-contract 0.5."
        )

    try:
        result = subprocess.run(
            ["seat", "awake", "--json"],
            text=True, capture_output=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Awake(state=None, reason=f"`seat awake` did not run: {exc}")

    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return Awake(
            state=None,
            reason=(
                f"`seat awake --json` did not return readable JSON "
                f"(exit {result.returncode}): "
                f"{(result.stderr or result.stdout or '').strip()[:200]}"
            ),
        )

    value = payload.get("awake")
    return Awake(
        state=value if isinstance(value, bool) else None,
        reason=payload.get("reason") or "",
    )
