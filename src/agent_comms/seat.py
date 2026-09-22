"""The agent-seat application, as this client sees it.

**One interface, and this is it.** `devagent-seat-contract` 1.0: comms hands the
seat a message and reads the answer. It does not judge or analyse the state of an
agent session — not liveness, not runtime, not attendance, not session counts —
and it does not reach around the seat for anything the seat did not say.

*What this module replaced, and why the deletion is the point.* Through 0.5x this
client carried a per-runtime sender table, a pinned-conversation field, per-runtime
session counts and a four-state verdict machine, and it asked `seat status` before
delivering and then acted on the answer. Each of those was this client forming its
own opinion about something the seat owns; the last was a race — two truths with a
gap between them, and the gap is where a message is lost (contract §3). All of it
is gone rather than adapted.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

#: Delivery statuses, from the contract's table. `success` is the field to branch
#: on; these are for deciding what to do NEXT, which is this client's business.
DELIVERED = "delivered"
QUEUED = "queued"
NO_SESSION = "no-session"
FAILED = "failed"
BROKEN = "broken"
UNKNOWN = "unknown"
#: Added by devagent-seat-contract 1.1, additive: exit codes are unchanged, so
#: only a consumer branching on the STRING has to care. These are that care.
UNKNOWN_AGENT = "unknown-agent"   # exit 10 — this seat does not serve that name
CONFLICTED = "conflicted"         # exit 20 — several sessions could be it; it will not choose

#: The seat's body limit (contract: 65536 bytes, never silently truncated).
MAX_BODY_BYTES = 65536

#: Which outcomes are worth trying again, and which are not. This is the retry
#: decision the operator ruled is ours: the queue is this client's, so the seat's
#: exit status is an INPUT here, not merely a report for a human.
#:
#: - `no-session` — nothing running yet. A session may come up; the message waits.
#: - `unknown` — the seat could not ask its runtime. Not an answer, so not a no.
#: - `failed` at exit 10 — the attempt was made and did not work. Transient until
#:   proven otherwise.
#:
#: Not retried: `broken` needs a person (contract §4 — not exactly one session),
#: and retrying would spin against a state no retry can change. `failed` at exit 2
#: is a usage error — OUR defect, not the seat's, and repeating a malformed call
#: is how a bug becomes a flood.
RETRYABLE = frozenset({NO_SESSION, UNKNOWN})


#: The contract major this client is built against. comms 1.x speaks `seat msg`
#: and nothing else; a pre-1.0 seat has no such command.
REQUIRED_CONTRACT_MAJOR = "1"


class SeatTooOld(Exception):
    """This seat implements a contract older than this client can speak.

    **Ruled 2026-09-17**: comms 1.0.0 hard-requires contract 1.0 and fails loudly
    rather than carrying both paths, with the seat-then-comms upgrade ordering
    living in the deployer's runbook where it can be checked.

    Measured the same day, and the reason it must be loud: a 0.5.1 seat given
    `seat msg` prints its own help and **exits 0**. Nothing in the shell says a
    thing went wrong, and without this check the only symptom is unparseable
    output — which this client would otherwise report as a *seat defect*, blaming
    the wrong component for an ordering mistake.
    """


class SeatUnavailable(Exception):
    """The `seat` command could not be run at all — absent, or it would not answer.

    Distinct from every status the seat itself reports. A seat that answers
    `broken` is working correctly and telling us something; a seat we cannot
    invoke is a different fault with a different remedy.
    """


@dataclass
class Delivery:
    """What the seat said about one message.

    Mirrors the contract's `--json` object exactly, with nothing added and nothing
    inferred. `message` is the seat's own sentence and is shown to people verbatim
    — it is not parsed, and it is not a stable identifier.
    """

    success: bool
    status: str
    message: str = ""
    runtime: str = ""
    seat: str = ""
    exit_code: int = 0
    #: Contract 1.1: the FQN that was resolved, and the seat-local label. Empty
    #: on a 1.0 seat and on a local session — absence is not an error.
    agent: str = ""
    label: str = ""

    @property
    def retryable(self) -> bool:
        """Should this client try again later?

        `failed` is split by exit code: 10 is the seat's attempt failing, which is
        worth another go; 2 is a usage error, which is ours and never is.
        """
        if self.success:
            return False
        if self.status == FAILED:
            return self.exit_code != 2
        return self.status in RETRYABLE

    @property
    def needs_a_person(self) -> bool:
        """The seat's invariant is violated and no retry fixes it.

        Both of these are exit 20 in the contract, which is the code that means
        *a person must intervene* — `broken` since 1.0, `conflicted` added by
        1.1 when several live sessions could be one agent and the seat refuses
        to choose between them. Leaving `conflicted` out would have made it fall
        through as an ordinary refusal and gone unreported to anyone who could
        fix it.
        """
        return self.status in (BROKEN, CONFLICTED)

    def summary(self) -> str:
        """One line for a log or a sender, in the seat's own words where it has them."""
        where = self.agent or self.seat
        where = f" [{where}]" if where else ""
        return f"{self.status}: {self.message}{where}" if self.message else f"{self.status}{where}"


