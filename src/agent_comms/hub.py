"""Transport: the outbound connection to the hub, and the checks around it.

Delivery is outbound from the seat (ADR-0009 §7). The client opens a long-lived
request and the server completes it when an event arrives; nothing listens on
the seat and nothing is reachable from outside. There is no webhook path and
there will not be one.

Everything here exists to keep the four connect-time commitments in contract §3
honest. The interesting one is `verify_lifespan`: see its docstring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import (
    LIFESPAN_ECHO_FEATURE_LEVEL,
    SOURCE_VERIFIED_FEATURE_LEVEL,
    Credential,
    Settings,
)
from .errors import NotSubscribed, QueueGapError


class Transport(Protocol):
    """The Zulip surface this client uses. Narrow on purpose, so it can be faked."""

    def call_endpoint(self, url: str, method: str = "GET", request: dict | None = None) -> dict: ...

    def register(self, **kwargs: Any) -> dict: ...

    def get_events(self, **kwargs: Any) -> dict: ...


@dataclass
class Registration:
    """The result of registering an event queue, plus what we could verify of it."""

    queue_id: str
    last_event_id: int
    feature_level: int | None = None
    zulip_version: str | None = None
    #: The lifespan the server echoed back, if it is new enough to echo one.
    echoed_lifespan: int | None = None
    #: Non-fatal findings raised at connect. Never empty-and-silent: a caller
    #: that ignores these is defeating the point of contract §3.
    warnings: list[str] = field(default_factory=list)
    #: Things worth recording but not worth interrupting anyone for. The
    #: difference matters: a warning that fires every time stops being read, and
    #: takes the real ones with it.
    notes: list[str] = field(default_factory=list)


class Hub:
    """Connect-time verification and the event loop's transport half."""

    def __init__(self, transport: Transport, settings: Settings, credential: Credential) -> None:
        self._t = transport
        self._settings = settings
        self._credential = credential

    # -- §3: bot not subscribed to its channel -----------------------------

    def verify_subscription(self) -> None:
        """Refuse to start if the bot is not subscribed to its channel.

        This is the failure mode with no symptom. An unsubscribed bot registers
        successfully and long-polls successfully and receives nothing at all,
        which is indistinguishable from a quiet day. The estate subscribes at
        mint time, so a failure here means the bootstrap did not complete — and
        it must be visible immediately, not discovered a week later.
        """
        result = self._t.call_endpoint(url="users/me/subscriptions", method="GET")
        if result.get("result") != "success":
            raise NotSubscribed(
                "could not list this bot's channel subscriptions "
                f"({result.get('msg') or result!r}). Refusing to start: an unverified "
                "subscription is the one failure that looks exactly like silence."
            )
        wanted = self._settings.channel
        names = {s.get("name") for s in result.get("subscriptions", [])}
        if wanted not in names:
            raise NotSubscribed(
                f"bot '{self._settings.identity.bot_name}' is not subscribed to channel "
                f"'{wanted}' (subscribed to: {', '.join(sorted(n for n in names if n)) or 'nothing'}). "
                "Refusing to start. It would otherwise register and poll successfully and "
                "receive nothing, which is indistinguishable from a quiet day. The estate "
                "subscribes the bot at mint time, so this means the bootstrap did not "
                "complete — ask for it rather than working around it."
            )

    # -- attribution: is this bot who the vault thinks it is? --------------

    def verify_identity(self) -> list[str]:
        """Check the bot's name is one ADR-0009 §7a recognises.

        Attribution is the only thing making a topology breach visible, since
        §1's hierarchy is convention rather than server-enforced. §7a ruled that
        what the name must guarantee is **unambiguity in every channel the bot
        appears in**, not a fixed pattern: a component bot may be `<seat>`, an
        arch bot carries its project as `<project>-<seat>`. Either is correct;
        anything else is reported, not refused.
        """
        accepted = self._settings.identity.canonical_names(self._settings.role)
        result = self._t.call_endpoint(url="users/me", method="GET")
        if result.get("result") != "success":
            return [f"could not read this bot's own identity ({result.get('msg') or result!r})"]

        notices: list[str] = []
        actual = result.get("full_name") or ""
        if actual not in accepted:
            expected = " or ".join(f"'{n}'" for n in accepted)
            notices.append(
                f"bot is named '{actual}', and this {self._settings.role} seat's canonical "
                f"name is {expected}. ADR-0009 §7a requires a bot's name to be unambiguous "
                "in every channel it appears in; this one does not identify the seat it "
                "speaks for, so a message from it cannot be traced back by name alone."
            )
        if not result.get("is_bot"):
            notices.append(
                f"credential belongs to a human account ({result.get('email')}), not a bot. "
                "Every message would be attributed to a person who did not send it."
            )
        return notices

    # -- §3: lifespan_secs not honoured ------------------------------------

    def verify_lifespan(self, registration: Registration) -> None:
        """Confirm the server honours the lifespan we asked for — or say it cannot.

        This check has three branches because there are three genuinely different
        situations, and an earlier version collapsed two of them into a warning
        that fired on every connect on every seat. That is the failure §3 is
        about, committed by §3's own machinery: a warning nobody can act on and
        everybody learns to skip.

        1. **Feature level ≥ 481** — the server echoes the effective lifespan
           (`idle_queue_timeout_secs`, Zulip 12.0+). Read it back; warn on
           mismatch, and warn if a server that should echo did not.
        2. **The estate's pinned server** — Zulip 10.4, feature level 372. It
           cannot echo, but the estate read `zerver/tornado/event_queue.py` on
           the running install and confirmed `lifespan_secs` is client-set with
           no server-side cap (`agent-comms-hub-response` 0.2). The property is
           verified; it was simply verified once, at the source, by the party who
           owns the server, rather than per-connect by us. **Silent.**
        3. **Anything else** — a server that can neither echo nor claim the
           estate's source verification. Warn, because that is the case where
           nobody has actually checked.
        """
        want = self._settings.lifespan_secs
        level = registration.feature_level

        if level is not None and level >= LIFESPAN_ECHO_FEATURE_LEVEL:
            echoed = registration.echoed_lifespan
            if echoed is None:
                registration.warnings.append(
                    f"lifespan unverified: server reports feature level {level}, which should "
                    "echo the effective queue lifespan, but no value came back. Treating as "
                    "unverified rather than assuming it was honoured."
                )
            elif echoed != want:
                registration.warnings.append(
                    f"lifespan mismatch: asked for {want}s, server allocated {echoed}s. The "
                    "offline window before events are lost is shorter than this client "
                    "assumes. Raise it with the estate rather than adjusting silently."
                )
            return

        if level == SOURCE_VERIFIED_FEATURE_LEVEL:
            registration.notes.append(
                f"lifespan {want}s: this server (Zulip {registration.zulip_version or '?'}, "
                f"feature level {level}) cannot echo it back, but the estate verified in the "
                "running server's source that lifespan_secs is client-set with no cap "
                "(agent-comms-hub-response 0.2). Honoured."
            )
            return

        registration.warnings.append(
            f"lifespan unverified: asked for lifespan_secs={want}, but this server "
            f"(Zulip {registration.zulip_version or '?'}, feature level {level}) neither "
            f"echoes the effective lifespan — that arrived at feature level "
            f"{LIFESPAN_ECHO_FEATURE_LEVEL} — nor is the level the estate source-verified "
            f"({SOURCE_VERIFIED_FEATURE_LEVEL}). Nobody has checked this combination. If the "
            "server silently fell back to its 600s default, the first symptom would be lost "
            "events after a short outage."
        )

    # -- the queue itself --------------------------------------------------

    def register_queue(self) -> Registration:
        """Register an event queue for messages, at the contracted lifespan."""
        result = self._t.register(
            event_types=["message"],
            lifespan_secs=self._settings.lifespan_secs,
        )
        if result.get("result") != "success":
            raise QueueGapError(
                f"could not register an event queue: {result.get('msg') or result!r}"
            )
        registration = Registration(
            queue_id=result["queue_id"],
            last_event_id=result["last_event_id"],
            feature_level=result.get("zulip_feature_level"),
            zulip_version=result.get("zulip_version"),
            echoed_lifespan=result.get("idle_queue_timeout_secs"),
        )
        self.verify_lifespan(registration)
        return registration

    def resume(self, queue_id: str, last_event_id: int) -> Registration:
        """Rebuild a Registration from a stored position, without re-registering.

        A daemon restart inside the lifespan window should resume rather than
        re-register: re-registering silently forfeits anything sent while it was
        down, which is the gap §3 asks us to report rather than create.
        """
        return Registration(queue_id=queue_id, last_event_id=last_event_id)

    def get_events(self, registration: Registration) -> list[dict]:
        """Fetch the next batch, raising `QueueGapError` if the queue was collected.

        A collected queue means events are gone. §3: re-register and report the
        gap and its window — the silence is the danger, not the gap.
        """
        result = self._t.get_events(
            queue_id=registration.queue_id,
            last_event_id=registration.last_event_id,
        )
        if result.get("result") == "error" and result.get("code") == "BAD_EVENT_QUEUE_ID":
            raise QueueGapError(
                f"event queue {registration.queue_id} was garbage-collected; any messages "
                f"sent since the last event are lost. Window: up to "
                f"{self._settings.lifespan_secs}s of inactivity."
            )
        if result.get("result") != "success":
            raise QueueGapError(f"event fetch failed: {result.get('msg') or result!r}")

        events = result.get("events", [])
        if events:
            registration.last_event_id = max(e["id"] for e in events)
        return events

    # -- sending -----------------------------------------------------------

    # -- who can be addressed ----------------------------------------------

    def realm_names(self) -> list[str]:
        """Every active account the hub knows, as it spells them.

        Existence is the hub's answer, not a pattern we recognise: `@**name**`
        for a name the realm does not hold renders as literal text, and the post
        succeeds. The sender cannot see that, which is why it is checked here.
        """
        users = self._t.call_endpoint(url="users", method="GET")
        if users.get("result") != "success":
            raise NotSubscribed(
                f"could not list realm users ({users.get('msg') or users!r}); refusing to "
                "post a mention that may not resolve."
            )
        return sorted(
            u["full_name"] for u in users.get("members", [])
            if u.get("is_active") and u.get("full_name")
        )

    def addressable_names(self) -> list[str]:
        """The seats this seat can address: in the realm **and** in this channel.

        The second half is the one that bites. Measured on this hub:
        `blocks-android` is a real bot and is *not* subscribed to `agent-eco`, so
        a mention of it from here renders perfectly and reaches nobody — which is
        indistinguishable from success. That is the failure of 2026-09-10.

        The realm is the roster. There is no second list to maintain, and nothing
        here is inferred from a name's shape.
        """
        subs = self._t.call_endpoint(url="users/me/subscriptions", method="GET")
        if subs.get("result") != "success":
            raise NotSubscribed(
                f"could not list this bot's subscriptions ({subs.get('msg') or subs!r}), "
                "so who is reachable in this channel cannot be established."
            )
        stream_id = next(
            (s.get("stream_id") for s in subs.get("subscriptions", [])
             if s.get("name") == self._settings.channel),
            None,
        )
        if stream_id is None:
            raise NotSubscribed(
                f"this bot is not subscribed to channel '{self._settings.channel}', so it "
                "cannot address anyone in it."
            )

        members = self._t.call_endpoint(url=f"streams/{stream_id}/members", method="GET")
        if members.get("result") != "success":
            raise NotSubscribed(
                f"could not list the subscribers of '{self._settings.channel}' "
                f"({members.get('msg') or members!r}); refusing to guess who is reachable."
            )
        here = set(members.get("subscribers", []))

        users = self._t.call_endpoint(url="users", method="GET")
        if users.get("result") != "success":
            raise NotSubscribed(
                f"could not list realm users ({users.get('msg') or users!r}); refusing to "
                "post a mention that may not resolve."
            )
        return sorted(
            u["full_name"] for u in users.get("members", [])
            if u.get("user_id") in here and u.get("is_active") and u.get("full_name")
        )

    def send(self, channel: str, topic: str, content: str) -> dict:
        """Post as this seat's bot. Attribution is automatic and not optional."""
        return self._t.call_endpoint(
            url="messages",
            method="POST",
            request={"type": "stream", "to": channel, "topic": topic, "content": content},
        )


def build_transport(credential: Credential) -> Transport:
    """Construct the real Zulip client.

    No insecure switch is threaded through, and none is accepted from the
    credential — see `config._reject_insecure`. TLS verification is the library
    default and stays there.
    """
    import zulip

    return zulip.Client(
        email=credential.email, api_key=credential.key, site=credential.site
    )
