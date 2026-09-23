"""One send: resolve → deliver by FQN → record.

**The division of labour, ruled by the operator 2026-09-22 and settled by
seat-router design v1.2:**

- the **directory** resolves a name to an FQN,
- the **seat** turns an FQN into a session — `FQN → seat_local_id → session id`,
- **comms delivers to an FQN and does nothing else.**

Comms never picks an agent, never resolves a session, never learns a
`seat_local_id`, and never guesses which conversation a message is for. It
passes the FQN the directory gave it and reads the answer.

**`local_route` is dead** (v1.2 §A1.2): the FQN binds to the slot directly and
no third name exists. An earlier version of this module refused to deliver when
a record carried no `local_route`. That refusal is gone — it was built on a
bridge the design has since removed.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import CommsError
from .resolve import Resolution


class NotDeliverable(CommsError):
    """Resolution did not produce an FQN, so there is nothing to deliver to."""

    tag = "not-deliverable"


@dataclass
class Plan:
    """What comms hands the seat: an FQN, and the context for the record."""

    fqn: str
    seat: str
    delivery: str
    degraded: bool
    reason: str = ""


def plan(answer: Resolution) -> Plan:
    """Turn a resolution into the one value `seat msg --agent` is given: the FQN.

    Nothing is derived. If the directory did not name the agent, comms does not
    invent a name for it — §11, and the failure mode is the one that matters:
    a message delivered into the wrong session cannot be noticed by anyone.
    """
    if not answer.success:
        raise NotDeliverable(f"{answer.status}: {answer.message or answer.requested}")

    fqn = answer.canonical_id
    if not fqn:
        raise NotDeliverable(
            f"'{answer.requested}' resolved without a canonical id, so there is no "
            "FQN to deliver to. Comms does not construct one: the estate names its "
            "agents and this client carries the name it is given."
        )

    return Plan(fqn=fqn, seat=answer.seat, delivery=answer.delivery,
                degraded=answer.degraded, reason=answer.reason)


#: Delivery modes, the estate's three. `none` refuses at send; `hold` stores
#: and never injects; `inject` goes to the session. A value outside these is
#: REFUSED, never defaulted — the seat carries it verbatim and never reads it,
#: so comms is the only thing that can catch a typo in it.
INJECT, HOLD, NONE = "inject", "hold", "none"


def permitted_to_send(mode: str) -> tuple[bool, str]:
    """May a message be sent to an agent in this delivery mode?"""
    if mode == NONE:
        return False, "this agent takes no messages (delivery: none)"
    if mode in (INJECT, HOLD):
        return True, ""
    return False, (
        f"delivery mode {mode!r} is not one of {INJECT}, {HOLD}, {NONE}. Refused "
        "rather than assumed: the seat carries this value verbatim and never reads "
        "it, so nothing else in the estate can catch a typo in it."
    )


# -- R15: transport defaults derived from the FQN -----------------------------
#
# **Empty `transports` is the NORMAL case, not a gap.** Measured on the live
# directory: our agents answer `"transports": {}`. The design's rule is
# "defaults are derived, exceptions are declared", so deriving is what makes
# delivery work at all — the directory only speaks up where reality differs.

def transport_for(fqn: str, declared: dict | None = None) -> dict:
    """Where a message to this FQN is posted, and as whom.

    `<estate>.<project>.<agent>` carries the routing rule: **project is the
    channel, agent is the bot**. A declared `transports.comms` block overrides
    either or both, and a partial override overrides only what it names —
    an exception that had to restate the defaults would drift from them.

    Raises on an FQN we cannot read rather than posting somewhere derived from
    a guess. A message in the wrong channel is not a failed delivery; it is a
    successful delivery to the wrong audience, which nobody notices.
    """
    override = ((declared or {}).get("comms") or {})
    parts = fqn.strip().split(".")
    if len(parts) != 3 or not all(parts):
        if override.get("channel") and override.get("bot"):
            return {"channel": override["channel"], "bot": override["bot"]}
        raise NotDeliverable(
            f"cannot derive a transport from {fqn!r}: an FQN is "
            "<estate>.<project>.<agent>, three non-empty dot-separated segments. "
            "Nothing is guessed — a message posted to a derived-from-nonsense "
            "channel is not a failed delivery, it is a successful delivery to the "
            "wrong audience, and nobody notices that."
        )
    _estate, project, agent = parts
    return {"channel": override.get("channel") or project,
            "bot": override.get("bot") or agent}


#: **Caller-relative shorthands are never resolvable here** (arch ruling,
#: 2026-09-22, restated 2026-09-23). Two sets, only one authored:
#:
#: - **Estate-scoped** names — `orchestrator`, `atlas` — have exactly one
#:   referent estate-wide and ARE authored as aliases. The directory resolves
#:   them.
#: - **Caller-relative** shorthands — bare `arch`, bare `product`,
#:   `<project>:arch` — are NEVER authored, because `arch` exists in nine
#:   projects and the right one depends on WHO ASKS. A global alias would
#:   resolve eight of them wrongly.
#:
#: So this client derives against FQNs and authored aliases only, and never
#: against a shorthand. 0.2 answering `unknown` for bare `arch` is the correct
#: interim until the resolver's caller-relative logic exists — not a gap, and
#: not ours to paper over by guessing the caller's project.
CALLER_RELATIVE_NEVER_AUTHORED = True
