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

    return Plan(fqn=fqn, delivery=answer.delivery,
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

    **Declared only, since 2026-09-25.** `transports.comms` from the resolution
    answer gives the channel and the hub identity. There is no derivation.

    Two rules meet here and leave nothing to derive from:

    - **§5 (amended)**: the hub identity is the *resolved seat's* bot — the
      agent segment must never pick a hub identity. That was measured: deriving
      `bot` from the agent produced accounts the hub does not have, and a send
      "succeeded" mentioning nobody.
    - **estate-directory-resolution 0.2**, in its own words: *"There is no
      `route`, seat, host, `control`, `local_route`, `seat_local_id` or
      runtime-session field in the 0.2 result."* Deliberate, and pinned by our
      own privacy test.

    So the seat §5 wants is not in the answer §5 says to take it from. The only
    honest reading is: **use what is declared, and refuse when nothing is.**
    Guessing a seat from the FQN's shape — stripping `-new001` to get
    `test-claude` — is prefix-matching, the trap this client refuses everywhere
    else, and it would be wrong the first time a seat is named unlike its
    agents.

    A refusal here costs a message nobody could have routed. A guess costs a
    message delivered to the wrong audience, which nobody notices.
    """
    override = ((declared or {}).get("comms") or {})
    channel, bot = override.get("channel"), override.get("bot")
    if channel and bot:
        return {"channel": channel, "bot": bot}

    raise NotDeliverable(
        f"no comms transport is declared for {fqn!r}, and none can be derived.\n"
        "  The hub identity is the resolved SEAT's bot (comms-design §5, amended "
        "2026-09-25) — the agent segment must never pick one, because that "
        "produced hub accounts which do not exist.\n"
        "  But estate-directory-resolution 0.2 carries no seat in its answer, by "
        "design: 'There is no route, seat, host, control, local_route, "
        "seat_local_id or runtime-session field in the 0.2 result.'\n"
        "  So the seat is not available to derive from, and it is not guessed "
        "from the FQN's shape — that is prefix-matching, and it is wrong the "
        "first time a seat is named unlike its agents.\n"
        "  FIX: author `transports.comms` for this agent at the directory "
        "(channel + bot), which §5 already makes the winning case. Until then "
        "address the seat by name.")


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
