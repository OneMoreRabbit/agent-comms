"""Operations layer — everything the CLI does, callable without a terminal.

Constitution §5: an `operations.py` above `cli.py`, so a later GUI or service
calls the same internals. Nothing here prints; every function returns a value or
raises. `cli.py` is the only module that formats for a human.
"""

from __future__ import annotations

import atexit
import json
import re
import signal
import os
import subprocess
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Collection

from .config import Credential, Settings, load_credential, load_settings
from .directory import Directory
from .directory import load as load_directory
from . import config_sync
from .errors import (
    ChannelNotReachable,
    CommsDisabled,
    CommsError,
    ConflictingWakeTriggers,
    CredentialMissing,
    CredentialUnreadable,
    DaemonAlreadyRunning,
    DaemonWillNotStop,
    InsecureTransportRefused,
    NotSubscribed,
    QueueGapError,
)
from .hub import Hub, Registration, Transport, build_transport
from .seat import SeatUnavailable, speaks_contract
from .seat import _client_version
from .seat import state as seat_state_now
from .wake import WakeError, wake
from .store import DaemonState, Mention, Store
from .queue import MessageStore


def message_store(state_dir) -> MessageStore:
    """The store of record for messages. **SQLite, since 2.0.**

    The JSONL file stops being the store and becomes what it always was
    underneath — the backup. On first use its contents are imported once, with
    the two refusals to guess that migration documents: a message refused by
    the permission graph imports as `refused` whatever its delivered flag says,
    and an undelivered one imports as `expired` rather than being assumed
    either way.

    Import happens HERE rather than in a deploy step because a migration a
    person has to remember is a migration that gets skipped on the seat nobody
    was watching.
    """
    store = MessageStore(Path(state_dir) / "comms.db")
    source = Path(state_dir) / "messages.jsonl"
    if source.exists():
        # **ALWAYS, not once.** The import is idempotent on the hub id, so
        # re-running it costs a file read and catches anything the JSONL has
        # that the database does not.
        #
        # It ran once behind a marker until 2026-09-23, and this seat proved
        # why that was wrong: a daemon still running PRE-2.0 code keeps
        # appending to the JSONL while the CLI reads SQLite, and the marker
        # stops the two ever meeting. Two of arch's messages were sitting in
        # the JSONL, invisible to `comms show`, with nothing reporting a
        # problem — a stranding window created by the very thing meant to
        # migrate cleanly.
        #
        # Removing the marker removes the window. The same shape as the WAL
        # fix an hour earlier: delete the state that goes stale rather than
        # manage it.
        from .migrate import import_jsonl
        out = import_jsonl(source, store)
        if out.written:
            (Path(state_dir) / ".last-import").write_text(
                out.report(source, store.path), encoding="utf-8")
    return store



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
    #: Whether a daemon is actually watching the hub for this seat. `None` when
    #: comms is off or unusable, where the question does not arise.
    daemon: "DaemonState | None" = None
    #: Which wake trigger is armed, or None when nothing is. A seat with a daemon
    #: and no trigger receives and does nothing, and `ready` above stays True
    #: because the hub half genuinely is ready — so this is a separate fact, not
    #: folded into it.
    wake_trigger: str | None = None


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
    # Asked here rather than left to `doctor`: "is comms working?" is the
    # question status exists to answer, and a configured seat with no daemon is
    # not working — it is losing messages while reporting itself ready.
    base.daemon = Store(settings.state_dir).daemon_state()
    base.wake_trigger = settings.notify_command or ("wake = true" if settings.wake else None)
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


def agents_reaching(assigned: dict, mine) -> tuple[list[str], list[str]]:
    """Split this seat's assigned agents into those pointing elsewhere and those
    pointing nowhere. `doctor`'s mirror check, kept testable.

    Returns `(wrong, undeclared)`:

    `mine` is EVERY name this seat's bot may carry, not one name. ADR-0009 §7a
    makes the canonical form conditional on role: a component bot is unambiguous
    as `<seat>` in its own channel and as `<project>-<seat>` anywhere, and
    **both are correct**. The directory authors the short form;
    `identity.bot_name` is the long one. Comparing against one alone fails every
    correctly-declared agent on every component seat — measured on test-claude
    2026-09-25, where this check called a provably working delivery
    "delivering to nobody".

    - **wrong** — the declared bot is not a name this seat answers to. The
      dangerous one: a sender obeying it posts where no bot of ours is subscribed, and a
      post no bot holds produces no event at all. Success reported, nothing
      delivered, nobody told.
    - **undeclared** — no transport at all. Not dangerous since derivation was
      removed: the send is refused at the sender and nothing is posted.
    """
    names = {mine} if isinstance(mine, str) else set(mine)
    wrong, undeclared = [], []
    for fqn, record in sorted(assigned.items()):
        bot = ((record.get("transports") or {}).get("comms") or {}).get("bot")
        if not bot:
            undeclared.append(fqn)
        elif bot not in names:
            wrong.append(f"{fqn} → bot '{bot}'")
    return wrong, undeclared


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

    # Every channel the directory says we must reach, checked against the ones
    # this bot actually holds. A transports record naming a channel we are not
    # subscribed to is silent non-delivery waiting to happen: the send would
    # succeed and the reply would never arrive.
    try:
        held = hub.subscribed_channels()
    except CommsError as exc:
        report.add("reachable channels", False, str(exc))
        held = frozenset()
    else:
        wanted = _transport_channels(settings)
        missing = sorted(c for c in wanted if c not in held)
        if missing:
            # GRANT WITHOUT SUBSCRIPTION. This is the direction that loses
            # messages: the send posts and the reply never comes back. §5a
            # provisions the two together, so this is drift, not a state
            # anybody chose — and it is fixed by orch's provisioning replay,
            # which is idempotent from the graph.
            report.add(
                "reachable channels", False,
                f"GRANT WITHOUT SUBSCRIPTION — the routing records name channel(s) this "
                f"bot cannot reach: {', '.join(missing)}. A message addressed there would "
                f"post and its reply would never come back. §5a provisions the grant and "
                f"the subscription together, so this is drift: ask the orchestrator to "
                f"replay provisioning, which reconciles subscriptions from the graph. "
                f"Subscribed to: {', '.join(sorted(held))}.")
        elif wanted:
            report.add("reachable channels", True,
                       f"every routed channel is subscribed: {', '.join(sorted(wanted))}")
        else:
            report.notes.append(
                "no routing records are cached yet, so there is nothing to check "
                "beyond this seat's own channel")

        # SUBSCRIPTION WITHOUT GRANT — the other direction, and deliberately a
        # NOTE. It loses nothing: an ungranted channel delivers events we then
        # refuse by the permission graph, which is the graph working. Making it
        # a warning would fire on every seat holding a test channel, and a
        # warning that fires every time is learned into invisibility
        # (constitution §9) — it would take the failure above down with it.
        if wanted:
            spare = sorted(c for c in held if c not in wanted)
            if spare:
                report.notes.append(
                    f"subscribed to {', '.join(spare)} with no routing record naming "
                    "it. Nothing is lost — mail from there is refused by the permission "
                    "graph — but after a narrowing, a subscription left behind is what "
                    "the provisioning replay tidies.")

    # THE MIRROR OF THE CHECK ABOVE, asked for by ansible-platform
    # (ansible-needs-comms-multi-agent-seat-delivery 0.1, ask 3).
    #
    # The channel check catches "we cannot reach where mail is addressed". This
    # catches the other half: an agent assigned to THIS seat whose declared
    # transport names a bot that is NOT this seat's. A sender obeying that
    # record posts where no bot of ours is subscribed, and §6 is measured on
    # this — a message posted where no bot is subscribed produces **no event at
    # all**. Not a refusal, not a log line. Silence.
    #
    # It is loud HERE and silent THERE, which is the whole reason it belongs at
    # the seat: the seat can see what it is supposed to serve; the sender only
    # sees a post that appeared to work.
    mine = settings.identity.canonical_names(settings.role)
    assigned = config_sync.agent_set(settings.state_dir)
    # **RESOLVE each one — the assignments answer carries no transports.**
    # Measured 2026-09-25 on test-claude: `/v0/seats/<p>/<s>/assignments`
    # returns agent, seat_local_id, label, runtime, delivery, route_revision
    # and NO transports block. Reading the cache for them would report every
    # agent as undeclared forever and could never catch the wrong-bot case --
    # a check that fires every time and detects nothing (constitution §9).
    # The need asks for the RESOLVED transport, and resolution is where it is.
    from .resolve import Resolver
    me = f"bakehouse.{settings.identity.project}.{settings.identity.seat}"
    resolver = Resolver(local_agents=assigned)
    resolved, unreadable = {}, []
    for fqn in sorted(assigned):
        answer = resolver.resolve(fqn, caller=me)
        if answer.success:
            resolved[fqn] = {"transports": answer.transports}
        else:
            unreadable.append(f"{fqn} ({answer.status})")
    wrong, undeclared = agents_reaching(resolved, mine)
    if unreadable:
        # We could not ask. Saying "undeclared" would be asserting an absence
        # we did not observe -- the difference between a no and a silence.
        report.notes.append(
            f"could not resolve assigned agent(s), so their transport is unchecked: "
            f"{', '.join(unreadable)}")
    if wrong:
        report.add(
            "agents reach this seat", False,
            f"DELIVERING TO NOBODY — this seat is assigned agent(s) whose declared "
            f"transport names a different bot: {'; '.join(wrong)}. This seat answers to "
            f"{' or '.join(repr(n) for n in mine)}. A sender obeying those records posts where no bot of ours is "
            f"subscribed, and a post no bot holds produces no event at all — the send "
            f"reports success and nothing ever arrives. Fix the directory's "
            f"`transports.comms` for those agents to name {mine[0]!r}.")
    elif undeclared:
        # NOT a failure. Derivation is gone (§5, amended 2026-09-25), so an
        # undeclared agent is refused at the sender, loudly, with nothing
        # posted. That is a missing record, not a silent loss.
        report.notes.append(
            f"assigned agent(s) with no declared transport: {', '.join(undeclared)}. "
            f"A seat serving more than one agent REQUIRES them, because one bot is one "
            f"seat's mailbox and nothing derives a per-agent one. Until they are "
            f"authored, a send to those names is refused at the sender and nothing "
            f"is posted.")
    elif assigned:
        report.add("agents reach this seat", True,
                   f"all {len(resolved)} resolved agent(s) declare a bot this seat answers to")

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
    # `seat status` is ADVISORY (contract §3) and this is the only place this
    # client may use it: a health check for a person, never a pre-check before
    # delivering. Delivery asks `seat msg` and reads its answer, full stop.
    try:
        sess = seat_state_now()
        report.add("deliverable", sess.ok, sess.summary())
    except SeatUnavailable as exc:
        report.add("deliverable", False, str(exc))

    # Who may talk to this seat. Reported because "no directory installed" is a
    # real state — the orchestrator installs the file with comms, so its absence
    # means permissions are a default rather than a declaration, and nobody
    # should have to read source to find that out.
    try:
        directory = load_directory(settings.state_dir)
        report.add("directory", True, directory.summary())
        report.warnings.extend(directory.warnings)
        refused = [m for m in Store(settings.state_dir).all() if not m.authorised]
        if refused:
            # The stored flag records the rule in force when the message arrived,
            # so re-check the senders against the directory as it stands now.
            # Otherwise this note reports a seat as blocked when the only thing
            # that changed is the rule — which is how a stale flag becomes a
            # false accusation.
            senders = {m.sender for m in refused}
            still = sorted(n for n in senders if not is_permitted(directory, hub, n))
            note = f"{len(refused)} stored message(s) were refused as not permitted"
            if still:
                note += f"; still refused today: {', '.join(still)}"
            if len(still) < len(senders):
                was = sorted(senders - set(still))
                note += (
                    f". {', '.join(was)} would be permitted now — those were refused "
                    "under an earlier rule, not by the current directory"
                )
            report.notes.append(
                note + ". All are stored and visible in `comms inbox`; if one should "
                "have arrived, the directory is what to fix."
            )
    except CommsError as exc:
        report.add("directory", False, str(exc))

    # Which seat build answered the questions above. Contractual from 0.3.3, and
    # consumed because a mixed estate is the normal state during a rollout: a
    # verdict is only as good as the build that produced it, and until 0.3.1 a
    # seat could misreport the contract it implemented.
    # Which seat build answered, read from the same advisory call rather than a
    # second one. A mixed estate is normal during a rollout and a verdict is only
    # as good as the build behind it.
    try:
        build = seat_state_now()
        known = bool(build.version and build.contract)
        report.add("seat build", known,
                   f"seat {build.version or '?'} (contract {build.contract or '?'})")
        if known and not speaks_contract(build.contract):
            report.warnings.append(
                f"this seat implements contract {build.contract}, which agent-comms "
                f"{_client_version()} does not speak. Nothing can be delivered here "
                "until the pair is on speaking terms — check which half is behind "
                "before upgrading either."
            )
    except SeatUnavailable as exc:
        report.add("seat build", False, str(exc))

    # A daemon is what makes any of the above matter. Without one the checks
    # above all pass and the seat receives nothing — every other failure in this
    # client's catalogue has that shape, and this one had it too until it was
    # measured by hand on 2026-09-10.
    side = Store(settings.state_dir)
    running_build = side.daemon_build()
    if daemon_is_running(side) and running_build and running_build != _client_version():
        report.add(
            "daemon build", False,
            f"the RUNNING daemon is {running_build}; this CLI is {_client_version()}. "
            "They are out of step, which is a real state during an upgrade and not a "
            "guess — the daemon is a process and the CLI is whatever is on disk now. "
            "Restart it to bring them together: comms daemon --restart. Until then "
            "the two halves may disagree about where messages are stored.")
    elif running_build:
        report.add("daemon build", True, f"daemon and CLI both {running_build}")

    daemon = Store(settings.state_dir).daemon_state()
    report.add("daemon", daemon.running and not daemon.stale, daemon.summary())

    # The last mile, and the one this client had no check for until arch found it
    # on 2026-09-13: a seat with no wake trigger stores every mention and wakes
    # nobody. Every check above passed on exactly that seat, which is the shape
    # constitution §9 names first — a check that declines to run under the
    # condition it exists to catch, reading as "all clear".
    #
    # It FAILS rather than notes. `_deliver_to_agent` is right that waking is a
    # consumer's decision and off by default, but for this estate the decision is
    # already made: wake-on-mention Ask 2 rules that "an unset notify_command is
    # not a valid pilot configuration — it is a seat that receives and does
    # nothing". A note would be true and would change nobody's behaviour.
    report.add("wake trigger", bool(settings.notify_command or settings.wake),
               _wake_summary(settings))

    report.warnings.extend(registration.warnings)
    report.notes.extend(registration.notes)
    return report


