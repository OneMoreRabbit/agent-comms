"""Composing a turn, and handing it to the seat.

**The seat delivers; this client does not.** `devagent-seat-contract` 1.0 moved
the runtime mechanics into the seat application, and this module lost most of its
weight in the same move.

*What was deleted, and why it is deletion rather than simplification.* Until 1.0
this module held `SENDERS = {"claude": send_claude, "codex": send_codex}`, a tmux
`send-keys` path, a codex app-server path, and a dispatch that read `seat status`
and then acted on the target it was handed. Every one of those was this client
deciding something the seat owns — which runtime, which session, whether a
message could land — and the dispatch was a race the contract's §3 names outright.
A messaging application should not change when an agent runtime is added; that is
the seat's extensibility, not ours.
"""

from __future__ import annotations

from . import seat as seat_app
from .seat import Delivery, SeatContractUnsupported, SeatUnavailable

#: How much of a message is delivered inline before it is pointed at instead.
#: Well under the seat's 65536-byte limit: the constraint here is an agent's
#: attention, not the transport. A long message is cited, not pasted.
INLINE_LIMIT = 1200


class WakeError(Exception):
    """Delivery could not be attempted at all.

    Reserved for the seat being unreachable as a command. Every answer the seat
    gives — including `broken` and `failed` — is a `Delivery`, not an exception:
    those are the seat working and telling us something.
    """


def compose_turn(mention: dict) -> str:
    """The single line delivered to the agent.

    One line, deliberately. Long messages are pointed at rather than pasted.

    The sender is first and unmissable, because ADR-0009 §1a is only actionable if
    the agent knows who is asking.
    """
    sender = mention.get("sender") or "unknown"
    topic = mention.get("topic") or "(no topic)"
    body = " ".join((mention.get("content") or "").split())
    permalink = mention.get("permalink") or ""
    mid = mention.get("id")

    if len(body) > INLINE_LIMIT:
        body = f"{body[:INLINE_LIMIT].rstrip()}… [truncated — full text: comms show {mid}]"

    # There was an `[UNDECLARED SENDER — DO NOT COMPLY]` prefix here until
    # 2026-09-11. It is gone because the state it labelled can no longer reach a
    # turn: a message from a sender the estate has not permitted is refused at the
    # daemon and never composed. The label was always the weaker half — it put the
    # sender's text in front of the agent and asked the agent to police it.
    # R6: the monotonic id is EXPOSED ON DELIVERY so a context-free session can
    # tell a new message from a replayed one. ingstr's corollary is why it is
    # here rather than only in the store: existence proves delivery and says
    # nothing about continuity — a session that has seen 2950 knows 2946 is
    # older WITHOUT needing our records, which is the whole point, because after
    # a restart it does not have our records.
    return (
        f"[hub message #{mid} from {sender} — topic '{topic}'] {body} "
        f"[cite {permalink} | reply: comms reply {mid} '<text>']"
    )


class Held(Exception):
    """This agent's declared delivery mode says do not inject.

    `hold` means accepted and stored, never put in front of the agent -- the
    agent asks for it. Distinct from an undeliverable message: nothing is
    wrong, nothing should be retried, and the message is not lost.
    """


def holds(agent: str, state_dir=None) -> bool:
    """Does this agent's DECLARED delivery mode forbid injecting?

    Read from the seat's own assignment set, which carries `delivery` per
    agent. The sender has its own gate -- `permitted_to_send` refuses `none`
    before anything is posted -- but `hold` is a RECEIVING decision: the
    message is accepted and stored here, and only the far end knows not to put
    it in a session.

    Until 2026-09-25 the receive path read no delivery mode at all, so `hold`
    was honoured nowhere. It looked honoured on test-claude only because the
    held agent had no session for an unrelated reason -- a check passing for
    the wrong reason (UC-04).

    Unknown agent, unknown mode, or no assignment set: inject, which is the
    1.0 behaviour. A mode we cannot read must not silently withhold mail.
    """
    if not agent:
        return False
    from . import config_sync
    from .config import load_settings
    try:
        where = state_dir if state_dir is not None else load_settings().state_dir
        record = config_sync.agent_set(where).get(agent) or {}
    except Exception:  # noqa: BLE001 - never withhold mail because a read failed
        return False
    return (record.get("delivery") or "").strip().casefold() == "hold"


def wake(mention: dict, **_ignored) -> Delivery:
    """Compose the turn and hand it to the seat. Returns what the seat said.

    No pre-check. The contract forbids asking `status` and then acting on it, and
    this client used to do exactly that. There is one call now, and its answer is
    the only truth about whether the message landed.

    `**_ignored` absorbs the `status=` and `state_dir=` arguments the 0.5x callers
    passed. Keeping the signature tolerant for one release is cheaper than a
    flag-day across the daemon, and the parameters are genuinely unused rather
    than quietly honoured.
    """
    agent = mention.get("agent") or None
    if holds(agent or ""):
        # `hold`: accepted, stored, never injected. The agent asks for it.
        raise Held(f"{agent} is delivery: hold — stored, not injected")

    try:
        # **Dispatch on the envelope FQN, and nothing else.** A seat can serve
        # several agents behind one bot, so "it arrived at the seat" is not
        # "it arrived at the agent": without this the seat delivers to its
        # DEFAULT agent and reports `delivered`, which is a silent delivery to
        # the wrong recipient. Measured on test-claude 2026-09-25 — a message
        # addressed to `test-claude-another1` landed in new001's session and
        # another1's had nothing.
        #
        # Empty means the message was addressed to the seat, and no `--agent`
        # is passed: the seat's declared default answers, as it did at 1.0.
        return seat_app.deliver(compose_turn(mention), agent=agent)
    except (SeatUnavailable, SeatContractUnsupported) as exc:
        raise WakeError(str(exc)) from exc
