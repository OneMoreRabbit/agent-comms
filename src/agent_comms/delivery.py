"""One send, end to end: resolve → permit → deliver → record.

**The division of labour, ruled by the operator 2026-09-22:**

- the **directory** resolves a name to an FQN and a route,
- the **seat** turns a route into a session,
- **comms delivers to an FQN and does nothing else.**

Comms never picks an agent, never resolves a session, and never guesses which
conversation a message is for. It carries what the directory told it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import CommsError
from .resolve import Resolution


class NoRouteToPass(CommsError):
    """Resolved, but the answer carries nothing the seat will accept.

    **Measured 2026-09-22.** `seat msg --agent` refuses an estate FQN:

        bakehouse.agent-eco.test-claude        -> unknown-agent, exit 10
        bakehouse.agent-eco.test-claude.main   -> unknown-agent, exit 10
        test-claude.main                       -> resolved

    The bridge between the two vocabularies is `route.local_route`, and it is
    **null on all 32 records in the live register today**. So a resolution can
    succeed and still leave nothing to deliver with.

    This fails loudly rather than deriving a route from the FQN. Deriving would
    be §11 exactly — a plausible value nobody declared — and it would be wrong
    in the way that matters: `local_route` is a NAMING CHOICE the estate makes,
    not a fact recoverable from the name. Two seats may spell the same agent
    differently and both be right. Guessing would deliver to whatever happened
    to match, and a message in the wrong session is worse than one that did not
    arrive, because nobody can tell it happened.
    """

    tag = "no-route-to-pass"


@dataclass
class Plan:
    """What comms will hand the seat, and why."""

    fqn: str
    route: str
    seat: str
    delivery: str
    degraded: bool
    reason: str = ""


def plan(answer: Resolution) -> Plan:
    """Turn a resolution into the one value `seat msg --agent` is given.

    `local_route` is what the seat accepts. Nothing else is substituted for it.
    """
    if not answer.success:
        raise NoRouteToPass(
            f"{answer.status}: {answer.message or answer.requested}")

    if not answer.local_route:
        raise NoRouteToPass(
            f"'{answer.canonical_id or answer.requested}' resolved to seat "
            f"'{answer.seat or 'unknown'}' but the record carries no "
            "`local_route`, and `seat msg --agent` refuses an estate FQN "
            "(measured: unknown-agent, exit 10). There is nothing to deliver "
            "with. This is not derived from the FQN — a local route is a naming "
            "choice the estate makes, not a fact recoverable from the name, and "
            "a message in the wrong session is worse than one that did not "
            "arrive. Population must author `local_route`."
        )

    return Plan(fqn=answer.canonical_id or answer.requested,
                route=answer.local_route, seat=answer.seat,
                delivery=answer.delivery, degraded=answer.degraded,
                reason=answer.reason)