def _wake_summary(settings: Settings) -> str:
    """Say which trigger is armed, or what the absence of one costs.

    Written for whoever is asking why a seat answers nothing despite a green
    `doctor` — so the absence names the remedy, not just the state.
    """
    if settings.notify_command:
        return f"notify_command: {settings.notify_command}"
    if settings.wake:
        return "wake = true (the built-in; notify_command is canonical)"
    return (
        "NONE — mentions are stored and no agent is ever woken, so this seat "
        "receives and does nothing. Every other check here can pass while that is "
        "true. Set notify_command = \"comms wake\" in ~/.comms/config.toml "
        "(the estate installs this file; ansible-platform owns it on a provisioned "
        "seat). Until then, mentions are only visible to `comms inbox`."
    )


def daemon_is_running(store) -> bool:
    """One place asks; `doctor` and the build check must not disagree."""
    return store.daemon_state().running


def _transport_channels(settings: Settings) -> set[str]:
    """Channels named by cached routing records, plus this seat's own.

    Read from the local config the periodic layer writes. Absent means nothing
    has been fetched yet, which is a note rather than a failure — there is
    genuinely nothing to check.
    """
    channels = {settings.channel.strip().casefold()}
    records = config_sync.load(settings.state_dir).get("routes") or []
    for record in records:
        name = ((record or {}).get("transports") or {}).get("comms", {}).get("channel")
        if name:
            channels.add(str(name).strip().casefold())
    return channels


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


def is_permitted(directory: Directory, hub: Hub, sender: str) -> bool:
    """May this sender exchange messages with this seat? ADR-0009 §9.

    Declared by the estate in `~/.comms/comms.yml`, never by the seat. Compared
    on the bot's display name, which is what attribution rests on (§1a) — the
    same name a human reads in the channel.

    Membership of "my project" is the hub's answer (the channel's subscriber
    list), not a second roster kept here, so a seat the estate minted this
    morning is permitted this afternoon with no file to edit.
    """
    in_project, is_human = hub.in_channel(sender)
    return directory.permits(sender, in_project=in_project, is_human=is_human)


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
        # **Our own post is ours -- unless it is addressed to one of our own
        # agents.** One bot is one SEAT's mailbox, so an agent addressing a
        # SIBLING on the same seat posts through this very bot and the message
        # comes straight back. Dropping every self-post made same-seat
        # addressing impossible.
        #
        # Only the explicit marker counts here, never the topic prefix: an
        # agent replying in its own thread has a topic naming itself, and
        # accepting that would hand the agent back its own words forever.
        # A send carries the marker; a reply does not.
        return ("addressed to an agent on this seat"
                if envelope_from_body(msg.get("content") or "") else None)

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
    site: str,
    event: dict,
    settings: Settings,
    own_email: str | None = None,
    permitted: Callable[[str], bool] | None = None,
) -> Mention | None:
    """Turn a Zulip message event into a stored mention, or None if not for us."""
    if event.get("type") != "message":
        return None
    msg = event["message"]
    reason = addressed_to_seat(settings, msg, event.get("flags") or [], own_email)
    if reason is None:
        return None
    serves = config_sync.agent_set(settings.state_dir)
    return Mention(
        id=msg["id"],
        sender=msg.get("sender_full_name") or msg.get("sender_email", "unknown"),
        channel=msg.get("display_recipient") if isinstance(msg.get("display_recipient"), str) else "",
        topic=msg.get("subject") or "",
        agent=addressed_agent(msg.get("subject") or "", serves,
                              body=msg.get("content") or ""),
        content=msg.get("content") or "",
        timestamp=msg.get("timestamp", 0),
        permalink=_permalink(site, msg),
        reason=reason,
        authorised=(
            True if permitted is None
            else permitted(msg.get("sender_full_name") or msg.get("sender_email", ""))
        ),
    )


