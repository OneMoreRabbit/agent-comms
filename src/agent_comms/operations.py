"""Operations layer — everything the CLI does, callable without a terminal.

Constitution §5: an `operations.py` above `cli.py`, so a later GUI or service
calls the same internals. Nothing here prints; every function returns a value or
raises. `cli.py` is the only module that formats for a human.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .config import Credential, Settings, load_credential, load_settings
from .errors import (
    CommsDisabled,
    CommsError,
    ConflictingWakeTriggers,
    CredentialMissing,
    QueueGapError,
)
from .hub import Hub, Registration, Transport, build_transport
from .seat import SeatStatus, SeatUnavailable
from .seat import status as seat_status_now
from .wake import WakeError, wake
from .store import Mention, Store


@dataclass
class Status:
    """What state this seat's comms is in, and why."""

    enabled: bool
    detail: str
    tag: str
    identity: str | None = None
    channel: str | None = None
    credential: str | None = None
    ready: bool = False


def status(**kw) -> Status:
    """Answer the question §3 insists we answer: disabled, or broken, and which.

    A seat with no credential is not broken; it is a seat without comms. A seat
    with comms *enabled* and no credential is broken and says so. The two are
    identical on disk, which is exactly why this distinction is a contract term
    rather than a nicety.
    """
    try:
        settings = load_settings(**kw)
    except CommsDisabled as exc:
        return Status(enabled=False, detail=str(exc), tag=exc.tag)
    except CommsError as exc:
        return Status(enabled=True, detail=str(exc), tag=exc.tag)

    base = Status(
        enabled=True,
        detail="",
        tag="ready",
        identity=settings.identity.bot_name,
        channel=settings.channel,
    )
    try:
        credential = load_credential(settings.identity)
    except CommsError as exc:
        base.detail, base.tag = str(exc), exc.tag
        return base

    base.credential = credential.source
    base.detail = f"comms enabled for {settings.identity.bot_name} on channel '{settings.channel}'"
    base.ready = True
    return base


@dataclass
class Preflight:
    """The result of the connect-time checks in contract §3.

    `disabled` is tracked separately from `ok` on purpose. A seat with comms off
    has not failed anything — reporting it as a failed check would repeat, in
    our own output, exactly the conflation §3 tells us to avoid.
    """

    ok: bool
    disabled: bool = False
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Recorded, not raised. Kept separate so a note never dilutes a warning.
    notes: list[str] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append((name, passed, detail))
        if not passed:
            self.ok = False


def preflight(
    transport_factory: Callable[[Credential], Transport] = build_transport, **kw
) -> Preflight:
    """Run every check the client would run at connect, and report them all.

    Deliberately does *not* stop at the first failure: an operator debugging a
    seat wants the whole picture, not one line at a time.
    """
    report = Preflight(ok=True)

    try:
        settings = load_settings(**kw)
    except CommsDisabled as exc:
        report.disabled = True
        report.checks.append(("enabled", True, str(exc)))
        return report
    except CommsError as exc:
        report.add("configuration", False, str(exc))
        return report
    report.add("enabled", True, f"{settings.identity.bot_name} → channel '{settings.channel}'")

    try:
        credential = load_credential(settings.identity)
    except CommsError as exc:
        report.add("credential", False, str(exc))
        return report
    report.add("credential", True, f"{credential.source} → {credential.site}")
    report.warnings.extend(credential.notices)

    hub = Hub(transport_factory(credential), settings, credential)
    identity_notices = hub.verify_identity()
    report.add("identity", True, f"expected bot '{settings.identity.bot_name}'")
    report.warnings.extend(identity_notices)

    try:
        hub.verify_subscription()
        report.add("subscription", True, f"subscribed to '{settings.channel}'")
    except CommsError as exc:
        report.add("subscription", False, str(exc))
        return report

    try:
        registration = hub.register_queue()
    except CommsError as exc:
        report.add("event queue", False, str(exc))
        return report

    report.add(
        "event queue",
        True,
        f"registered {registration.queue_id} at lifespan_secs={settings.lifespan_secs}",
    )

    # Deliverability is a separate question from connectivity, and the one that
    # went unnoticed for 21 minutes in the live incident. We no longer answer it
    # ourselves — this is a passthrough of the seat's verdict, so the estate has
    # one source for the fact rather than two that can disagree.
    try:
        sess = seat_status_now()
        report.add("deliverable", sess.deliverable,
                   f"seat says {sess.verdict}"
                   + (f" — {sess.reason}" if sess.reason else "")
                   + (f" (awake={sess.awake})" if sess.awake is not None else ""))
    except SeatUnavailable as exc:
        report.add("deliverable", False, str(exc))
    report.warnings.extend(registration.warnings)
    report.notes.extend(registration.notes)
    return report