_contract_checked: str | None = None


def require_contract(timeout: int = 20) -> str:
    """Refuse to deliver through a seat older than contract 1.0. Returns its version.

    Asked once per process: the seat build does not change under a running daemon,
    and re-asking on every message would add a subprocess to the hot path to
    re-learn a constant.
    """
    global _contract_checked
    if _contract_checked is not None:
        return _contract_checked

    try:
        result = subprocess.run(
            ["seat", "--version", "--json"], capture_output=True, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise SeatUnavailable(
            "the `seat` command is not on this seat, so nothing can be delivered."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SeatUnavailable(f"`seat --version` did not answer within {timeout}s") from exc

    raw = (result.stdout or b"").decode("utf-8", "replace").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {}
    contract = str(payload.get("contract") or "") if isinstance(payload, dict) else ""

    if not contract.split(".")[0] == REQUIRED_CONTRACT_MAJOR:
        raise SeatTooOld(
            f"this seat implements devagent-seat-contract {contract or 'an unreadable version'}, "
            f"and agent-comms {_client_version()} requires {REQUIRED_CONTRACT_MAJOR}.x. "
            "It has no `seat msg`, so nothing can be delivered here. Upgrade the seat "
            "application first — seat-then-comms is the deployer's ordering — and note "
            "that a pre-1.0 seat answers `seat msg` by printing its help and exiting 0, "
            "so this check is the only thing that catches it."
        )

    _contract_checked = contract
    return contract


def _client_version() -> str:
    from . import __version__
    return __version__


def deliver(body: str, timeout: int = 30, agent: str | None = None) -> Delivery:
    """Hand one message to the seat. The only way this client delivers anything.

    The body goes on **stdin**, never as an argument — the seat refuses an argument
    with exit 2, and it is right to. Everything this client delivers is
    sender-authored text from the hub: backticks, `$( )`, quotes and newlines occur
    routinely, and they survive a pipe and not a command line. This client asked
    for the stdin form during the 1.0 review for exactly that reason.

    Raises `SeatUnavailable` if `seat` cannot be run. Every other outcome — including
    every failure the seat reports — comes back as a `Delivery`, because those are
    answers, not breakages.
    """
    require_contract()

    encoded = body.encode("utf-8")
    if len(encoded) > MAX_BODY_BYTES:
        # Checked here so the caller gets a Delivery rather than an exception, and
        # so the byte count is ours to report. The seat would also refuse it; this
        # saves a round trip and says the same thing.
        return Delivery(
            success=False,
            status=FAILED,
            message=(
                f"the message is {len(encoded)} bytes and the seat's limit is "
                f"{MAX_BODY_BYTES}. Not truncated — a shortened message that "
                "reported success is worse than one that did not arrive."
            ),
            exit_code=2,
        )

    # `--agent` is the ONLY way to address one agent (contract 1.1 §7.2); a
    # runtime session id is never an address. Omitting it means the seat's
    # declared default, which on a one-agent seat is today's behaviour exactly.
    command = ["seat", "msg", "--json"]
    if agent:
        command += ["--agent", agent]

    try:
        result = subprocess.run(
            command,
            input=encoded,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise SeatUnavailable(
            "the `seat` command is not on this seat, so nothing can be delivered. "
            "agent-comms talks to the agent-seat application and to nothing else "
            "(devagent-seat-contract 1.0)."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SeatUnavailable(
            f"`seat msg` did not answer within {timeout}s. The message was not "
            "delivered and has not been reported either way — it stays this "
            "client's to retry."
        ) from exc

    return _parse(result.stdout, result.stderr, result.returncode)


def _parse(stdout: bytes, stderr: bytes, code: int) -> Delivery:
    """Read the seat's answer.

    The contract promises `--json` is always valid JSON on every path including
    failures. Read leniently anyway: if it is not, that is the seat's defect to
    report, and swallowing the exit code to raise a parse error would lose the one
    fact we did get.
    """
    raw = (stdout or b"").decode("utf-8", "replace").strip()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {}

    if not isinstance(payload, dict) or "status" not in payload:
        detail = (stderr or b"").decode("utf-8", "replace").strip() or raw or "no output"
        return Delivery(
            success=False,
            status=UNKNOWN,
            message=(
                f"`seat msg --json` exited {code} without a readable answer: "
                f"{detail[:300]}. The contract requires valid JSON on every path, "
                "so this is a seat defect — raise it rather than working around it."
            ),
            exit_code=code,
        )

    return Delivery(
        success=bool(payload.get("success")),
        status=str(payload.get("status") or UNKNOWN),
        message=str(payload.get("message") or ""),
        runtime=str(payload.get("runtime") or ""),
        seat=str(payload.get("seat") or ""),
        exit_code=code,
        agent=str(payload.get("agent") or ""),
        label=str(payload.get("label") or ""),
    )


@dataclass
class SeatState:
    """`seat status` — advisory only, and never part of delivering.

    Kept for `comms doctor` and for a person asking whether a seat needs
    attention. **Never call this and then call `deliver`**: the contract forbids
    it (§3), because the two answers can disagree in the gap between them and that
    gap is where a message is lost. It is why the old ask-then-act path is gone.
    """

    answer: str = ""
    reason: str = ""
    runtime: str = ""
    seat: str = ""
    sessions: int | None = None
    remote_control_url: str = ""
    version: str = ""
    contract: str = ""
    exit_code: int = 30

    @property
    def ok(self) -> bool:
        return self.answer == "yes"

    def summary(self) -> str:
        return f"{self.answer or 'cannot tell'}: {self.reason}" if self.reason else (self.answer or "cannot tell")


def state(timeout: int = 20) -> SeatState:
    """Ask the seat how it is. Advisory — see `SeatState`."""
    try:
        result = subprocess.run(
            ["seat", "status", "--json"], capture_output=True, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise SeatUnavailable("the `seat` command is not on this seat") from exc
    except subprocess.TimeoutExpired as exc:
        raise SeatUnavailable(f"`seat status` did not answer within {timeout}s") from exc

    raw = (result.stdout or b"").decode("utf-8", "replace").strip()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    sessions = payload.get("sessions")
    return SeatState(
        answer=str(payload.get("answer") or ""),
        reason=str(payload.get("reason") or ""),
        runtime=str(payload.get("runtime") or ""),
        seat=str(payload.get("seat") or ""),
        sessions=sessions if isinstance(sessions, int) else None,
        remote_control_url=str(payload.get("remote_control_url") or ""),
        version=str(payload.get("version") or ""),
        contract=str(payload.get("contract") or ""),
        exit_code=result.returncode,
    )