def addressed_agent(topic: str, serves: Collection[str] = (), body: str = "") -> str:
    """The FQN a message was addressed to, from its topic prefix. Empty if none.

    The sender writes the topic as `<what --to named>: <subject>`, so when a
    caller addressed an FQN the topic prefix IS that FQN. That is the only
    place it survives: the body carries an `@`-mention of the seat's bot, and
    one bot serves every agent on the seat.

    **Shape only, and deliberately not a lookup.** An FQN is
    `estate.project.agent` — three non-empty dot-separated segments, no spaces.
    A bare seat name (`test-claude: uc01`) has no dots and returns empty, which
    is what keeps plain seat-name addressing working exactly as it did.

    **It must also name an agent THIS seat is assigned.** A REPLY stays in the
    topic it answers, so the prefix names whoever the thread was opened to —
    not whoever this message is for. Measured 2026-09-25: test-claude's agent
    replied to test-codex in topic
    `bakehouse.agent-eco.test-claude-another1: uc01-another1`; test-codex read
    that prefix as its envelope address, and its seat answered `unknown-agent`
    — *"assigned to another seat"*. The reply sat queued and undelivered.

    So the seat's assigned set is the filter. It is not a second opinion on the
    seat's authority: the question here is not "does this seat serve the name"
    but "is this prefix MY envelope, or somebody else's thread name". Whether a
    name we DO claim can be delivered to remains the seat's call, answered
    loudly as `unknown-agent` at exit 10.

    Fails safe: an empty assigned set yields no `--agent`, so the seat's
    default answers — the 1.0 behaviour, never worse than before.
    """
    marked = envelope_from_body(body)
    if marked:
        return marked if marked in set(serves) else ""

    prefix = (topic or "").split(":", 1)[0].strip()
    if not prefix or any(c.isspace() for c in prefix):
        return ""
    parts = prefix.split(".")
    if len(parts) != 3 or not all(parts):
        return ""
    return prefix if prefix in set(serves) else ""


def inbox(unread_only: bool = True, **kw) -> list[Mention]:
    settings = load_settings(**kw)
    store = message_store(settings.state_dir)
    return store.unread() if unread_only else store.all()


def show(message_id: int, **kw) -> Mention | None:
    settings = load_settings(**kw)
    store = message_store(settings.state_dir)
    for m in store.all():
        if m.id == message_id:
            store.mark_read(message_id)
            return m
    return None


class Unaddressed(CommsError):
    """This message names no recipient. Refused before it is posted.

    The failure it prevents, observed on blocks 2026-09-10: arch posted under
    its **own** topic prefix with the recipients typed as plain text. Comms
    routes by topic prefix or by a real mention, and a seat ignores its own
    posts — so neither target was addressed by any route and nothing was
    delivered. Correct behaviour, invisible outcome.
    """

    tag = "unaddressed"


class UnknownRecipient(CommsError):
    """`--to` named a seat this seat cannot address. Refused before it is posted.

    Two ways to earn this, and they are different problems: a name that exists
    nowhere in the realm (a typo, or a seat that was never minted), and a name
    that exists but is not in this seat's channel (real, reachable — just not
    from here). Both are told apart in the message, because the fix differs.
    """

    tag = "unknown-recipient"


#: A Zulip mention as it is actually written: `@**name**`. Deliberately only this
#: form. Bare `@name` in prose is prose — deciding what counts is the guesswork
#: `send` refuses to do, and it is wrong silently when it decides badly. `@**…**`
#: is unambiguous: the sender has already said "this is an address".
_MENTION = re.compile(r"@\*\*([^*\n]+)\*\*")


@dataclass
class Posted:
    """What the hub said, and anything the sender needs to know about it.

    `warnings` is empty on the normal path. It carries the case the ArcPlatform
    finding named: a mention that renders perfectly and notifies nobody.
    """

    response: dict
    warnings: list[str] = field(default_factory=list)


def unreachable_mentions(hub: Hub, content: str) -> list[str]:
    """Names mentioned in the body that this channel cannot deliver to.

    The check the client already knew how to make and never ran on this path.
    `addressable_names()` and the recipient resolver have carried the right
    answer — and the right wording — since 0.40; they were reachable only when a
    recipient arrived as a *parameter*. So the one way a seat naturally addresses
    someone, `@**name**` typed into the text, was the one way nothing validated.

    Cost so far: `blocks-android` on 2026-09-10 and `orchestrator` on 2026-09-13.
    Both rendered correctly, both returned `sent`, both reached nobody, and both
    were noticed by a third party days later rather than by the sender.

    Warn, do not refuse (the reporter's own preference, and right): the message
    is usually still worth posting to the channel, and what the sender needs is
    to learn *at the moment of sending* that one addressee will not see it, so
    they can route another way.
    """
    named = [m.group(1).strip() for m in _MENTION.finditer(content)]
    if not named:
        return []
    reachable = {n.casefold() for n in hub.addressable_names()}
    seen, missing = set(), []
    for name in named:
        key = name.casefold()
        if key in reachable or key in seen:
            continue
        seen.add(key)
        missing.append(name)
    return missing


def _mention_warnings(hub: Hub, content: str, channel: str) -> list[str]:
    """One line per unreachable mention, in the resolver's own words."""
    return [
        f"'{name}' is mentioned in the body but is not in channel '{channel}', so that "
        f"mention notifies nobody. The message was posted; reach "
        f"{name} another way."
        for name in unreachable_mentions(hub, content)
    ]


def send(
    content: str,
    to: str | None = None,
    subject: str | None = None,
    topic: str | None = None,
    channel: str | None = None,
    transport_factory: Callable[[Credential], Transport] = build_transport,
    **kw,
) -> dict:
    """Post to this seat's project channel, addressed to a named seat.

    **The protocol is one line: the sender names the seat, this client spells the
    address.** `--to` is required and carries a plain seat name; the topic
    becomes `<recipient>: <subject>` and the body is prefixed with a real
    `@**<recipient>**` mention, so both of the two routes a recipient matches on
    are covered without the sender knowing which.

    **The body is never rewritten.** An earlier version scanned message text for
    `@name` and converted it, which is guesswork about prose — it has to decide
    what is a mention, what is an email address, what is already correct, and it
    is wrong silently when it decides badly. Addressing is structure, so it
    travels in a flag, not in the text.

    **It is read for exactly one thing** (0.52.0): an explicit `@**name**` is
    checked against the channel, and an unreachable one is returned as a warning
    on `Posted.warnings`. That is not the guesswork above — `@**…**` is the
    sender saying "this is an address", so there is nothing to infer. See
    `unreachable_mentions`.

    The named seat is checked against the hub before anything is posted: it must
    exist in the realm and be subscribed to this channel. A message to a seat
    that cannot be reached from here is refused loudly rather than posted into
    the void — which is the failure that started this, and which looks exactly
    like success.
    """
    settings = load_settings(**kw)

    if not to or not to.strip():
        raise Unaddressed(
            "nothing to address this to. Every message names its recipient: "
            "--to <seat> --subject '<what it is about>'. The body is not scanned "
            "for addressing, so a seat named only in the text reaches nobody."
        )

    recipient = to.lstrip("@").strip("*").strip()
    if not topic:
        if not subject:
            raise Unaddressed(
                f"--to {recipient} needs a --subject, so the topic can be "
                f"'{recipient}: <subject>'. Without one there is no topic to post under."
            )
        topic = f"{recipient}: {subject}"

    credential = load_credential(settings.identity)
    hub = Hub(transport_factory(credential), settings, credential)

    # **Resolve first, and only fall back to a seat name.** Until 2026-09-25
    # this went straight to `_resolve_recipient`, a hub seat-name lookup, so
    # R7/R10/R15 were built, tested, green and NOT CONNECTED: an FQN was
    # refused as an unknown seat while `comms resolve` answered `resolved` for
    # the same name in the same second. Found by running UC-02, not by reading
    # the code — a suite that cannot fail on an unwired component is not
    # evidence about wiring.
    routed = _route(settings, recipient)
    if routed is not None:
        # **The derived bot must be an account the hub actually has.**
        # R15 derives `bot` from the FQN's agent segment, which assumes one hub
        # identity PER AGENT. The deployed hub has one per SEAT. Measured
        # 2026-09-25: `bakehouse.agent-eco.test-claude-new001` derives
        # `test-claude-new001`, which is not an account, so the post mentioned
        # nobody — posted, `sent` reported, read by no one. A successful
        # delivery to the wrong audience, which is the thing nobody notices.
        #
        # So this checks before posting and refuses loudly. It does NOT guess a
        # substitute: falling back to the seat's bot would deliver to the seat's
        # default agent while the caller named a different one, which is the
        # same silent-wrong-recipient failure wearing a helpful face.
        if not hub.addressable(routed.bot):
            raise UnknownRecipient(
                f"'{recipient}' resolves to {routed.fqn}, whose transport derives the "
                f"hub identity '{routed.bot}' — and the hub has no such account.\n"
                "  Nothing was posted. A message mentioning an account that does not "
                "exist reaches nobody while reporting success.\n"
                "  The estate declares per-agent transports in the directory's "
                "`transports.comms` block; this agent has none, so the name was "
                "derived from the FQN (project → channel, agent → bot). Either the "
                "hub identity is missing or the directory must declare the override.")
        recipient, channel = routed.bot, channel or routed.channel
    else:
        recipient = _resolve_recipient(settings, hub, recipient)
        channel = channel or settings.channel

    require_reachable(hub, channel)

    warnings = _mention_warnings(hub, content, channel)
    response = hub.send(channel, topic,
                        addressed(recipient, content,
                                  to_fqn=routed.fqn if routed is not None else ""))
    return Posted(response=response, warnings=warnings)


@dataclass
class Routed:
    """Where the directory says a name goes, and as whom."""

    fqn: str
    channel: str
    bot: str
    delivery: str