def _permalink(site: str, event_msg: dict) -> str:
    """Build a citable permalink. Chat is not the record; this is how it cites one."""
    stream_id = event_msg.get("stream_id")
    channel = event_msg.get("display_recipient") or ""
    topic = event_msg.get("subject") or ""
    if stream_id is None:
        return site
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", str(channel)).strip("-")
    quoted = urllib.parse.quote(topic, safe="")
    return f"{site}/#narrow/channel/{stream_id}-{slug}/topic/{quoted}/near/{event_msg['id']}"


def is_authorised(settings: Settings, sender: str) -> bool:
    """May this sender direct this seat? ADR-0009 §9.

    Declared by the estate, defaulting to the seat's own arch bot. Compared on
    the bot's display name, which is what attribution rests on (§1a) — the same
    name a human reads in the channel.
    """
    return sender.strip().casefold() in {a.casefold() for a in settings.authority}


def addressed_to_seat(
    settings: Settings, msg: dict, flags: list[str], own_email: str | None = None
) -> str | None:
    """Is this message for this seat? Returns why, or None.

    **Our own messages are never for us.** A seat posts to a topic named after
    itself, so without this it stores everything it says and reads its own words
    back as an ask. Observed live: this seat's store contained its own smoke
    test. Under the topic rule below it would have applied to every post.

    Three ways to reach a seat, and the middle one was missing until 0.4:

    1. **An explicit `@`-mention.** Unambiguous, always works.
    2. **The topic convention.** ADR-0009 §1 puts one channel per project and
       *"one topic per arch ↔ component conversation"*, and R1 agreed the naming
       `<component>: <ask>` precisely *"so a seat can filter its own
       conversations without relying on permissions"*. The topic **is** the
       addressing. 0.1–0.3 matched only on mentions, which contradicted this
       client's own contract §2a and made every message to a seat require an
       `@`-mention — including replies in a topic already named for it.
    3. **A direct message** to the bot.

    Everything else in the channel is other people's conversation, and is not
    stored: with one channel per project, matching everything would wake every
    seat on every message.
    """
    sender = (msg.get("sender_email") or "").casefold()
    if own_email and sender == own_email.casefold():
        return None

    if "mentioned" in flags:
        return "mentioned"
    if msg.get("type") == "private":
        return "direct message"

    topic = (msg.get("subject") or "").strip()
    prefix = topic.split(":", 1)[0].strip().casefold() if ":" in topic else ""
    if prefix and prefix in {n.casefold() for n in settings.identity.bot_names}:
        return "topic addressed to this seat"
    return None


def mention_from_event(
    site: str, event: dict, settings: Settings, own_email: str | None = None
) -> Mention | None:
    """Turn a Zulip message event into a stored mention, or None if not for us."""
    if event.get("type") != "message":
        return None
    msg = event["message"]
    reason = addressed_to_seat(settings, msg, event.get("flags") or [], own_email)
    if reason is None:
        return None
    return Mention(
        id=msg["id"],
        sender=msg.get("sender_full_name") or msg.get("sender_email", "unknown"),
        channel=msg.get("display_recipient") if isinstance(msg.get("display_recipient"), str) else "",
        topic=msg.get("subject") or "",
        content=msg.get("content") or "",
        timestamp=msg.get("timestamp", 0),
        permalink=_permalink(site, msg),
        reason=reason,
        authorised=is_authorised(
            settings, msg.get("sender_full_name") or msg.get("sender_email", "")
        ),
    )


def inbox(unread_only: bool = True, **kw) -> list[Mention]:
    settings = load_settings(**kw)
    store = Store(settings.state_dir)
    return store.unread() if unread_only else store.all()


def show(message_id: int, **kw) -> Mention | None:
    settings = load_settings(**kw)
    store = Store(settings.state_dir)
    for m in store.all():
        if m.id == message_id:
            store.mark_read(message_id)
            return m
    return None


def send(
    topic: str,
    content: str,
    transport_factory: Callable[[Credential], Transport] = build_transport,
    **kw,
) -> dict:
    settings = load_settings(**kw)
    credential = load_credential(settings.identity)
    hub = Hub(transport_factory(credential), settings, credential)
    return hub.send(settings.channel, topic, content)


