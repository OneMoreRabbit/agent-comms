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
import os
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
class Persistence:
    """What the seat says about how long its session lasts.

    Declared in `~/.seat/session.yml` (contract 0.5). We asked for this in
    ADR-0011 step 0 because it decides what "queued" means to a sender —
    minutes, or until somebody notices — and nothing else can tell them.
    """

    survives: tuple[str, ...] = ()
    lost_on: tuple[str, ...] = ()

    def summary(self) -> str:
        if not self.survives and not self.lost_on:
            return ""
        parts = []
        if self.survives:
            parts.append(f"survives {', '.join(self.survives)}")
        if self.lost_on:
            parts.append(f"lost on {', '.join(self.lost_on)}")
        return "; ".join(parts)


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


#: The seat contract this client is written against. Consumed per its own
#: guidance: compare on `.seat`, because a build below 0.3.1 could misreport the
#: contract it implements — 0.3.0 shipped saying `contract 0.5` after the
#: renumber, having held the two as separate literals.
MINIMUM_SEAT = "0.3.3"


@dataclass
class SeatVersion:
    """Which build of `seat` this is. Contractual from 0.3.3."""

    seat: str = ""
    contract: str = ""
    raw: str = ""

    @property
    def known(self) -> bool:
        return bool(self.seat)

    def below(self, minimum: str = MINIMUM_SEAT) -> bool:
        """Is this build older than the one this client is written against?

        Compared as integer tuples, so `0.3.10` sorts above `0.3.9` — the trap
        that made us propose `0.3.0` for our own release when we were at 0.17.
        An unparseable version is not treated as old: it is unknown, and the
        caller says so rather than acting on a guess.
        """
        def parts(v: str) -> tuple[int, ...] | None:
            try:
                return tuple(int(x) for x in v.split("."))
            except ValueError:
                return None

        mine, theirs = parts(self.seat), parts(minimum)
        if mine is None or theirs is None:
            return False
        return mine < theirs

    def summary(self) -> str:
        if not self.known:
            return "seat build unknown — `seat --version` did not report one"
        line = f"seat {self.seat} (contract {self.contract or 'unstated'})"
        if self.below():
            line += (
                f" — older than {MINIMUM_SEAT}, which this client is written against. "
                "It will still work: 0.3.3 changed no calls or fields. What it cannot "
                "do is guarantee a codex seat declares a thread id rather than a tmux "
                "target, which is the corruption 0.3.2 fixed."
            )
        return line


def version(timeout: int = 20) -> SeatVersion:
    """Ask the seat which build it is.

    Read leniently on purpose. From 0.3.1 `--json` emits the object and nothing
    else, but 0.3.0 printed the two prose lines *first* and the object after —
    so during a staged rollout both shapes are on real seats at once. Scanning
    for the object rather than parsing the whole stream reads either.

    That is tolerance of an older build, not a workaround of a live defect: the
    defect is fixed, and this exists so a half-upgraded estate reports honestly
    instead of reporting nothing.
    """
    if not seat_available():
        raise SeatUnavailable(
            "this seat has no `seat` command, so it cannot say which build it is."
        )
    try:
        result = subprocess.run(
            ["seat", "--version", "--json"],
            text=True, capture_output=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SeatVersion(raw=f"`seat --version` did not run: {exc}")

    out = (result.stdout or "").strip()
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            return SeatVersion(
                seat=str(payload.get("seat") or ""),
                contract=str(payload.get("contract") or ""),
                raw=out,
            )

    # No object anywhere: fall back to the guaranteed plain form, `seat <v>`.
    for line in out.splitlines():
        if line.strip().startswith("seat "):
            return SeatVersion(seat=line.strip().split()[1], raw=out)
    return SeatVersion(raw=out)


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


def persistence(path: str | None = None) -> Persistence:
    """Read the seat's declared session persistence.

    Parsed by hand rather than with PyYAML: the two keys are flat lists in a
    file the seat owns and documents. Absence is not an error — an older seat
    simply does not say, and a sender is told less rather than told wrongly.
    """
    path = path or os.path.join(os.path.expanduser("~"), ".seat", "session.yml")
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return Persistence()

    found, section = {}, False
    for line in text.splitlines():
        if line.startswith("persistence:"):
            section = True
            continue
        if section:
            if line and not line.startswith((" ", "\t")):
                break
            stripped = line.strip()
            for key in ("survives", "lost_on"):
                if stripped.startswith(f"{key}:"):
                    raw = stripped.split(":", 1)[1].strip().strip("[]")
                    found[key] = tuple(
                        v.strip().strip("'\"") for v in raw.split(",") if v.strip()
                    )
    return Persistence(survives=found.get("survives", ()), lost_on=found.get("lost_on", ()))