def _route(settings: Settings, name: str, **kw):
    """Ask the directory where a name goes. `None` means 'not an estate name'.

    Three things happen here that used not to happen at all:

    1. **The name is resolved** against the directory (R7), so an FQN or an
       authored alias addresses an AGENT rather than being mistaken for a seat.
    2. **The delivery mode is honoured at send** (R10) — `none` refuses here,
       and a value outside `inject | hold | none` refuses with the value
       quoted. Comms is the only component that ever reads this field.
    3. **The transport is derived from the FQN** (R15) — project is the
       channel, agent is the bot — with a declared override taking precedence.

    Returning `None` for a name the directory does not know is deliberate: a
    bare seat name is still a legitimate address between seats, and this must
    add a capability without removing one. But a name the directory REFUSES is
    an error we raise, not a seat name to try next — falling through would turn
    'you may not address that' into 'no such seat', which is the wrong-cause
    class.
    """
    from .delivery import NotDeliverable, permitted_to_send, plan, transport_for
    from .resolve import Resolver

    me = f"bakehouse.{settings.identity.project}.{settings.identity.seat}"
    answer = Resolver(local_agents=config_sync.agent_set(settings.state_dir)).resolve(
        name, caller=me)

    if not answer.success:
        if answer.status == "not-permitted":
            # A permission DECISION. Refusing here is the point; falling
            # through would turn "you may not address that" into "no such
            # seat", which is the wrong-cause class this change exists to fix.
            raise UnknownRecipient(
                f"'{name}' is not permitted: {answer.message or 'no reason given'}")
        # Anything else — `unknown` (not an estate name) or `not-registered`
        # (known, no route yet) — means the DIRECTORY cannot route it, not that
        # the message cannot be sent. A bare seat name is still a legitimate
        # address between seats, and most estate agents have no announced slot
        # today: refusing here would break every send that works now. Add a
        # capability without removing one.
        return None

    allowed, why = permitted_to_send(answer.delivery or "inject")
    if not allowed:
        raise UnknownRecipient(f"'{name}' will not be sent to: {why}")

    p = plan(answer)
    try:
        t = transport_for(p.fqn, answer.transports)
    except NotDeliverable as exc:
        raise UnknownRecipient(str(exc)) from None
    return Routed(fqn=p.fqn, channel=t["channel"], bot=t["bot"],
                  delivery=p.delivery)


def require_reachable(hub: Hub, channel: str) -> None:
    """Refuse to post into a channel this bot is not subscribed to.

    **Subscription is the routing mechanism and nothing declares it.** The event
    queue carries no narrow, so a seat receives exactly what its bot holds.
    Measured 2026-09-22: a message posted in a channel this bot does not hold
    produces no event at all — not a refusal, not a stored record, not a log
    line. Silence at the transport layer, before any permission check.

    So posting into an unsubscribed channel would send a message whose reply we
    could never read: we would start a topic we cannot follow. Cross-project
    conversations live entirely in the RECIPIENT's channel with the sender's bot
    subscribed there (comms-design §5a) — this is the check that makes that a
    requirement rather than a hope.
    """
    held = hub.subscribed_channels()
    if channel.strip().casefold() in held:
        return
    raise ChannelNotReachable(
        f"this seat's bot is not subscribed to '{channel}', so a message posted "
        "there would go out and its reply would never come back — the event "
        "queue carries no narrow, so a seat receives exactly what its bot holds "
        "and nothing else.\n"
        f"  subscribed to: {', '.join(sorted(held)) or '(nothing)'}\n"
        "  A cross-project conversation lives in the RECIPIENT's channel and the "
        "sender's bot must be subscribed there (comms-design §5a). Subscribe it, "
        "or address an agent whose channel this seat already holds."
    )


def _resolve_recipient(settings: Settings, hub: Hub, name: str) -> str:
    """Return the recipient as Zulip spells it, or refuse and say why.

    Matched case-insensitively so a seat need not know the hub's capitalisation,
    and returned in the hub's own spelling because that is what `@**...**` has to
    contain to resolve. Addressing yourself is refused: a seat ignores its own
    posts, so it is the one mention guaranteed to reach nobody.
    """
    ours = {n.casefold() for n in settings.identity.canonical_names(settings.role)}
    if name.casefold() in ours:
        raise UnknownRecipient(
            f"'{name}' is this seat. A seat ignores its own posts, so this would "
            "reach nobody. Name the seat you want to reach."
        )

    reachable = hub.addressable_names()
    match = next((n for n in reachable if n.casefold() == name.casefold()), None)
    if match is not None:
        # Reachable is not permitted. The hub says a message *can* arrive; the
        # directory says whether the estate allows it. Same rule as inbound, so
        # a link cannot be one-way by accident.
        directory = load_directory(settings.state_dir)
        if not is_permitted(directory, hub, match):
            in_project, _ = hub.in_channel(match)
            raise UnknownRecipient(
                f"not permitted: {directory.refusal(match, in_project=in_project)}"
            )
        return match

    others = ", ".join(n for n in reachable if n.casefold() not in ours) or "nobody"
    exists = any(n.casefold() == name.casefold() for n in hub.realm_names())
    if exists:
        raise UnknownRecipient(
            f"'{name}' exists on the hub but is not in channel '{settings.channel}', so a "
            "mention of it here would render correctly and notify nobody. Reach it through "
            "a channel you both sit in, or ask the estate to subscribe it. "
            f"Reachable from here: {others}."
        )
    # **Say WHY it failed, not just that it did.** Until 2026-09-25 this was the
    # only sentence a bad address ever got, so a correctly-spelled,
    # never-authored estate shorthand was refused as a typo — "check the
    # spelling" against a name spelled perfectly. A refusal naming the wrong
    # cause is worse than a bare refusal, because it is actionable in the wrong
    # direction. Ask the directory, and if it has something to say, say that
    # instead.
    hint = _directory_hint(name)
    if hint:
        raise UnknownRecipient(hint + f"\n  Seats reachable from here: {others}.")
    raise UnknownRecipient(
        f"no seat named '{name}' exists on the hub, and the directory does not "
        f"know it either — check the spelling. Reachable from here: {others}."
    )


def _directory_hint(name: str, **kw) -> str:
    """What the directory says about a name `send` could not place, or "".

    This is the same answer `comms resolve` gives, brought to the place a
    person actually hits the problem. Best-effort: a directory that cannot be
    reached costs the hint, never the refusal.
    """
    try:
        from .resolve import Resolver
        settings = load_settings(**kw)
        me = f"bakehouse.{settings.identity.project}.{settings.identity.seat}"
        answer = Resolver(
            local_agents=config_sync.agent_set(settings.state_dir)).resolve(name, caller=me)
    except Exception:                                    # noqa: BLE001 — a hint
        return ""
    if answer.near_misses:
        hint = (f"'{name}' is not a seat, and the directory does not know it as an "
                f"agent either.\n  Did you mean: {', '.join(answer.near_misses[:5])}")
        # The bare-role note only when it IS one — the same name ending several
        # FQNs in different projects. On a plain typo it is noise, and a hint
        # that explains something the reader did not do is a hint they stop
        # reading.
        tail = name.strip().casefold()
        projects = {m.split(".")[1] for m in answer.near_misses
                    if m.count(".") >= 2 and m.rsplit(".", 1)[-1].casefold() == tail}
        if len(projects) > 1:
            hint += ("\n  (A bare role like this is never authored as an alias — it "
                     "exists in several projects, so which one is meant depends on "
                     "who is asking. Use the full name.)")
        return hint
    if answer.status == "not-registered":
        return (f"'{name}' IS a known estate agent, but the directory has no route "
                "for it yet — it has not been assigned and announced. This is not a "
                "spelling mistake.")
    return ""


#: The envelope marker the sender writes and the receiver reads:
#: `@**seat-bot** \u2192`estate.project.agent` body`.
#:
#: **Why the body and not the topic.** The topic cannot tell "addressed to"
#: from "thread named after": a reply stays in the topic it answers, so an
#: agent replying in its own thread looks exactly like a message addressed to
#: that agent. That is harmless across seats and a LOOP on one seat -- we would
#: store our own reply and hand it back to the agent that wrote it. A marker
#: the sender writes is carried by a send and not by a reply, which is the
#: distinction the topic cannot make.
ENVELOPE = re.compile(r"^@\*\*[^*]+\*\*\s*\u2192`([^`]+)`")


def envelope_from_body(content: str) -> str:
    """The FQN the sender addressed, from the marker. Empty if unmarked."""
    m = ENVELOPE.match((content or "").lstrip())
    return m.group(1).strip() if m else ""


def addressed(sender: str, content: str, to_fqn: str = "") -> str:
    """Prefix a message with an @-mention of the seat it is for.

    `to_fqn` adds the envelope marker: one bot serves every agent on a seat, so
    the mention says WHICH SEAT and the marker says WHICH AGENT.

    Without this the arch↔component loop is invisible from the arch side: a
    seat's inbox is mention-based, and a reply posted into
    `agent-comms: roll call` matches no topic prefix an *arch* seat answers to.
    So replies landed in the channel and the arch seat reported that nobody had
    answered — a false negative pointing the same way as the false `delivered`.

    The name is turned into Zulip's `@**name**` syntax here and nowhere else,
    and the body is passed through untouched. A seat writes seat names; only
    this client writes hub syntax.
    """
    name = (sender or "").lstrip("@").strip("*").strip()
    mark = f" \u2192`{to_fqn}`" if to_fqn else ""
    if not name:
        return content
    return f"@**{name}**{mark} {content}"


def reply(
    message_id: int,
    content: str,
    transport_factory: Callable[[Credential], Transport] = build_transport,
    **kw,
) -> dict:
    """Reply in the mention's own topic, so the conversation stays one thread."""
    settings = load_settings(**kw)
    store = message_store(settings.state_dir)
    target = next((m for m in store.all() if m.id == message_id), None)
    if target is None:
        raise CommsError(f"no message {message_id} in the local store")
    credential = load_credential(settings.identity)
    hub = Hub(transport_factory(credential), settings, credential)
    channel = target.channel or settings.channel
    warnings = _mention_warnings(hub, content, channel)
    result = hub.send(channel, target.topic, addressed(target.sender, content))
    store.mark_read(message_id)
    return Posted(response=result, warnings=warnings)