def addressed(sender: str, content: str) -> str:
    """Prefix a reply with an @-mention of whoever asked.

    Without this the arch↔component loop is invisible from the arch side: a
    seat's inbox is mention-based, and a reply posted into
    `agent-comms: roll call` matches no topic prefix an *arch* seat answers to.
    So replies landed in the channel and the arch seat reported that nobody had
    answered — a false negative pointing the same way as the false `delivered`.

    Mentioning the sender puts the reply in their inbox by the route they
    already read, rather than requiring them to query Zulip directly, which is
    the per-seat API integration the operator ruled against.
    """
    if not sender or sender.startswith("@"):
        return content
    return f"@**{sender}** {content}"


def reply(
    message_id: int,
    content: str,
    transport_factory: Callable[[Credential], Transport] = build_transport,
    **kw,
) -> dict:
    """Reply in the mention's own topic, so the conversation stays one thread."""
    settings = load_settings(**kw)
    store = Store(settings.state_dir)
    target = next((m for m in store.all() if m.id == message_id), None)
    if target is None:
        raise CommsError(f"no message {message_id} in the local store")
    credential = load_credential(settings.identity)
    hub = Hub(transport_factory(credential), settings, credential)
    result = hub.send(
        target.channel or settings.channel, target.topic, addressed(target.sender, content)
    )
    store.mark_read(message_id)
    return result


def wake_agent(
    mention: dict,
    transport_factory: Callable[[Credential], Transport] = build_transport,
    **kw,
) -> str:
    """Deliver a mention to the running agent, reporting a failure to the sender.

    §7b.5: a wake that fails is loud. A sender who is told nothing waits forever
    on a seat that never woke — and under §7e "no session yet" is normal, so
    silence is genuinely ambiguous between asleep and broken. Queuing announces
    itself once and then stays quiet until the next successful delivery, because
    a sleeping seat repeating itself is noise.
    """
    settings = load_settings(**kw)
    store = Store(settings.state_dir)
    store.ensure()

    try:
        status = seat_status_now()
    except SeatUnavailable as exc:
        outcome = f"queued: {exc}"
    else:
        try:
            outcome = wake(mention, status)
        except WakeError as exc:
            store.record("warn", f"delivery failed for message {mention.get('id')}: {exc}")
            _tell_sender(settings, store, mention,
                         f"could not deliver that to my agent: {exc}", transport_factory)
            raise

    queued = outcome.startswith("queued")
    store.record("info" if not queued else "warn", f"wake: {outcome}")
    if queued:
        if not store.sleeping():
            store.set_sleeping(True)
            _tell_sender(
                settings, store, mention,
                "queued — no agent session is running on this seat, so nothing has read "
                "this yet. It is stored and will be taken up when a session next starts "
                "(ADR-0009 §7e: a message never starts an agent). Saying so once rather "
                "than repeating it for every message while asleep.",
                transport_factory,
            )
    else:
        store.set_sleeping(False)
    return outcome


def _report_undeclared(
    settings: Settings,
    store: Store,
    mention: Mention,
    transport_factory: Callable[[Credential], Transport],
) -> None:
    """Raise an undeclared sender with the arch seat, per §1a's report half.

    "Report, never comply" only works if the reporting is mechanical. Left to the
    agent it depends on the agent noticing, which is the aspirational version §9
    exists to replace.
    """
    store.record("warn", f"undeclared sender {mention.sender!r} on message {mention.id}")
    _post(
        settings, store, mention.channel or settings.channel,
        f"{settings.identity.seat}: undeclared sender",
        f"**{mention.sender} directed {settings.identity.seat}, and is not a declared "
        f"sender for this seat** (ADR-0009 §9). The message was stored and shown to the "
        f"agent labelled *do not comply*; it has not been acted on.\n\n"
        f"Declared senders: {', '.join(settings.authority)}.\n"
        f"Cite: {mention.permalink}\n\n"
        "If this should be actionable, the estate declares the link — the seat cannot "
        "widen its own authority, which is the point of the rule.",
        transport_factory,
    )


def _announce_unreachable(
    settings: Settings,
    store: Store,
    report: str,
    transport_factory: Callable[[Credential], Transport],
) -> None:
    """Say on the channel that this seat cannot be reached, and how to fix it.

    Arch's position, 2026-09-08: the refusal is right, its **silence** is the
    defect. A seat was unreachable for 21 minutes because the sender saw a
    refusal and nobody watching the seat learned it was offline. This posts to a
    findable topic of the seat's own so anyone looking at the project sees it,
    not only whoever happened to message.

    Announced once per outage and cleared on the next success, because a seat
    repeating "I am unreachable" every message is the noise that gets skipped.
    """
    if store.unreachable():
        return
    store.set_unreachable(True)
    seat = settings.identity.seat
    _post(
        settings, store, settings.channel, f"{seat}: unreachable",
        f"**{seat} cannot receive messages.**\n\n{report}\n\n"
        "Messages are held in this seat's inbox meanwhile — nothing is lost, but "
        "nothing is being read either.",
        transport_factory,
    )


