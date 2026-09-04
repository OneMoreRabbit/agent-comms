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
    CredentialMissing,
    QueueGapError,
)
from .hub import Hub, Registration, Transport, build_transport
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
    report.warnings.extend(registration.warnings)
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


def mention_from_event(site: str, event: dict) -> Mention | None:
    """Turn a Zulip message event into a stored mention, or None if not for us.

    Only messages flagged `mentioned` are ours. A channel carries every
    conversation in the project; without this filter a seat would store the lot.
    """
    if event.get("type") != "message":
        return None
    if "mentioned" not in (event.get("flags") or []):
        return None
    msg = event["message"]
    return Mention(
        id=msg["id"],
        sender=msg.get("sender_full_name") or msg.get("sender_email", "unknown"),
        channel=msg.get("display_recipient") or "",
        topic=msg.get("subject") or "",
        content=msg.get("content") or "",
        timestamp=msg.get("timestamp", 0),
        permalink=_permalink(site, msg),
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
    result = hub.send(target.channel or settings.channel, target.topic, content)
    store.mark_read(message_id)
    return result


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
    credential = load_credential(settings.identity)
    store = Store(settings.state_dir)
    store.ensure()

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
            mention = mention_from_event(credential.site, event)
            if mention is None:
                continue
            store.append(mention)
            stored += 1
            _notify(settings, store, mention)
            if on_mention is not None:
                on_mention(mention)
        store.save_position(registration.queue_id, registration.last_event_id)

    return stored


def _register(hub: Hub, store: Store) -> Registration:
    registration = hub.register_queue()
    for warning in registration.warnings:
        store.record("warn", warning)
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