def wake_agent(
    mention: dict,
    transport_factory: Callable[[Credential], Transport] = build_transport,
    **kw,
) -> str:
    """Hand one message to the seat, and decide what happens if it does not land.

    **One call, no pre-check.** The contract forbids asking `seat status` and then
    acting on it (§3): two truths with a gap between them, and the gap is where a
    message is lost. This client did exactly that until 1.0.

    **The queue is ours and so is the retry** (operator, 2026-09-17). The seat is
    stateless about delivery — it does not store, retry or queue, and an
    undelivered message remains ours. So the seat's answer is an *input to a
    decision* here, not merely a report for a human:

    - delivered / queued  → success, marked, done. `queued` is codex taking it for
      a thread that is not loaded; it is not a degraded delivery.
    - no-session / unknown → keep it, retry later, tell the sender once.
    - failed (exit 10)     → the attempt failed; same treatment.
    - broken               → a person must look. Not retried: nothing a retry can
      change, and spinning would bury the reason.
    - failed (exit 2)      → a usage error, which is OUR defect. Never retried; a
      malformed call repeated is how a bug becomes a flood.
    """
    settings = load_settings(**kw)
    store = message_store(settings.state_dir)
    store.ensure()
    mid = mention.get("id")

    try:
        result = wake(mention)
    except WakeError as exc:
        # The seat could not be invoked at all — a different fault from anything
        # the seat reports. The message stays ours and stays queued.
        store.record("warn", f"delivery could not be attempted for {mid}: {exc}")
        _announce_held(settings, store, mention, str(exc), transport_factory)
        return f"queued: {exc}"

    if result.success:
        store.mark_delivered(mid) if mid is not None else None
        store.set_sleeping(False)
        store.record("info", f"wake: {result.summary()}")
        return result.summary()

    store.record("warn", f"wake: {result.summary()}")

    if result.needs_a_person:
        # §4: not exactly one session, or no runtime declared. Loud, once, and
        # never retried — the seat is telling us a person has to intervene.
        _announce_held(
            settings, store, mention,
            f"**this seat is broken and a person must look at it** — {result.message}\n\n"
            "Your message is stored here and is not lost. It will not be retried "
            "until the seat is fixed, because no retry can change this state.",
            transport_factory,
        )
        return result.summary()

    if not result.retryable:
        # exit 2 — we called the seat wrongly. Ours to fix, and loud about it.
        store.record("warn", f"NOT retrying {mid}: {result.status} exit {result.exit_code} "
                             "is a usage error in this client, not a seat fault")
        _tell_sender(settings, store, mention,
                     f"could not deliver that to my agent: {result.message}",
                     transport_factory)
        return result.summary()

    _announce_held(settings, store, mention, result.message, transport_factory)
    return result.summary()


def _announce_held(
    settings: Settings,
    store: Store,
    mention: dict,
    reason: str,
    transport_factory: Callable[[Credential], Transport],
) -> None:
    """Tell the sender once that their message is held, not lost.

    **Once per spell, not per message.** A seat repeating "still not deliverable"
    at every message is the noise that teaches people to skip the notice, and the
    notice is the thing that stops a sender waiting forever on a seat that never
    woke (ADR-0009 §7e: a message never starts an agent).
    """
    if store.sleeping():
        return
    store.set_sleeping(True)
    _tell_sender(
        settings, store, mention,
        f"**not deliverable right now** — {reason}\n\n"
        "Your message is stored on this seat and will be retried. Saying so once "
        "rather than repeating it while the seat stays this way.",
        transport_factory,
    )


#: **The bounds, wired into the delivery path.** Before this, `retry_undelivered`
#: took up to TWENTY undelivered messages per pass with no staleness check at
#: all, so restoring a seat after an outage replayed its whole backlog as live
#: turns. That is the September incident, and it is what made 2.0 undeployable
#: without a manual precaution.
#:
#: Configurable, and these are the operator's numbers.
MAX_AGE_SECS = 24 * 60 * 60
#: At most this many per pass, so a backlog never arrives as N live turns. The
#: NEWEST ones, because the newest are the likeliest to still be current — the
#: rest stay stored and the agent is told once that they exist.
MAX_PER_PASS = 3


def retire_stale(store: Store, max_age_secs: int = MAX_AGE_SECS) -> list[Mention]:
    """Retire everything past the age bound BEFORE any attempt is made.

    **Checked before every pass, not only before the first attempt.** The design
    says before the first; that leaves any message which got one attempt
    unbounded, because nothing fixes a retry interval — so it reaches a session
    days later, which is the exact thing the rule exists to prevent. Built to
    the rule's stated intent: *a stale message must not reach a session even
    once, because it arrives looking current.*

    Nothing is deleted. Retired messages stay in the store and in `comms inbox`,
    to be read deliberately, newest-first.
    """
    now = time.time()
    retired = []
    for mention in store.undelivered():
        if now - mention.timestamp > max_age_secs:
            store.mark_retired(mention.id, f"older than {max_age_secs // 3600}h")
            retired.append(mention)
    return retired


def retirement_summary(retired: list[Mention], still_waiting: int) -> str:
    """ONE line for a batch. Never N stale turns.

    Carries the caveat that earned itself: a context-free seat cannot tell a
    superseded instruction from a current one, and the neighbours are the
    evidence.
    """
    oldest = min((m.when for m in retired), default="")
    line = (f"**{len(retired)} message(s) were not delivered** — older than "
            f"{MAX_AGE_SECS // 3600}h (oldest {oldest}). They are in `comms inbox`, "
            "unread and unretried.")
    if still_waiting:
        line += f" {still_waiting} more are still queued behind this pass."
    return line + ("\n\nThese are **not instructions**: later messages commonly "
                   "supersede earlier ones, so read newest-first and check the "
                   "current state before acting on any of them.")


def _tell_agent_once(settings, store, text, transport_factory) -> None:
    """Put the summary in front of the agent — one line, via the wake path.

    Deliberately not a hub post: this is about mail already on this seat, so
    telling the channel would be noise to everyone else. Failure to wake is
    swallowed on purpose — the summary is a courtesy, and a seat that cannot be
    woken has a louder problem that `doctor` already reports.
    """
    try:
        wake({"id": 0, "sender": "agent-comms", "channel": settings.channel,
              "topic": "retired mail", "content": text, "timestamp": int(time.time()),
              "permalink": "", "read": False, "reason": "summary",
              "delivered": False, "attempts": 0, "authorised": True, "retired": ""})
    except (WakeError, Exception):  # noqa: BLE001 — a courtesy must not break a pass
        store.record("warn", "could not deliver the retirement summary")


def retry_undelivered(
    transport_factory: Callable[[Credential], Transport] = build_transport,
    limit: int = MAX_PER_PASS,
    max_age_secs: int = MAX_AGE_SECS,
    **kw,
) -> int:
    """Try the queue again, BOUNDED. Returns how many landed this pass.

    Three bounds, each bought by the September incident:

    1. **Age is checked before every attempt.** Anything past the bound is
       retired here, before delivery is attempted, so nothing stale reaches a
       session even once.
    2. **At most `limit` per pass, the NEWEST ones**, so a backlog never arrives
       as N live turns. Selected newest-first and then delivered in arrival
       order, because a conversation delivered out of order is worse than one
       delivered late.
    3. **One summary line** tells the agent what was retired and what is still
       waiting — not N turns saying it.

    It stops at the first still-undeliverable message: if the seat cannot take
    one it will not take the next either.
    """
    settings = load_settings(**kw)
    store = message_store(settings.state_dir)

    retired = retire_stale(store, max_age_secs)
    waiting = [m for m in store.undelivered() if m.authorised]
    # Newest first for the CAP, then arrival order for DELIVERY.
    pending = sorted(sorted(waiting, key=lambda m: m.id, reverse=True)[:limit],
                     key=lambda m: m.id)
    if retired:
        store.record("warn", f"retired {len(retired)} message(s) past the "
                             f"{max_age_secs // 3600}h bound without delivering them")
        _tell_agent_once(settings, store,
                         retirement_summary(retired, max(0, len(waiting) - len(pending))),
                         transport_factory)
    if not pending:
        return 0

    landed = 0
    for mention in pending:
        try:
            result = wake(asdict(mention))
        except WakeError:
            break  # the seat is not reachable at all; nothing else will land either
        attempts = store.record_attempt_for(mention.id)
        if not result.success:
            if not result.retryable:
                # broken, or our own usage error. Leave it stored and stop: both
                # need attention rather than another attempt.
                store.record("warn", f"retry: giving up on {mention.id} — "
                                     f"{result.summary()} is not retryable")
                break
            if attempts >= MAX_DELIVERY_ATTEMPTS:
                # Retryable by status, but not in fact. Say so once, loudly, and
                # leave it stored — a person can see it in `comms inbox`, and the
                # queue behind it stops being held hostage.
                store.record(
                    "warn",
                    f"retry: message {mention.id} has failed {attempts} times and is "
                    f"no longer being retried — {result.summary()}. It is still "
                    "stored and visible in `comms inbox`.",
                )
                store.mark_delivered(mention.id)  # out of the queue, not lost
            break
        store.mark_delivered(mention.id)
        landed += 1

    if landed:
        store.set_sleeping(False)
        store.record("info", f"retry: delivered {landed} held message(s)")
    return landed