def _post(
    settings: Settings,
    store: Store,
    channel: str,
    topic: str,
    text: str,
    transport_factory: Callable[[Credential], Transport],
) -> None:
    try:
        credential = load_credential(settings.identity)
        hub = Hub(transport_factory(credential), settings, credential)
        hub.send(channel, topic, text)
    except Exception as exc:
        store.record("warn", f"could not post to {topic!r}: {exc}")


def _tell_sender(
    settings: Settings,
    store: Store,
    mention: dict,
    text: str,
    transport_factory: Callable[[Credential], Transport],
) -> None:
    """Post back into the mention's own topic. Best effort, and logged if it fails."""
    try:
        credential = load_credential(settings.identity)
        hub = Hub(transport_factory(credential), settings, credential)
        hub.send(mention.get("channel") or settings.channel,
                 mention.get("topic") or f"{settings.identity.seat}: comms", text)
    except Exception as exc:
        store.record("warn", f"could not tell the sender about message "
                             f"{mention.get('id')}: {exc}")


def run_daemon(
    transport_factory: Callable[[Credential], Transport] = build_transport,
    max_iterations: int | None = None,
    on_mention: Callable[[Mention], None] | None = None,
    **kw,
) -> int:
    """Hold the outbound connection and record what arrives.

    Returns the number of mentions stored. `max_iterations` bounds the loop for
    tests; in a seat it runs unbounded until interrupted.

    This never touches the working session. Mentions land in the store, and the
    seat's designated comms conversation is reached through `notify_command` —
    contract §3 'Non-invasive', the one term that does not flex. The client does
    not decide *how* a seat surfaces a mention, only that it is not mid-task.
    """
    settings = load_settings(**kw)
    check_wake_triggers(settings)
    credential = load_credential(settings.identity)
    store = Store(settings.state_dir)
    store.ensure()
    lock = store.acquire_daemon_lock()  # released by the OS when this process ends

    for notice in credential.notices:
        store.record("warn", notice)

    hub = Hub(transport_factory(credential), settings, credential)
    for notice in hub.verify_identity():
        store.record("warn", notice)
    hub.verify_subscription()

    registration = _resume_or_register(hub, store)

    stored, iterations, backoff = 0, 0, 1
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        try:
            events = hub.get_events(registration)
            backoff = 1
        except QueueGapError as exc:
            store.record("warn", f"{exc} Re-registering.")
            registration = _register(hub, store)
            continue
        except KeyboardInterrupt:  # pragma: no cover - operator stop
            store.record("info", "daemon stopped by operator")
            break
        except Exception as exc:  # transport hiccup, not a contract failure
            store.record("warn", f"event fetch failed ({exc}); retrying in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        for event in events:
            mention = mention_from_event(credential.site, event, settings, credential.email)
            if mention is None:
                continue
            store.append(mention)
            stored += 1
            if not mention.authorised:
                _report_undeclared(settings, store, mention, transport_factory)
            _notify(settings, store, mention)
            if on_mention is not None:
                on_mention(mention)

        # Every tick, not only when a message arrives. Zulip sends a heartbeat
        # about once a minute (measured: ~54s), so this loop turns over even on
        # a silent channel — which is what lets a queued message go in the
        # moment the seat wakes, with no timer and no second thread.
        _flush_pending(settings, store, transport_factory)
        store.save_position(registration.queue_id, registration.last_event_id)

    return stored


def _register(hub: Hub, store: Store) -> Registration:
    registration = hub.register_queue()
    for warning in registration.warnings:
        store.record("warn", warning)
    for note in registration.notes:
        store.record("info", note)
    store.save_position(registration.queue_id, registration.last_event_id)
    return registration


def _resume_or_register(hub: Hub, store: Store) -> Registration:
    """Resume a stored queue if one exists; otherwise register a fresh one.

    Re-registering when a usable queue was already held silently forfeits
    anything that arrived while the daemon was down — which is the gap §3 asks
    us to report, not to create.
    """
    saved = store.load_position()
    if not saved or not saved.get("queue_id"):
        return _register(hub, store)

    # Resume optimistically and let the main loop discover a dead queue. Probing
    # with a get_events call here would fetch real events and discard them,
    # advancing last_event_id past messages nobody ever saw. That is the silent
    # loss §3 exists to prevent, so the probe is deliberately absent.
    registration = hub.resume(saved["queue_id"], int(saved.get("last_event_id", 0)))
    store.record(
        "info",
        f"resuming queue {registration.queue_id} from event {registration.last_event_id}",
    )
    return registration


def check_wake_triggers(settings: Settings) -> None:
    """Refuse to start with both wake triggers armed.

    `notify_command = "comms wake"` is canonical — it is what ADR-0009 §7b
    describes, what the estate has deployed, and the hook a consumer can point
    anywhere. `wake = true` is the built-in shorthand for the same delivery.

    Set together they would each deliver the mention. Silent duplication is worse
    than a refusal — the same argument that put an flock on the daemon — and a
    seat answering every message twice presents as a hub fault, which is where it
    would be looked for.
    """
    if settings.wake and settings.notify_command:
        raise ConflictingWakeTriggers(
            "both wake triggers are set: notify_command="
            f"{settings.notify_command!r} and wake=true. Each delivers the mention, so "
            "together they deliver it twice. `notify_command` is canonical — unset "
            "`wake` in ~/.comms/config.toml (or unset notify_command if you meant to use "
            "the built-in). Refusing to start rather than answering every message twice."
        )


def _flush_pending(
    settings: Settings,
    store: Store,
    transport_factory: Callable[[Credential], Transport],
) -> int:
    """Deliver everything still waiting, if the seat can take it now.

    **The dormant-seat path.** A seat with no live session queues rather than
    losing messages, and this is what empties the queue once someone wakes it —
    the operator typing into the session is enough, and the daemon notices
    within a heartbeat.

    Ordering is preserved and the first failure stops the run: a conversation
    delivered out of order is worse than one delivered late, and if the seat is
    still dormant there is no point trying the rest.
    """
    if not settings.wake:
        return 0

    pending = store.undelivered()
    if not pending:
        return 0

    was_waiting = len(pending)
    # Read before delivering: a successful delivery clears the marker inside
    # wake_agent, so checking afterwards would always say "was not sleeping".
    was_sleeping = store.sleeping()
    sent = 0
    for mention in pending:
        try:
            outcome = wake_agent(asdict(mention), transport_factory,
                                 state_dir=settings.state_dir)
        except WakeError:
            break  # already recorded and reported; the seat is not takeable
        if outcome.startswith("queued"):
            break  # still dormant — leave the rest in order for the next tick
        store.mark_delivered(mention.id)
        sent += 1

    if sent and was_waiting > sent:
        store.record("info", f"delivered {sent} of {was_waiting} queued messages")
    elif sent:
        store.record("info", f"delivered {sent} queued message(s)")

    # Say it once, on the transition. A seat that woke and cleared a backlog is
    # worth announcing for the same reason a seat that went unreachable is: the
    # sender was told it was queued, and nothing else would tell them it landed.
    if sent and was_sleeping:
        store.set_sleeping(False)
        _post(
            settings, store, settings.channel,
            f"{settings.identity.seat}: awake",
            f"**{settings.identity.seat} is awake and has taken {sent} queued "
            f"message{'s' if sent != 1 else ''}.** They were held while the seat had no "
            "live session and have now been delivered in the order they arrived.",
            transport_factory,
        )
    return sent


def _deliver_to_agent(
    settings: Settings,
    store: Store,
    mention: Mention,
    transport_factory: Callable[[Credential], Transport],
) -> None:
    """Wake the seat's agent, if waking is turned on.

    Off by default. ADR-0009 §7d gated wake-ups behind the governor; §7e then
    removed that gate, because a message that cannot start an agent cannot run
    up an unattended night. The default stays off regardless — turning a seat
    from receiving to acting is a consumer's decision, not a package default.
    """
    if not settings.wake:
        return
    try:
        wake_agent(asdict(mention), transport_factory, state_dir=settings.state_dir)
    except WakeError:
        pass  # already recorded and reported to the sender by wake_agent


def _notify(settings: Settings, store: Store, mention: Mention) -> None:
    """Hand a mention to the seat's comms conversation, if one is configured."""
    if not settings.notify_command:
        return
    payload = json.dumps(asdict(mention), ensure_ascii=False)
    try:
        completed = subprocess.run(
            settings.notify_command,
            shell=True,
            input=payload,
            text=True,
            capture_output=True,
            timeout=30,
        )
        if completed.returncode != 0:
            store.record(
                "warn",
                f"notify_command exited {completed.returncode} for message {mention.id}: "
                f"{(completed.stderr or '').strip()[:400]}",
            )
    except Exception as exc:
        store.record("warn", f"notify_command failed for message {mention.id}: {exc}")
