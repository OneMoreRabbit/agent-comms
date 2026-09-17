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
from .seat import Delivery, SeatTooOld, SeatUnavailable

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
    return (
        f"[hub message from {sender} — topic '{topic}'] {body} "
        f"[cite {permalink} | reply: comms reply {mid} '<text>']"
    )


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
    try:
        return seat_app.deliver(compose_turn(mention))
    except (SeatUnavailable, SeatTooOld) as exc:
        raise WakeError(str(exc)) from exc