def _refuse_sender(
    settings: Settings,
    store: Store,
    mention: Mention,
    directory: Directory,
    hub: Hub,
    bounced: set[str],
    transport_factory: Callable[[Credential], Transport],
) -> None:
    """Refuse a message from a sender the estate has not permitted.

    "Report, never comply" only worked if the reporting was mechanical, and
    leaving it to the agent depended on the agent noticing. Now it does not
    reach the agent at all — but it is stored, logged and answered, because a
    refusal nobody can see is how a wrong directory becomes a silent outage.

    The sender is told **once per daemon run**. Bouncing every message would
    ping-pong against a seat that refuses us in turn.
    """
    store.record(
        "warn",
        f"refused message {mention.id} from {mention.sender!r}: not a permitted partner",
    )
    key = mention.sender.strip().casefold()
    if key in bounced:
        return
    bounced.add(key)

    in_project, _ = hub.in_channel(mention.sender)
    # **Into the sender's own topic**, not a topic of our own. Found by the live
    # test on 2026-09-11: the reply went to `agent-comms: not a permitted sender`
    # while the operator sat in the topic they had posted in, so from their side
    # the refusal was silent. The unit test asserted the post existed and passed,
    # which is exactly the check that cannot see this.
    _post(
        settings, store, mention.channel or settings.channel,
        mention.topic or f"{settings.identity.seat}: not a permitted sender",
        f"@**{mention.sender}** **your message was not delivered to "
        f"{settings.identity.seat}.** {directory.refusal(mention.sender, in_project=in_project)}"
        f"\n\nIt is stored on the seat and visible to the operator, but it did not reach "
        f"the agent. Cite: {mention.permalink}\n\n"
        "Further messages from you to this seat are refused without a reply until the "
        "estate declares the link.",
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
    """Post back into the mention's own topic. Best effort, and logged if it fails.

    Mentioning the sender for the same reason `reply` does: the topic is named
    after *us*, so an arch seat filtering on its own topic prefix would never see
    a "your message is held" notice posted there. A hold nobody is told about is
    the silence §7b.5 exists to prevent.
    """
    try:
        credential = load_credential(settings.identity)
        hub = Hub(transport_factory(credential), settings, credential)
        hub.send(mention.get("channel") or settings.channel,
                 mention.get("topic") or f"{settings.identity.seat}: comms",
                 addressed(mention.get("sender") or "", text))
    except Exception as exc:
        store.record("warn", f"could not tell the sender about message "
                             f"{mention.get('id')}: {exc}")


def detach_daemon(log_path: str | None = None, **kw) -> int:
    """Start the daemon in the background, detached from this shell.

    A double fork with `setsid` between: the first fork lets this command
    return, `setsid` puts the daemon in its own session so it has no controlling
    terminal, and the second fork means it can never acquire one. The practical
    effect is the one that matters on a seat — **closing the shell, or losing the
    tmux session, no longer takes the daemon with it**, which is how this seat's
    daemon has died more than once.

    It is deliberately not a supervisor. Nothing restarts this process, and
    saying so plainly is better than a half-restarter that hides the gap.
    """
    settings = load_settings(**kw)
    store = message_store(settings.state_dir)
    store.ensure()

    state = store.daemon_state()
    if state.running:
        raise DaemonAlreadyRunning(
            f"a comms daemon is already running for this seat (pid {state.pid}, "
            f"{state.summary()}). Two daemons mean two event queues and every mention "
            "handled twice. To replace it rather than add to it: comms daemon --restart"
        )

    out = Path(log_path) if log_path else settings.state_dir / "daemon.out"

    if os.fork() > 0:
        # Reap the intermediate child immediately so it cannot linger as a zombie.
        os.wait()
        for _ in range(50):  # up to ~5s for the grandchild to take the lock
            time.sleep(0.1)
            state = store.daemon_state()
            if state.running:
                return state.pid or 0
        raise CommsError(
            f"the daemon was started but has not taken its lock within 5s. Look in {out} "
            "for why — it is refusing to start rather than failing silently."
        )

    os.setsid()
    if os.fork() > 0:
        os._exit(0)

    handle = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.dup2(handle, 1)
    os.dup2(handle, 2)
    os.chdir("/")

    try:
        run_daemon(**kw)
    except BaseException as exc:  # noqa: BLE001 - last line before the process ends
        try:
            store.record("warn", f"detached daemon exited: {type(exc).__name__}: {exc}")
        finally:
            os._exit(1)
    os._exit(0)


def stop_daemon(timeout: float = 10.0, **kw) -> tuple[bool, int | None]:
    """Stop this seat's daemon and wait until the lock is actually free.

    Returns `(stopped, pid)` — `stopped` False means there was nothing running,
    which is a state and not a failure, so an operator can run this twice.

    The wait is the point. SIGTERM returns immediately, but the lock is released
    by the OS when the process ends, and `detach_daemon` refuses to start while
    anyone holds it. Signalling and returning would therefore make `--restart`
    a race that usually works — and when it lost, it would report a daemon
    started that never was. So this polls the lock, not the clock, and raises
    `DaemonWillNotStop` rather than returning a hopeful answer.
    """
    settings = load_settings(**kw)
    store = message_store(settings.state_dir)
    store.ensure()

    state = store.daemon_state()
    if not state.running:
        return False, None
    if state.pid is None:
        # Both sources are exhausted: the lock file is empty AND the kernel's
        # lock table could not name a holder. Say so and stop. It must NOT
        # suggest a pattern match — `pkill -f "comms daemon"` matches the tmux
        # server's own argv and takes every session on the seat with it. That
        # advice used to live in this message and it cost thirteen seats their
        # sessions (orchestrator need, 2026-09-21).
        lock = settings.state_dir / "daemon.lock"
        raise DaemonWillNotStop(
            f"something holds {lock} but neither the lock file nor the kernel's lock "
            "table can name it, so there is nothing safe to signal. Find the holder "
            f"exactly, by that one file:\n"
            f"    fuser -v {lock}        # or: lsof {lock}\n"
            "then stop that pid. NEVER match on the command line: a seat's tmux server "
            "carries 'comms daemon' in its own argv, so pkill -f would kill the server "
            "and every session inside it."
        )

    pid = state.pid
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        # Gone between the lock probe and the signal. Fall through to the wait,
        # which is what actually decides the answer.
        pass
    except PermissionError:
        raise DaemonWillNotStop(
            f"pid {pid} holds this seat's lock but belongs to another user, so this "
            "seat cannot stop it. A daemon for one seat should never be owned by "
            "another — check who started it before killing it by hand."
        ) from None

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.1)
        if not store.daemon_state().running:
            return True, pid

    raise DaemonWillNotStop(
        f"daemon pid {pid} was sent SIGTERM and still holds the lock {timeout:.0f}s later. "
        "It is wedged somewhere that does not return. Stop it by hand — kill -9 "
        f"{pid} — then start a fresh one with: comms daemon --detach"
    )


def restart_daemon(log_path: str | None = None, timeout: float = 10.0, **kw) -> tuple[bool, int]:
    """Stop the running daemon, if any, then start a detached one.

    Returns `(replaced, pid)` — `replaced` says whether something was actually
    stopped, so the caller can tell "restarted" from "there was nothing running,
    so I started one".

    **This is not supervision**, and naming it `restart` does not make it so.
    It is an operator action: something has to run it. Nothing here notices a
    dead daemon or brings it back, which remains the deployer's to solve (see
    the daemon-supervision need with ansible-platform). What it does fix is the
    two-step by hand, where the second step was forgotten and the seat sat with
    no daemon at all — the exact five-hour outage of 2026-09-10.

    The replacement is always **detached**, never re-hosted the way the old one
    was. A daemon started under tmux, systemd or a bare shell is hosted by
    whoever declared it, and copying the arrangement observed at runtime would
    be inferring an owned fact from visible state (constitution §10). Detached
    is this command's own declared answer, and it is stated in the output so
    nobody has to guess which they got.
    """
    replaced, _ = stop_daemon(timeout=timeout, **kw)
    return replaced, detach_daemon(log_path, **kw)


#: Faults a restart cannot fix. Respawning on these is a crash loop that buries
#: the reason in a scrolling log — worse than stopping with it on screen, because
#: the operator then has a supervisor reporting activity and a seat receiving
#: nothing. Every one of these needs a person: a credential, a config line, or a
#: decision.
UNFIXABLE_BY_RESTART = (
    CommsDisabled,
    CredentialMissing,
    CredentialUnreadable,
    InsecureTransportRefused,
    NotSubscribed,
    ConflictingWakeTriggers,
    DaemonAlreadyRunning,
)


def supervise_daemon(
    max_restarts: int | None = None,
    backoff_start: float = 1.0,
    backoff_max: float = 60.0,
    **kw,
) -> int:
    """Run the daemon, and start it again if it exits. Returns the restart count.

    **This is not full supervision and the help says so.** It survives the daemon
    *crashing*. It does not survive being killed, the container restarting or the
    host rebooting — and nothing inside the container can, because there is no
    init in a devagent seat to own it (measured: no systemd, no cron, PID 1 is
    sshd). A self-respawning parent has the same lifetime as the thing it
    supervises. The durable answer is a host-side unit, which is asked for in
    `comms-daemon-supervision`; this closes the gap the client can close.

    Two properties make it worth having rather than a loop anyone could write:

    - **It refuses to restart on a fault a restart cannot fix.** A bad
      credential, comms disabled, an unsubscribed bot or two wake triggers are
      re-raised, not retried. A crash loop turns a legible error into noise and
      reports activity while the seat receives nothing.
    - **It backs off**, doubling to a cap, so a transient network fault does not
      become a hot loop against the hub — and it resets the delay after a run
      that lasted, because a daemon that ran for an hour and died is a different
      event from one failing instantly.

    `max_restarts` bounds the loop for tests; unbounded in a seat.
    """
    settings = load_settings(**kw)
    store = message_store(settings.state_dir)
    store.ensure()

    restarts, delay = 0, backoff_start
    while True:
        started = time.monotonic()
        try:
            run_daemon(**kw)
        except UNFIXABLE_BY_RESTART:
            # Loud and final. The supervisor's job is to keep a working daemon
            # running, not to keep an unworkable one company.
            raise
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 — anything else is worth retrying
            store.record("warn", f"daemon exited ({type(exc).__name__}: {exc}); supervisor restarting")
        else:
            store.record("warn", "daemon returned; supervisor restarting")

        if max_restarts is not None and restarts >= max_restarts:
            return restarts

        # A run that lasted was healthy until it was not: start the next backoff
        # from the bottom. Only repeated fast failures are worth slowing down.
        if time.monotonic() - started >= backoff_max:
            delay = backoff_start
        time.sleep(delay)
        delay = min(delay * 2, backoff_max)
        restarts += 1


