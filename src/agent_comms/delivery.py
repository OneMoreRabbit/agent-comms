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
