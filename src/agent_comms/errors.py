"""The failure modes the contract requires to be loud.

`agent-comms-client` 0.1 §3 commits five conditions to aborting or warning
rather than passing quietly. Each is a defect this estate has already paid for,
and they share one pattern: a check that declines to run under exactly the
condition it exists to catch. Every class here therefore carries the remedy in
its message, not just the symptom.

`CommsDisabled` is deliberately *not* an error in that sense: a seat without
comms is a seat without comms, not a broken one (§2). It exists so callers can
tell the two apart, which §3 requires of us.
"""

from __future__ import annotations


class CommsError(Exception):
    """Base for every condition that must not pass quietly."""

    #: Short machine-readable tag, used by `comms doctor` and the exit-code map.
    tag = "comms-error"


class CommsDisabled(CommsError):
    """Comms is not enabled on this seat. Not a fault — the normal resting state.

    Enablement is per seat and optional, and off unless explicitly turned on
    (§2). Callers must distinguish this from `CredentialMissing`: the two look
    identical from the filesystem, which is exactly why §3 names the ambiguity.
    """

    tag = "disabled"


class CredentialMissing(CommsError):
    """Comms is enabled but the credential is absent.

    The distinction §3 demands: enabled-without-credential is broken and says
    so; not-enabled is quiet.
    """

    tag = "credential-missing"


class CredentialUnreadable(CommsError):
    """The credential exists but cannot be used, and we say which part failed."""

    tag = "credential-unreadable"


class InsecureTransportRefused(CommsError):
    """A credential or environment asked us to skip TLS verification.

    §3: *refuse; no insecure flag exists*. The client does not expose one, and
    refuses to honour one it is handed — otherwise the estate could deliver a
    zuliprc with `insecure=true` and we would quietly comply.
    """

    tag = "insecure-transport"


class NotSubscribed(CommsError):
    """The bot is not subscribed to the channel it is supposed to watch.

    The failure mode with no symptom: registration succeeds, polling succeeds,
    and nothing ever arrives — indistinguishable from a quiet day. Verified at
    connect; refuses to start.
    """

    tag = "not-subscribed"


class LifespanUnverifiable(CommsError):
    """The server is too old to echo the queue lifespan back.

    Not raised as a failure — reported. See `agent_comms.hub` for why this is a
    warning rather than an abort, and why it must never be silent.
    """

    tag = "lifespan-unverifiable"


class QueueGapError(CommsError):
    """The event queue was garbage-collected and events in the gap are lost."""

    tag = "queue-gap"