def _record_exits(store: Store) -> None:
    """Make the daemon say so when it dies, however it dies.

    It did not, and that is why this seat's daemon was down for five hours on
    2026-09-10 with nothing in the log to say when or why — the gap had to be
    inferred, which is the silent-failure shape this whole client exists to
    refuse. Only the poll was guarded before; a signal or an exception anywhere
    else left no record at all.

    SIGTERM and SIGHUP are turned into `SystemExit` so the `atexit` line still
    runs: SIGHUP matters because it is what arrives when the shell or tmux
    session that launched the daemon goes away, which is the most common way one
    dies on a seat with no supervisor. SIGKILL cannot be caught by anyone, and is
    the one case still inferred from a stale heartbeat.
    """
    def _die(signum, _frame):
        raise SystemExit(f"signal {signal.Signals(signum).name}")

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _die)
        except (ValueError, OSError, AttributeError):
            pass  # not the main thread, or no such signal here: best effort

    def _note() -> None:
        exc = sys.exc_info()[1]
        if exc is None:
            store.record("info", "daemon stopped")
        else:
            store.record("warn", f"daemon stopped: {type(exc).__name__}: {exc}")

    atexit.register(_note)


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
    store = message_store(settings.state_dir)
    store.ensure()
    lock = store.acquire_daemon_lock()  # released by the OS when this process ends

    for notice in credential.notices:
        store.record("warn", notice)

    hub = Hub(transport_factory(credential), settings, credential)
    for notice in hub.verify_identity():
        store.record("warn", notice)
    hub.verify_subscription()

    directory = load_directory(settings.state_dir)
    for notice in directory.warnings:
        store.record("warn", notice)
    store.record("info", f"comms directory: {directory.summary()}")

    # Said once per daemon, at the top of the log, because it governs everything
    # the daemon does afterwards: with no trigger it will store every mention and
    # wake nobody, and the log would otherwise show a healthy daemon storing
    # messages with no hint that the last mile is missing. It does NOT refuse to
    # start — a seat that receives into `comms inbox` is degraded, not broken, and
    # refusing would take away the half that works.
    if not (settings.notify_command or settings.wake):
        store.record("warn", f"no wake trigger configured — {_wake_summary(settings)}")

    #: Senders already told they are not permitted, this daemon run. The bounce
    #: is itself a channel message, so a seat that considers us unpermitted would
    #: bounce it back and we would bounce that: two seats ping-ponging refusals.
    #: Saying it once per sender bounds that whatever the other side does, and is
    #: the same shape as the queued-message announcement.
    bounced: set[str] = set()

    registration = _resume_or_register(hub, store)
    _record_exits(store)
    # Beat once at startup. The first poll blocks until Zulip's heartbeat (~54s),
    # so without this a freshly started daemon carries the *previous* daemon's
    # last tick and reads as wedged for its first minute — a false alarm on the
    # one signal that has to stay trustworthy.
    store.record_build(_client_version())
    store.save_position(registration.queue_id, registration.last_event_id)

    stored, iterations, backoff = 0, 0, 1
    last_backstop = time.monotonic()
    last_config = 0.0
    gap_recovery = False
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        try:
            events = hub.get_events(registration)
            backoff = 1
        except QueueGapError as exc:
            # No longer "messages are lost": note it, re-register, then read
            # channel history forward on the next pass through the loop.
            store.record("warn", f"{exc} Re-registering and backfilling.")
            registration = _register(hub, store)
            gap_recovery = True
            events = []
        except KeyboardInterrupt:  # pragma: no cover - operator stop
            store.record("info", "daemon stopped by operator")
            break
        except Exception as exc:  # transport hiccup, not a contract failure
            store.record("warn", f"event fetch failed ({exc}); retrying in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        def handle_event(event: dict) -> None:
            """One message, whether the queue delivered it or history did.

            Backfill calls this too, so a recovered message is permission-checked,
            stored, refused or notified exactly like a live one.
            """
            nonlocal stored
            # The permission check asks the hub, so it can fail — and this loop
            # sits outside the guard around `get_events`. Unguarded, a single
            # transport hiccup would end the daemon, which is the silent-death
            # class this client exists to refuse; 0.40.2 introduced it and this
            # closes it. An undetermined permission **holds**: stored, visible,
            # not delivered, and not bounced — the same rule as the seat's own
            # `undetermined` verdict, because an answer nobody could get is not
            # a "no", it is a "not now".
            undetermined = False

            def _permitted(name: str) -> bool:
                nonlocal undetermined
                # **This seat is always permitted to address its own agents.**
                # A sibling message never crosses a trust boundary: it is this
                # seat's bot, this seat's agents, and this machine. The
                # permission graph governs who may reach us from OUTSIDE, and
                # making a seat list itself as its own partner would be a
                # config trap that reads as an error when it is omitted.
                #
                # Narrow on purpose: only a post from our own bot that carries
                # an envelope for one of our agents gets here at all --
                # `addressed_to_seat` has already dropped every other self-post.
                if name.strip().casefold() in {
                        n.casefold() for n in settings.identity.canonical_names(settings.role)}:
                    return True
                try:
                    return is_permitted(directory, hub, name)
                except Exception as exc:  # noqa: BLE001 - any hub failure
                    undetermined = True
                    store.record(
                        "warn",
                        f"could not determine whether {name!r} may message this seat "
                        f"({exc}); holding the message rather than refusing it.",
                    )
                    return False

            mention = mention_from_event(
                credential.site, event, settings, credential.email,
                permitted=_permitted,
            )
            if mention is None:
                return
            store.append(mention)
            stored += 1
            if not mention.authorised:
                # **Refused, not delivered.** Labelling it and handing it to the
                # agent made the boundary advisory — it put text from a sender
                # the estate has not permitted into the session, carrying an
                # instruction not to obey it, and an agent is exactly the thing
                # that can be argued out of a rule. Operator ruling, 2026-09-11.
                # It is still stored, still visible in `comms inbox`, and the
                # sender is told once, so nothing is silent and a wrong
                # directory is recoverable.
                if undetermined:
                    return  # held, already logged; no bounce for a non-answer
                _refuse_sender(settings, store, mention, directory, hub,
                               bounced, transport_factory)
                return
            _notify(settings, store, mention)
            if on_mention is not None:
                on_mention(mention)

        for event in events:
            handle_event(event)

        # The doorbell rang, but the queue is not the record. Anything that
        # arrived while it was down is in channel history and nowhere else, so
        # read forward from the last message actually handled.
        if gap_recovery:
            gap_recovery = False
            _catch_up(hub, store, handle_event, "queue was replaced")

        # Check the doorstep regardless. Two failures hide from the queue alone:
        # a connection that hangs without erroring, and a queue replaced between
        # ticks. Both look exactly like a quiet channel.
        if time.monotonic() - last_config >= CONFIG_REFRESH_SECS:
            last_config = time.monotonic()
            got = config_sync.fetch(settings.identity.project, settings.identity.seat,
                                    settings.state_dir)
            if got.source == "directory":
                store.record("info", f"config: {got.line()}")
            elif got.reason:
                store.record("warn", f"config: {got.reason}; running from file")

        if time.monotonic() - last_backstop >= BACKSTOP_SECS:
            last_backstop = time.monotonic()
            # The queue is ours, so something has to work it. Without this,
            # "stored and will be retried" is a claim nothing honours — the shape
            # this estate has spent a fortnight removing. Same timer as the
            # backstop: both ask "what did the fast path miss?".
            try:
                retry_undelivered(transport_factory=transport_factory, **kw)
            except CommsError as exc:
                store.record("warn", f"retry pass failed: {exc}")
            before = store.last_message_id()
            found = _catch_up(hub, store, handle_event, "backstop")
            if found:
                newest = store.last_message_id()
                stale = [m for m in store.all()
                         if before < m.id <= newest
                         and (time.time() - m.timestamp) > MISSED_AFTER_SECS]
                if stale:
                    # Not a race: these were sitting in history while the queue
                    # said nothing. The queue is not delivering, so replace it
                    # rather than trusting it for another ten minutes.
                    store.record(
                        "warn",
                        f"the event queue missed {len(stale)} message(s) that channel "
                        "history had; it is not delivering. Re-registering.",
                    )
                    registration = _register(hub, store)

        # Every tick, not only when a message arrives. Zulip sends a heartbeat
        # about once a minute (measured: ~54s), so this loop turns over even on
        # a silent channel — which is what lets a queued message go in the
        # moment the seat wakes, with no timer and no second thread.
        _flush_pending(settings, store, transport_factory)
        store.save_position(registration.queue_id, registration.last_event_id)

    return stored


#: How often to read channel history regardless of what the queue said. The
#: queue is a doorbell; this is checking the doorstep. Five minutes (operator,
#: 2026-09-16; was ten): it bounds how long a dead doorbell can go unnoticed,
#: and that is the number worth spending an API call on. Twelve calls an hour
#: against a hub this seat already long-polls continuously is not a cost.
BACKSTOP_SECS = 300
#: Slow-changing facts, §4a. Not on the hot path: resolution is per send.
CONFIG_REFRESH_SECS = 300

#: How many times a held message is retried before this client stops and says so.
#:
#: Bounded because "retryable" is a judgement about a STATUS, not a guarantee the
#: cause is temporary. Measured against a real seat 1.0.2: an oversized body
#: answers `failed` at exit 10 — retryable by the status table, and identical
#: every time. Unbounded retry would spin on it forever and bury the queue behind
#: it. Six attempts across the retry cadence is long enough for a seat to be
#: restarted and short enough that a permanent failure surfaces the same day.
MAX_DELIVERY_ATTEMPTS = 6

#: A message must be this old before its absence from the queue is evidence the
#: queue is broken. Without it, a message arriving between the doorbell firing
#: and the backstop reading would look like a missed notification, and the daemon
#: would tear down a healthy queue on an ordinary timing race.
MISSED_AFTER_SECS = 60


def _event_from_message(msg: dict) -> dict:
    """Shape a history message like the event the queue would have delivered.

    So a backfilled message goes through exactly the same permission check,
    storage, refusal and notify path as a live one. A second code path for
    recovered messages is how recovery quietly behaves differently from normal
    receipt — and the difference only shows up in the case nobody tests.

    `flags` carries `mentioned` when this seat is named, which is what the queue
    would have set; addressing itself is re-derived downstream from the topic and
    body, so nothing here decides who a message is for.
    """
    return {"id": msg.get("id"), "type": "message", "flags": msg.get("flags") or [],
            "message": msg}


def _catch_up(hub: Hub, store: Store, handle, reason: str) -> int:
    """Read everything after the last handled message and run it through `handle`.

    Returns how many were recovered. Says so in the log either way: "backfilled
    0" is the evidence that a gap cost nothing, and it is the line that turns
    "any messages sent meanwhile are lost" into a measured claim.
    """
    since = store.last_message_id()
    if since <= 0:
        # Nothing handled yet. A full history replay on a fresh seat would
        # notify the agent about every message ever sent to the channel, which
        # is worse than the gap. Start from now.
        return 0
    try:
        missed = hub.messages_after(since)
    except Exception as exc:  # noqa: BLE001 - the queue path still works
        store.record("warn", f"{reason}: could not read channel history to catch up ({exc})")
        return 0
    for msg in missed:
        handle(_event_from_message(msg))
    store.record("info" if missed else "info",
                 f"{reason}: backfilled {len(missed)} message(s) from channel history "
                 f"after id {since}")
    return len(missed)


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


# -- the reading surface (design §6, R17) -------------------------------------

def _state_of(m: "Mention") -> str:
    """One honest word for a 1.0-shaped record.

    `retired` is checked BEFORE `delivered` because a retired message carries
    both: `delivered` is the bookkeeping that stops it being picked up again,
    `retired` is the fact that nobody ever saw it. Reading them the other way
    round is exactly the conflation that made the 1.0.0 store ambiguous.
    """
    if not m.authorised:
        return "refused"
    if m.retired:
        return "retired"
    if m.delivered:
        return "delivered"
    return "queued"


def recent(last: int = 20, state: str = "", **kw) -> list[dict]:
    """The last N messages, newest first, one dict each."""
    store = message_store(load_settings(**kw).state_dir)
    rows = sorted(store.all(), key=lambda m: m.id, reverse=True)
    out = []
    for m in rows:
        current = _state_of(m)
        if state and current != state:
            continue
        out.append({"id": m.id, "when": m.when, "state": current, "sender": m.sender,
                    "topic": m.topic, "attempts": m.attempts,
                    "retired": m.retired, "permalink": m.permalink})
        if len(out) >= last:
            break
    return out


def stats(**kw) -> dict:
    """Counts by state and queue health — for a person and for the estate.

    Exposed as JSON on purpose: the estate polls this rather than reading a
    seat's prose, and a number nobody can poll is a number nobody checks.
    """
    settings = load_settings(**kw)
    store = message_store(settings.state_dir)
    rows = store.all()
    counts: dict[str, int] = {}
    for m in rows:
        counts[_state_of(m)] = counts.get(_state_of(m), 0) + 1
    waiting = [m for m in rows if _state_of(m) == "queued"]
    daemon = store.daemon_state()
    return {
        "seat": settings.identity.seat,
        "stored": len(rows),
        "by_state": counts,
        "undelivered": len(waiting),
        "retired": counts.get("retired", 0),
        "refused": counts.get("refused", 0),
        "oldest_undelivered": min((m.when for m in waiting), default=""),
        "daemon": daemon.summary(),
        "daemon_running": daemon.running,
    }


def trace(message_id: int, **kw) -> list[str]:
    """One message end to end: where it came from, what was decided, what happened.

    The point of `trace` is that it ends arguments — so it prints what is known
    and says plainly when something is not recorded, rather than implying the
    absence is a fact.
    """
    store = message_store(load_settings(**kw).state_dir)
    found = next((m for m in store.all() if m.id == message_id), None)
    if found is None:
        return [f"no message {message_id} on this seat."]

    lines = [
        f"message {found.id}   {found.when}",
        f"  from      {found.sender}   ({found.reason})",
        f"  topic     {found.topic}",
        f"  channel   {found.channel}",
        f"  state     {_state_of(found)}"
        + (f" — {found.retired}" if found.retired else ""),
        f"  attempts  {found.attempts}",
        f"  permitted {'yes' if found.authorised else 'NO — stored and never delivered'}",
    ]
    if found.permalink:
        lines.append(f"  cite      {found.permalink}")
    lines.append("  wake and per-attempt history are in ~/.comms/events.log; "
                 "the per-transition record arrives with the SQLite store.")
    return lines


def resolve_name(name: str, **kw) -> list[str]:
    """`comms resolve <name>` — what would this address resolve to, and WHY.

    **The command that ends arguments.** It prints the resolution path, the
    answer, the delivery mode and the permission verdict, and it sends nothing.
    Every failure this client has had in the field looked like "the message
    went nowhere"; this is how a person finds out where it would have gone
    before they send it.
    """
    from .delivery import NotDeliverable, permitted_to_send, plan, transport_for
    from .resolve import Resolver

    settings = load_settings(**kw)
    me = f"bakehouse.{settings.identity.project}.{settings.identity.seat}"
    answer = Resolver(local_agents=config_sync.agent_set(settings.state_dir)).resolve(
        name, caller=me)

    out = [f"{name}", f"  asked as   {me}", f"  answer     {answer.status}",
           f"  source     {answer.source}" + ("  (degraded)" if answer.degraded else ""),
           f"  because    {answer.reason or answer.message or '—'}"]
    if answer.near_misses:
        out.append(f"  did you mean  {', '.join(answer.near_misses[:5])}")
    if not answer.success:
        out.append("  would send  NO — nothing would be delivered")
        return out

    out += [f"  canonical  {answer.canonical_id}",
            f"  revision   {answer.route_revision}"]
    try:
        p = plan(answer)
        t = transport_for(p.fqn, answer.transports)
        allowed, why = permitted_to_send(p.delivery or "inject")
        out += [f"  transport  channel {t['channel']}, bot {t['bot']}",
                f"  delivery   {p.delivery or '(none declared)'}",
                f"  would send  {'YES' if allowed else 'NO — ' + why}"]
    except NotDeliverable as exc:
        out.append(f"  would send  NO — {exc}")
    return out


def queued_now(**kw) -> list[dict]:
    """`comms queue` — exactly what the next pass would deliver, in order.

    Not "everything waiting": the bounds apply here as they do in the pass, so
    what this prints is what would actually go. A queue view that shows more
    than the pass would send teaches the wrong expectation.
    """
    settings = load_settings(**kw)
    store = message_store(settings.state_dir)
    retired = retire_stale(store)
    waiting = [m for m in store.undelivered() if m.authorised]
    due = sorted(sorted(waiting, key=lambda m: m.id, reverse=True)[:MAX_PER_PASS],
                 key=lambda m: m.id)
    return [{"id": m.id, "when": m.when, "sender": m.sender, "topic": m.topic,
             "attempts": m.attempts, "of": len(waiting),
             "retired_this_check": len(retired)} for m in due]


def retire(message_id: int, reason: str, **kw) -> str:
    """`comms retire <id> --reason` — the supported version of editing the store.

    **Logged, always.** A person removing a message from the queue by hand is a
    legitimate act; doing it by editing a file is how a store stops being
    evidence. The reason is required for the same purpose: `retired` without a
    cause is indistinguishable from a bug, six months later.
    """
    store = message_store(load_settings(**kw).state_dir)
    if not store.mark_retired(message_id, f"by hand: {reason}"):
        return f"no message {message_id} on this seat."
    store.record("info", f"retired {message_id} by hand: {reason}")
    return f"retired {message_id}, never delivered — {reason}"


def requeue(message_id: int, reason: str, **kw) -> str:
    """`comms requeue <id> --reason` — deliberate resurrection, logged.

    The ONE place a message moves backwards, and it needs a person to say so.
    Everything else in this store is forward-only; an operator resurrecting a
    message is a decision, not a transition, and the record says which by
    carrying the reason and the fact that a human asked.
    """
    from .queue import QUEUED

    settings = load_settings(**kw)
    store = message_store(settings.state_dir)
    row = store._find(message_id)
    if row is None:
        return f"no message {message_id} on this seat."
    store.db.execute(
        "UPDATE messages SET state=?, attempts=0, retired_reason='' WHERE id=?",
        (QUEUED, row["id"]))
    store._log(row["id"], row["state"], QUEUED, f"requeued by hand: {reason}",
               rule="operator")
    store.record("info", f"requeued {message_id} by hand: {reason}")
    return (f"requeued {message_id} from {row['state']} — {reason}\n"
            "Attempts reset. The age bound still applies: if it is past the bound "
            "it will retire again on the next pass rather than deliver.")
