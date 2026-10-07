"""Operations layer — everything the CLI does, callable without a terminal.

Constitution §5: an `operations.py` above `cli.py`, so a later GUI or service
calls the same internals. Nothing here prints; every function returns a value or
raises. `cli.py` is the only module that formats for a human.
"""

from __future__ import annotations

import atexit
import json
import sqlite3
import traceback
from datetime import datetime, timezone
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
from . import addressable, config_sync
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
from .seat import _client_version, build_id
from .seat import state as seat_state_now
from .wake import Held, WakeError, wake
from .store import DaemonState, Mention, Store
from .queue import ForwardOnly, MessageStore


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

    # **"Subscribed to ''" is not a pass.** `verify_subscription` returns early
    # when there is no channel, because a seat with no assignments has nothing
    # to be subscribed TO and must be allowed to idle -- but reporting that as
    # a subscription is asserting one that cannot exist. Measured on
    # test-claude 2026-09-29: a deaf seat read `PASS subscription — subscribed
    # to ''`.
    if not (settings.channel or "").strip():
        report.add(
            "subscription", False,
            "this seat has no channel, so it is subscribed to nothing and can "
            "receive nothing. That is the correct and temporary state of a seat "
            "the estate has not assigned any agents to yet: the channel comes "
            "from the agents' own `transports.comms`, and the daemon joins on "
            "its next refresh once one is assigned. Nothing to fix here — ask "
            "for an assignment, and do NOT ask the orchestrator to replay "
            "provisioning, which reconciles subscriptions and would find "
            "nothing wrong.")
        # **Deliberately NOT an early return.** A genuine subscription error
        # stops the run below, because every check after it asks the hub. This
        # case is different: there is nothing wrong with the hub, and the
        # checks that follow carry the rest of the picture -- in particular
        # `deliverable`, which is the one a person actually reads to answer
        # "can this seat receive". Returning here would hide it.
    else:
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
    mine = settings.identity.known_names()
    assigned = config_sync.agent_set(settings.state_dir)
    # **RESOLVE each one — the assignments answer carries no transports.**
    # Measured 2026-09-25 on test-claude: `/v0/seats/<p>/<s>/assignments`
    # returns agent, seat_local_id, label, runtime, delivery, route_revision
    # and NO transports block. Reading the cache for them would report every
    # agent as undeclared forever and could never catch the wrong-bot case --
    # a check that fires every time and detects nothing (constitution §9).
    # The need asks for the RESOLVED transport, and resolution is where it is.
    from .resolve import Resolver
    resolver = Resolver(local_agents=assigned)
    resolved, unreadable = {}, []
    for fqn in sorted(assigned):
        # Each of our own agents asks about itself: a self-lookup needs no
        # stated sender, and there is no seat identity to offer.
        answer = resolver.resolve(fqn, caller=fqn)
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
        # posted. That is a missing record, not a silent loss. It is still a
        # CHECK rather than only a note, so the line exists either way.
        report.add(
            "agents reach this seat", True,
            f"nothing to verify for {', '.join(undeclared)} — no declared transport "
            f"yet. A seat serving more than one agent REQUIRES them, because one bot "
            f"is one seat's mailbox and nothing derives a per-agent one. Until they "
            f"are authored, a send to those names is refused at the sender with "
            f"nothing posted.")
    elif resolved:
        report.add("agents reach this seat", True,
                   f"all {len(resolved)} resolved agent(s) declare a bot this seat "
                   f"answers to")
    else:
        # **A CHECK WITH NOTHING TO VERIFY SAYS SO BY NAME.**
        #
        # This branch used to be silent: no agents assigned, or none of them
        # resolvable, and the check simply did not appear. Measured on the fresh
        # test-codex 2026-09-26 — doctor reported 11 checks where test-claude
        # reported 12, with nothing saying which was missing or why.
        #
        # An absent check and a passing check are indistinguishable to anyone
        # counting, which is the diagnostic-without-information class in the one
        # tool whose whole job is information. Ruled by arch the same day: a
        # check with nothing to verify says so by name, never silently absent.
        why = (f"could not resolve any of them ({', '.join(unreadable)})" if unreadable
               else "this seat is assigned no agents yet" if not assigned
               else f"{len(assigned)} assigned and none resolvable")
        report.add("agents reach this seat", True,
                   f"nothing to verify — {why}. Stated rather than omitted: an absent "
                   f"check reads the same as a passing one.")

    # **A policy entry this client cannot honour must be VISIBLE.**
    #
    # The per-agent blocked list is matched FQN to FQN exactly, with no
    # last-segment fallback since 2026-09-28. That is the right comparison and
    # it has one failure mode: an entry written in the old short form now
    # matches nobody. The rule the estate meant to apply is silently inert, and
    # silence here fails OPEN -- the sender is delivered.
    #
    # Every other refusal in this client is loud. This one cannot be, because
    # nothing is refused; so the seat reports the entry instead, and names the
    # agent it was authored against.
    strays: list[str] = []
    for fqn, record in (assigned or {}).items():
        for entry in ((record.get("permissions") or {}).get("comms") or {}).get("blocked") or []:
            text = str(entry).strip()
            # An FQN is estate.project.agent -- three non-empty segments. Shape
            # only: nothing is inferred from the parts.  # gate-exempt: SHAPE VALIDATION only — asks whether a string is FQN-shaped and infers no fact from the parts
            if not (text.count(".") == 2 and all(text.split("."))):  # gate-exempt: SHAPE VALIDATION only — asks whether a string is FQN-shaped and infers no fact from the parts
                strays.append(f"{text!r} on {fqn}")
    if strays:
        report.add(
            "policy entries", False,
            "blocked-list entries that are not FQNs and therefore match nobody: "
            + "; ".join(strays)
            + ". The estate authors these at the directory "
            "(permissions.comms.blocked) and they are compared FQN to FQN, "
            "exactly. A short name is not narrowed to a guess — it is reported "
            "here, because an unenforceable block fails OPEN and would "
            "otherwise be invisible. Re-author it as estate.project.agent.")
    elif assigned:
        report.add("policy entries", True,
                   "every blocked-list entry this seat caches is an FQN")

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
    # **The one check whose question is "would a message reach the agent" must
    # not answer YES when none could.**
    #
    # `seat_state_now()` asks the SEAT whether a session is there, and on a
    # deaf seat the answer is a truthful yes -- a session is running. But comms
    # has no channel to receive on, so nothing reaches it. Measured on
    # test-claude 2026-09-29: `PASS deliverable — yes: a message sent now would
    # reach the agent`, on a seat that could not receive at all. That is
    # catalogue 0.58 in the check the question belongs to: green because it
    # could not go red.
    if not (settings.channel or "").strip():
        report.add(
            "deliverable", False,
            "no: this seat has no channel, so a message sent now would reach "
            "nobody however healthy the session is. The seat's own runtime may "
            "be fine -- this is comms having nowhere to listen, not the agent "
            "being absent.")
    else:
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
        # **Policy is per agent, from the directory. There is no seat file.**
        # comms 2.x carries no `comms.yml` (ansible-platform's retire need,
        # operator ruling 2026-09-28), so this reports which of this seat's
        # agents the directory actually speaks for. An agent it says nothing
        # about runs on the channel default -- which is a real configuration,
        # not a fault, but it must be VISIBLE rather than assumed.
        stated, defaulted = [], []
        for fqn in sorted(assigned or {}):
            (stated if agent_partners(fqn, settings.state_dir) is not None
             else defaulted).append(fqn)
        if stated or defaulted:
            report.add("directory", True, "; ".join(filter(None, [
                f"{len(stated)} agent(s) with a partners list from the directory"
                if stated else "",
                (f"{len(defaulted)} on the channel default (no partners stated): "
                 + ", ".join(defaulted)) if defaulted else "",
            ])))
        else:
            report.add("directory", True,
                       "no agents assigned yet, so there is no policy to state")
        refused = [m for m in message_store(settings.state_dir).all() if not m.authorised]
        if refused:
            # The stored flag records the rule in force when the message arrived,
            # so re-check the senders against the directory as it stands now.
            # Otherwise this note reports a seat as blocked when the only thing
            # that changed is the rule — which is how a stale flag becomes a
            # false accusation.
            senders = {m.sender_fqn or m.sender for m in refused}
            still = sorted({
                m.sender_fqn or m.sender for m in refused
                if not may_write_to(hub, m.agent, m.sender, m.sender_fqn,
                                    settings.state_dir)
            })
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
    mine = build_id()
    if daemon_is_running(side) and running_build and running_build != mine:
        # **The same version number is not the same build.** Until 2026-09-27
        # both sides were bare `__version__`, so a daemon still running code
        # from before a reinstall of the same version compared equal and the
        # check reported "daemon and CLI both 2.1.1" -- green because it could
        # not go red (catalogue 0.58). `build_id()` carries a hash of the code
        # on disk, so this fires on the case that actually happens during
        # development: reinstall without a version bump.
        same_version = running_build.split("+")[0] == mine.split("+")[0]
        extra = (
            " Both call themselves "
            f"{mine.split('+')[0]}, and the code differs — a reinstall of the same "
            "version number, which a version comparison cannot see."
            if same_version else "")
        report.add(
            "daemon build", False,
            f"the RUNNING daemon is {running_build}; this CLI is {mine}. "
            "They are out of step, which is a real state during an upgrade and not a "
            f"guess — the daemon is a process and the CLI is whatever is on disk now.{extra} "
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

    last_wake = message_store(settings.state_dir).last_wake(successful=True)
    report.add(
        "wake history", True,
        (f"last successful wake {last_wake['at']}: {last_wake['outcome']}"
         if last_wake else
         "never recorded — this can mean no message has arrived since wake tracking was installed"),
    )

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
    # **An absent channel is not a channel named ''.** A seat with no
    # assignments has none, and including it made `reachable channels` report
    # GRANT WITHOUT SUBSCRIPTION for a channel called nothing, then send the
    # reader to orch to replay provisioning for a drift that did not exist.
    # Measured on test-claude 2026-09-29.
    channels = {c for c in {settings.channel.strip().casefold()} if c}
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


def agent_partners(agent: str, state_dir) -> tuple[str, ...] | None:
    """The ADDRESSED AGENT's own allow-list from the directory, or None.

    **`None` and `()` mean different things and the difference is the whole
    point.** `None` is *the directory says nothing about who may write to this
    agent* — the seat file still governs. `()` would be *the directory says
    nobody may*, which no caller should silently turn into "everybody".

    Until 2026-09-28 comms read the directory's `blocked` and took `partners`
    only from `comms.yml`, so an allow-list authored at the directory was
    ignored while the file's copy decided. Two stores for one fact, and comms
    obeying the one the estate had stopped authoring. Measured on test-claude:
    `bakehouse.agent-eco.test-claude` carried three FQN partners at the
    directory and nothing read them.
    """
    if not agent:
        return None
    from . import config_sync
    try:
        record = config_sync.agent_set(state_dir).get(agent) or {}
    except Exception:  # noqa: BLE001 - an unreadable cache must not decide
        return None
    comms = (record.get("permissions") or {}).get("comms") or {}
    if "partners" not in comms:
        return None
    return tuple(str(e).strip() for e in (comms.get("partners") or ()))


def permits_sender(agent: str, sender_fqn: str, state_dir) -> bool | None:
    """Does the addressed agent's own allow-list admit this sender?

    `True` admitted, `False` refused, **`None` the directory has no opinion**
    — and only `None` falls through to the seat file. Compared FQN to FQN,
    exactly: an entry that is not an FQN matches nobody and is reported by
    `doctor`, never narrowed to a guess.
    """
    allowed = agent_partners(agent, state_dir)
    if allowed is None:
        return None
    theirs = (sender_fqn or "").strip().casefold()
    if not theirs:
        # An unmarked sender states no FQN, so a per-agent allow-list cannot
        # name it. That is the seat file's case, not a silent admission here.
        return None
    return any(e.strip().casefold() == theirs for e in allowed)


def blocks_sender(agent: str, sender_fqn: str, state_dir) -> bool:
    """Does the ADDRESSED AGENT's own directory policy refuse this sender?

    **The directory is the source of truth; the local file is its cache.** The
    agent's `permissions.comms.blocked` is authored at the directory, cached
    here by `config_sync`, and enforced at the receiving end -- the only end
    that can, since the sender is the party being refused.

    Per AGENT, not per seat. `comms.yml` declares one policy for the whole
    seat and cannot say "another1 blocks test-codex while new001 does not",
    which is exactly what the estate authored (UC-03, 2026-09-25). This check
    runs FIRST and the seat-level file still applies to everything it does not
    cover: a per-agent block narrows, it never widens.

    **FQN to FQN, and nothing else.** `sender_fqn` comes from the envelope,
    where the sending comms states its own; a hub display name is never used,
    because it cannot be turned into one -- the directory answers
    `canonical_id: null` for `test-codex` and `agent-eco-test-codex` alike.

    Display-name matching was a bypass twice over. `short_name` splits on
    dots, so a bot arriving as `agent-eco-test-codex` walked through a block
    on `test-codex`; and any hyphen-stripping wide enough to catch that also
    made `blocks-arch` answer to an agent-eco seat's entry of `arch`. There is
    no spelling rule that is both tight enough and wide enough, which is the
    signal that the comparison was on the wrong thing.

    **FQN to FQN, exactly. No last-segment fallback, since 2026-09-28.**
    Until then a short entry was also matched against the last segment of the
    sender's FQN, because the estate wrote short names in these lists. The
    estate now writes FQNs (directory generation 42, measured), so the
    comparison is string equality between two FQNs and nothing is taken apart.

    An entry that is not an FQN therefore matches nothing and is reported by
    `doctor` rather than guessed at: a rule the estate meant to apply and
    spelled in a form this client cannot honour must be visible, not silently
    inert.

    An unmarked bot sender states no FQN and is ignored before policy is
    evaluated. Humans have no FQN by design and remain on the hub-human path.
    """
    if not agent or not sender_fqn:
        return False
    from . import config_sync
    try:
        record = config_sync.agent_set(state_dir).get(agent) or {}
    except Exception:  # noqa: BLE001 - an unreadable cache must not block mail
        return False
    blocked = ((record.get("permissions") or {}).get("comms") or {}).get("blocked") or []
    theirs = sender_fqn.strip().casefold()
    if not theirs:
        return False
    return any(str(entry).strip().casefold() == theirs for entry in blocked)


class SenderUnknown(CommsError):
    """The claimed sender is not one of the agents this seat serves.

    A seat has no FQN. An FQN names an agent session, and a seat that serves
    more than one has no single sender to put in `from:`. The caller states it,
    and the seat's full assignment cache proves that the claim belongs here.
    """

    tag = "sender-unknown"
    exit_code = 1


def _is_full_fqn(value: str) -> bool:
    """Whether *value* has the canonical estate.project.agent FQN syntax."""
    # Directory contract 0.2: exactly three non-empty segments containing only
    # lowercase letters, digits and hyphens. Shape validation only; no identity
    # or route is inferred from any segment.
    return re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+){2}", value) is not None


def sending_agent(explicit: str, state_dir: Path) -> str:
    """The FQN of the agent sending a message. STATED and seat-proven.

    **One rule for every message: `--from` is required and has no default.**
    A message is from an agent to an agent; the seat is only the delivery
    mechanism, mapped to a bot on the hub. comms runs on the seat, so it cannot
    know which of the seat's agents invoked it — and a seat has no FQN of its
    own to fall back on.

    Earlier versions read "the one agent this seat serves" when there was only
    one. That is correct exactly while a seat serves one agent and silently
    wrong the moment it serves two, which is the shape the estate is moving to.
    One rule that always applies beats one that usually does.

    A statement is not enough: it must be an exact key in this seat's full
    assignment cache. That cache is the last verified directory answer and is
    deliberately retained when refresh fails, so an outage neither invents a
    sender nor discards a sender the seat has already proved it serves.
    """
    fqn = (explicit or "").strip()
    if not fqn:
        raise SenderUnknown(
            "every message states the agent it is from: --from "
            "<estate.project.agent>. A message is from an agent to an agent, "
            "and comms cannot tell which agent on this seat is asking."
        )
    assigned = config_sync.agent_set(state_dir)
    if fqn not in assigned:
        served = ", ".join(sorted(assigned)) or "none"
        raise SenderUnknown(
            f"--from {fqn!r} is not an agent assigned to this seat. Nothing was "
            "posted. --from must be one of the exact FQNs in this seat's full "
            f"assignment cache; this seat serves: {served}."
        )
    return fqn


def why_refused(mention, state_dir, in_project: bool = False) -> str:
    """Which rule refused this sender, and WHERE THAT RULE LIVES.

    **All policy is per agent and authored at the directory.** comms 2.x reads
    no `~/.comms/comms.yml`, so there is no second layer to confuse this with
    — which is what the old version of this function existed to get right.

    Measured 2026-09-25, before the file went: a message correctly refused by
    an agent's own blocked list was reported as *"not a permitted partner"* —
    the seat file's sentence — while that file listed the sender in `partners`
    with an empty `blocked`. The decision was right and the explanation sent
    the reader to a file that said the opposite. With one layer there is one
    sentence, and it can only name the rule that fired.
    """
    agent = getattr(mention, "agent", "") or ""
    sender_fqn = getattr(mention, "sender_fqn", "") or ""
    if agent and sender_fqn and blocks_sender(agent, sender_fqn, state_dir):
        return (f"{sender_fqn} is on {agent}'s blocked list, which the estate "
                f"authors at the directory (permissions.comms.blocked) and this "
                f"seat caches in ~/.comms/routes.json. It applies to that agent "
                f"alone, not to this seat")
    allowed = agent_partners(agent, state_dir) if agent else None
    if allowed is not None:
        named = ", ".join(allowed) or "nobody"
        return (f"{sender_fqn or mention.sender} is not on {agent}'s partners "
                f"list, which the estate authors at the directory "
                f"(permissions.comms.partners) and this seat caches in "
                f"~/.comms/routes.json. That list names: {named}")
    if not in_project:
        return (f"{mention.sender} is not in this seat's channel, and the "
                f"directory states no partners list for "
                f"{agent or 'the addressed agent'}, so the channel is what "
                f"decides. Author the list at the directory to say otherwise")
    return (f"{mention.sender} was refused, and no rule this seat can read says "
            f"why — which is itself the fault to report")


def may_write_to(hub: Hub, agent: str, sender: str, sender_fqn: str, state_dir) -> bool:
    """May this sender write to this agent? **The directory decides; no file.**

    comms 2.x carries no `comms.yml` (ansible-platform's retire-comms-yml need,
    operator ruling 2026-09-28). Policy is per AGENT and authored at the
    directory, because a seat is a delivery mechanism and not a party to a
    conversation — it has no FQN, so it cannot be the subject of a rule about
    who may talk to whom.

    The order, and each step is read rather than computed:

    1. **Humans are never governed.** A human has no FQN and the hub display
       name is the address; policy is between agents.
    2. **The agent's own `blocked`** — FQN to FQN, exactly.
    3. **The agent's own `partners`** — if the directory states one, it
       decides, and nothing widens it.
    4. **Otherwise the channel.** The directory has said nothing about this
       agent, so the default is what a seat with no policy has always had:
       anyone subscribed to this seat's channel, no cross-project. That is
       reported by `doctor` per agent, so running on the default is visible
       rather than assumed.

    Measured before this replaced the file: a seat with no `comms.yml` already
    behaved as step 4 (`project: true`, no partners), so removing the file
    widens nothing that was not already the estate's no-file default — and for
    any agent the directory speaks for, step 3 is narrower than the file was.
    """
    # **The directory answers first, because the directory holds the policy.**
    #
    # This asked the hub FIRST until 2026-09-29 — a live Zulip call on every
    # admission, to read one boolean about account type — and any failure of
    # that call made the verdict undeterminable, which stores the message
    # `refused` and loses it. So a blocked sender and an allowed one were both
    # gated behind a network round trip that could decide nothing about either.
    #
    # Both directory checks read `routes.json` from disk. No network. A sender
    # the directory has an opinion about is now decided with the hub untouched.
    if agent and sender_fqn and blocks_sender(agent, sender_fqn, state_dir):
        return False
    admitted = permits_sender(agent, sender_fqn, state_dir)
    if admitted is not None:
        return admitted

    # **Only now the hub, and only for what the directory cannot express.**
    #
    # A human has no FQN, so no directory list can name them: `blocks_sender`
    # and `permits_sender` both decline on an absent `sender_fqn`, which is
    # why moving them ahead of this does not start governing humans. The
    # channel fallback below is for an agent the directory states nothing
    # about — which, since every agent carries an authored partners list, is
    # now rare.
    #
    # **Accepted cost, operator's decision 2026-09-29:** if the hub is
    # unreachable at this point the verdict is undeterminable and the message
    # is lost. That now costs only senders the directory could not decide —
    # humans, and agents with no list — instead of every sender.
    in_project, is_human = hub.in_channel(sender)
    if is_human:
        return True
    # An agent with an authored partners list accepts only canonical FQNs from
    # that list. If a bot could not be resolved, channel membership must not
    # widen the list by admitting its display name.
    if agent_partners(agent, state_dir) is not None:
        return False
    return in_project


def is_permitted(directory: Directory, hub: Hub, sender: str,
                 sender_fqn: str = "") -> bool:
    """May this sender exchange messages with this seat? ADR-0009 §9.

    Declared by the estate in `~/.comms/comms.yml`, never by the seat. Compared
    on the bot's display name, which is what attribution rests on (§1a) — the
    same name a human reads in the channel.

    Membership of "my project" is the hub's answer (the channel's subscriber
    list), not a second roster kept here, so a seat the estate minted this
    morning is permitted this afternoon with no file to edit.
    """
    in_project, is_human = hub.in_channel(sender)
    # **Compare the FQN when the sender states one.** A hub display name names
    # a SEAT, so it cannot name the agent that wrote, and matching on it needed
    # a spelling rule that had to be tight enough to keep `blocks-arch` from
    # matching `arch` and wide enough to catch `agent-eco-test-codex` against
    # `test-codex`. No such rule exists -- which is the signal the comparison
    # was on the wrong thing.
    #
    # A sender that states no FQN still gets the display-name comparison. Not
    # because it is right, but because every seat in the estate is one of those
    # until it upgrades, and refusing them would stop estate comms dead. It
    # narrows to nothing as senders carry the envelope.
    return directory.permits(sender_fqn or sender, in_project=in_project,
                             is_human=is_human)


def addressed_to_seat(
    settings: Settings, msg: dict, flags: list[str], own_email: str | None = None,
    serves: Collection[str] = (),
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
        #
        # **And the marker must name an agent THIS SEAT SERVES.** Asking only
        # whether a marker EXISTS admits every message this seat sends to
        # anyone -- which every seat on the channel then stores and hands to
        # its own default agent. Measured on test-codex 2026-09-25: a message
        # it had sent to `…test-claude-another1` came back through its own
        # daemon as "addressed to an agent on this seat" and was delivered to
        # its own main. A silent delivery to the wrong recipient, one layer up
        # from the one this envelope was built to fix.
        # **Name the agent.** "an agent on this seat" does not say WHICH, so a
        # recipient cannot tell whether the message is for it. Reported by a
        # live agent on test-claude 2026-09-25: it read the unnamed line as
        # "some OTHER agent on my seat", inferred that the addressee shared
        # its seat, and declined to act -- correctly, on a wrong label. A right
        # answer and a wrong answer must not read alike (write-time gate 9).
        marked = envelope_from_body(msg.get("content") or "")
        mine = marked and marked in set(serves or ())
        return f"addressed to {marked}, an agent on this seat" if mine else None

    if "mentioned" in flags:
        return "mentioned"
    if msg.get("type") == "private":
        return "direct message"

    topic = (msg.get("subject") or "").strip()
    prefix = topic.split(":", 1)[0].strip().casefold() if ":" in topic else ""
    if prefix and prefix in {n.strip().casefold() for n in settings.identity.known_names()}:
        return "topic addressed to this seat"
    return None


def mention_from_event(
    site: str,
    event: dict,
    settings: Settings,
    own_email: str | None = None,
    permitted: Callable[[str, str], bool] | None = None,
) -> Mention | None:
    """Turn a Zulip message event into a stored mention, or None if not for us."""
    if event.get("type") != "message":
        return None
    msg = event["message"]
    serves = config_sync.agent_set(settings.state_dir)
    reason = addressed_to_seat(settings, msg, event.get("flags") or [], own_email, serves)
    if reason is None:
        return None
    agent = addressed_agent(msg.get("subject") or "", serves,
                            body=msg.get("content") or "")
    sender = msg.get("sender_full_name") or msg.get("sender_email", "unknown")
    sender_fqn = envelope_sender(msg.get("content") or "")
    return Mention(
        id=msg["id"],
        sender=sender,
        channel=msg.get("display_recipient") if isinstance(msg.get("display_recipient"), str) else "",
        topic=msg.get("subject") or "",
        agent=agent,
        sender_fqn=sender_fqn,
        content=msg.get("content") or "",
        timestamp=msg.get("timestamp", 0),
        permalink=_permalink(site, msg),
        reason=reason,
        authorised=(
            True if permitted is None
            else permitted(sender, sender_fqn)
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
    parts = prefix.split(".")  # gate-exempt: SHAPE VALIDATION only — is this topic prefix FQN-shaped; the value itself is then matched WHOLE against the served set
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


class EmptyMessage(CommsError):
    """The message has no body text. Refused before any lookup or hub call."""

    tag = "empty-message"


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
    from_fqn: str | None = None,
    transport_factory: Callable[[Credential], Transport] = build_transport,
    **kw,
) -> dict:
    """Post to this seat's channel, addressed from one AGENT to another.

    **The protocol is one line: both ends on the wire are FQNs.** `--to` takes
    either the exact recipient FQN or a directory-authored alias. Before posting,
    the directory must resolve it to one canonical FQN; that FQN is written into
    the envelope and new topic. Bot names, unauthored short names, ambiguous
    aliases and unknown inputs are refused. The body is prefixed with a real
    mention of the recipient's seat bot, so both of the routes a recipient
    matches on are covered without the sender knowing which.

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

    The named agent's route is checked against the hub before anything is posted: it must
    exist in the realm and be subscribed to this channel. A message to a seat
    that cannot be reached from here is refused loudly rather than posted into
    the void — which is the failure that started this, and which looks exactly
    like success.
    """
    settings = load_settings(**kw)

    if not (content or "").strip():
        raise EmptyMessage(
            "message body is empty. Nothing was posted. Supply body text; the "
            "FQN envelope and topic identify a message but are not its content."
        )

    sender = sending_agent(from_fqn or "", settings.state_dir)

    if not to or not to.strip():
        raise Unaddressed(
            "nothing to address this to. Every message names its recipient by "
            "FQN: --to <estate.project.agent> --subject '<what it is about>'. "
            "The body is not scanned for addressing, so a name that appears only "
            "in the text reaches nobody."
        )

    recipient = to.strip()
    if not topic:
        if not subject:
            raise Unaddressed(
                f"--to {recipient} needs a --subject, so the topic can be "
                f"'{recipient}: <subject>'. Without one there is no topic to post under."
            )

    # **Resolve before any hub call.** The input is either the canonical FQN or
    # a directory-authored alias. The directory must reduce both to one
    # canonical FQN; bot names and unauthored/ambiguous short names do not.
    routed = _route(settings, recipient, caller=sender)
    if not topic:
        topic = f"{routed.fqn}: {subject}"

    credential = load_credential(settings.identity)
    hub = Hub(transport_factory(credential), settings, credential)
    # **The declared bot must be an account the hub actually has.** Existence,
    # not reachability: a cross-project recipient's bot may legitimately be
    # outside this seat's home channel, and `require_reachable` judges the
    # selected channel below.
    if not hub.in_realm(routed.bot):
        raise UnknownRecipient(
            f"'{recipient}' resolves to {routed.fqn}, whose declared transport "
            f"names the hub identity '{routed.bot}' — and the hub has no such "
            f"account.\n"
            "  Nothing was posted. A message mentioning an account that does not "
            "exist reaches nobody while reporting success.\n"
            "  The bot comes from the directory's `transports.comms` block for "
            "this agent, and nothing is derived from the FQN. So either the hub "
            "account is missing, or the directory declares the wrong bot.")
    recipient, channel = routed.bot, channel or routed.channel

    require_reachable(hub, channel)

    warnings = _mention_warnings(hub, content, channel)
    response = hub.send(channel, topic,
                        addressed(recipient, content,
                                  to_fqn=routed.fqn,
                                  from_fqn=sender))
    return Posted(response=response, warnings=warnings)


@dataclass
class Routed:
    """Where the directory says a name goes, and as whom."""

    fqn: str
    channel: str
    bot: str
    delivery: str


def _route(settings: Settings, name: str, caller: str = "", **kw):
    """Ask the directory to reduce an FQN or authored alias to one FQN.

    Three things happen here that used not to happen at all:

    1. **The input is resolved** against the directory (R7). An exact canonical
       FQN passes; a different canonical id passes only when `alias_used` says
       the directory authored that exact input as an alias.
    2. **The delivery mode is honoured at send** (R10) — `none` refuses here,
       and a value outside `inject | hold | none` refuses with the value
       quoted. Comms is the only component that ever reads this field.
    3. **The transport is derived from the FQN** (R15) — project is the
       channel, agent is the bot — with a declared override taking precedence.

    There is no fallback to a seat, bot, human or inferred short name. Aliases
    are input convenience only: delivery is agent FQN to agent FQN on the wire.
    """
    from .delivery import NotDeliverable, permitted_to_send, plan, transport_for
    from .resolve import Resolver

    # The caller is the SENDING AGENT, stated by --from. The directory decides
    # permissions per agent, so telling it the wrong caller gets the wrong
    # answer -- and there is no seat-level identity to offer instead.
    answer = Resolver(local_agents=config_sync.agent_set(settings.state_dir)).resolve(
        name, caller=caller)

    if not answer.success:
        if answer.status == "not-permitted":
            # A permission DECISION. Refusing here is the point; falling
            # through would turn "you may not address that" into "no such
            # seat", which is the wrong-cause class this change exists to fix.
            raise UnknownRecipient(
                f"'{name}' is not permitted: {answer.message or 'no reason given'}. "
                "Nothing was posted.")
        raise UnknownRecipient(
            f"--to {name!r} could not be resolved to one canonical agent FQN: "
            f"{answer.message or answer.status}. Nothing was posted. Pass an exact "
            "FQN or an alias authored by the directory; bot names, unauthored short "
            "names and ambiguous aliases are refused."
        )

    if not _is_full_fqn(answer.canonical_id):
        raise UnknownRecipient(
            f"--to {name!r} resolved without a valid canonical FQN. Nothing was "
            "posted; the directory answer cannot be put on the wire."
        )

    if answer.canonical_id != name and answer.alias_used != name:
        raise UnknownRecipient(
            f"--to {name!r} returned canonical FQN {answer.canonical_id!r} without "
            "declaring that input as an authored alias. Nothing was posted."
        )

    allowed, why = permitted_to_send(answer.delivery or "inject")
    if not allowed:
        raise UnknownRecipient(f"'{name}' will not be sent to: {why}")

    # **A permission verdict is READ, never computed here.**
    #
    # 733e717 added a matcher on this path: it took the recipient's
    # `permissions.comms.blocked` out of the resolution answer and matched the
    # caller's FQN against the entries, scoping short entries by parsing the
    # project out of the FQN. That is pattern-matching an identifier to reach a
    # permission decision, and the operator has ruled against it repeatedly.
    # Reverted 2026-09-26.
    #
    # The decision is the directory's and it already makes it: its own
    # `/v0/addressable?from=<caller>` EXCLUDES a recipient whose blocked list
    # names that caller. `POST /v0/resolve` for the same pair answers
    # `resolved` — so two directory surfaces disagree, and the fix is there,
    # not a matcher here. Raised with a measurement rather than worked around.
    #
    # `not-permitted` is the verdict comms consumes, and it is handled above
    # where the answer is read. Nothing else is inferred.

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


def _resolve_recipient(settings: Settings, hub: Hub, name: str, caller: str = "") -> str:
    """Return the recipient as Zulip spells it, or refuse and say why.

    Matched case-insensitively so a seat need not know the hub's capitalisation,
    and returned in the hub's own spelling because that is what `@**...**` has to
    contain to resolve. Addressing yourself is refused: a seat ignores its own
    posts, so it is the one mention guaranteed to reach nobody.
    """
    ours = {n.strip().casefold() for n in settings.identity.known_names()}
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
        in_project, is_human = hub.in_channel(match)
        if not (is_human or in_project):
            raise UnknownRecipient(
                f"'{match}' is on the hub but not in this seat's channel, so a "
                "message to it would render and reach nobody. Policy for an agent "
                "is authored at the directory per agent; this path is the legacy "
                "seat-name one and checks only reachability.")
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
    hint = _directory_hint(name, caller=caller)
    if hint:
        raise UnknownRecipient(hint + f"\n  Seats reachable from here: {others}.")
    raise UnknownRecipient(
        f"no seat named '{name}' exists on the hub, and the directory does not "
        f"know it either — check the spelling. Reachable from here: {others}."
    )


def _directory_hint(name: str, caller: str = "", **kw) -> str:
    """What the directory says about a name `send` could not place, or "".

    This is the same answer `comms resolve` gives, brought to the place a
    person actually hits the problem. Best-effort: a directory that cannot be
    reached costs the hint, never the refusal.

    **`caller` is the SENDING AGENT, and it is not optional in effect.**
    Near-misses are caller-relative — the caller's own project comes first
    (UC-08 fact 2) — so the directory answers a nonsense caller with an empty
    set. This function used to pass `caller=name`, the TARGET, which is never a
    real caller: `comms resolve --from <me> arch` listed five candidates and
    `comms send --to arch` got none, from the same directory, in the same
    second. Measured on test-claude 2026-09-27.
    """
    try:
        from .resolve import Resolver
        settings = load_settings(**kw)
        answer = Resolver(
            local_agents=config_sync.agent_set(settings.state_dir)).resolve(
                name, caller=caller or name)
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
        projects = {m.split(".")[1] for m in answer.near_misses  # gate-exempt: a HINT in a refusal message, not a decision. It groups near-misses by project so a person reading 'did you mean' sees them ordered; nothing branches on it
                    if m.count(".") >= 2 and m.rsplit(".", 1)[-1].casefold() == tail}  # gate-exempt: a HINT in a refusal message, not a decision. It groups near-misses by project so a person reading 'did you mean' sees them ordered; nothing branches on it
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
ENVELOPE = re.compile(
    r"^@\*\*[^*]+\*\*\s*(?:`([^`]+)`)?\s*\u2192(?:`([^`]+)`)?")


def envelope_from_body(content: str) -> str:
    """The FQN the sender ADDRESSED, from the marker. Empty if unmarked."""
    m = ENVELOPE.match((content or "").lstrip())
    # `to` is optional -- a reply states its sender and addresses no agent.
    return (m.group(2) or "").strip() if m else ""


def envelope_sender(content: str) -> str:
    """The FQN the message came FROM, from the marker. Empty if unmarked.

    **Policy compares FQN to FQN, never display names.** Current senders state
    their FQN here. A bot message that omits it or puts a non-FQN in this slot
    is legacy and the receive path ignores it. It is not rescued from the bot
    name: one bot can serve several agents, so the transport name cannot say
    which agent wrote the message.

    Matching display names instead is what this replaces, and it was a bypass
    twice over: `short_name` splits on dots, so a bot arriving as
    `agent-eco-test-codex` walked straight through a block on `test-codex`;
    and any hyphen-stripping wide enough to catch it also made `blocks-arch`
    answer to an agent-eco seat's entry of `arch`.

    This is POLICY, not authentication. The hub's bot attribution is what says
    who posted; this says which agent behind that bot. A block keeps an agent
    out of a conversation; it is not a security boundary and was never one.
    """
    m = ENVELOPE.match((content or "").lstrip())
    claimed = (m.group(1) or "").strip() if m else ""
    # A legacy client could put its bot/display name in this slot. That is not
    # an agent identity, so treat it exactly like an absent claim. The receive
    # path ignores both forms and performs no bot-to-FQN lookup.
    return claimed if _is_full_fqn(claimed) else ""


def addressed(sender: str, content: str, to_fqn: str = "", from_fqn: str = "") -> str:
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
    # **Both ends are stated whenever they are known.** A REPLY knows its
    # sender and not its recipient agent -- it answers a seat, in a thread --
    # so `to` is omitted and `from` is still stated. Without that, an upgraded
    # seat's replies looked exactly like a legacy sender's and fell to the
    # display-name comparison for no reason.
    mark = (f" `{from_fqn}`\u2192`{to_fqn}`" if to_fqn and from_fqn
            else f" \u2192`{to_fqn}`" if to_fqn
            else f" `{from_fqn}`\u2192" if from_fqn else "")
    if not name:
        return content
    return f"@**{name}**{mark} {content}"


def reply(
    message_id: int,
    content: str,
    from_fqn: str | None = None,
    transport_factory: Callable[[Credential], Transport] = build_transport,
    **kw,
) -> dict:
    """Reply in the mention's own topic, so the conversation stays one thread.

    **A reply states both ends as FQNs too.** `to:` is the FQN the original
    sender declared in ITS envelope (`sender_fqn`), which is exactly who the
    reply is for — so a reply is addressed as precisely as a send, and the
    receiving seat can dispatch it to the agent that asked rather than to its
    default. Where the original stated no sender FQN, there is no `to:` to
    state: that sender is on a build that predates the envelope.
    """
    settings = load_settings(**kw)
    sender = sending_agent(from_fqn or "", settings.state_dir)
    store = message_store(settings.state_dir)
    target = next((m for m in store.all() if m.id == message_id), None)
    if target is None:
        raise CommsError(f"no message {message_id} in the local store")
    credential = load_credential(settings.identity)
    hub = Hub(transport_factory(credential), settings, credential)
    channel = target.channel or settings.channel
    warnings = _mention_warnings(hub, content, channel)
    result = hub.send(channel, target.topic,
                      addressed(target.sender, content,
                                to_fqn=getattr(target, "sender_fqn", "") or "",
                                from_fqn=sender))
    store.mark_read(message_id)
    return Posted(response=result, warnings=warnings)


@dataclass(frozen=True)
class Woken:
    """What one wake did, as a WORD plus the line a person reads.

    `outcome` is a closed set and it exists because the word used to be the
    first token of a prose sentence, read by callers as
    `startswith("queued")`. That is a prefix-match on a closed word set --
    `queued-for-review` would match `queued` -- and a caller choosing an EXIT
    CODE from it is choosing a consumer surface by guesswork. Write-time
    gate 1; fixed on arch's ruling 2026-09-25.

    `str(Woken)` is the human line, so a caller that echoes it is unchanged.
    """

    outcome: str
    line: str

    DELIVERED = "delivered"
    QUEUED = "queued"
    HELD = "held"
    REFUSED = "refused"

    def __str__(self) -> str:
        return self.line


def _woken(result) -> "Woken":
    return Woken(Woken.DELIVERED if result.success else Woken.REFUSED,
                 result.summary())


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
    - queue-at-capacity / queue-unreadable / runtime-unavailable /
      engine-unavailable → the seat accepted no body; keep it pending without
      consuming an attempt and expose the machine status in the wake record.
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
    except Held as held:
        # `hold`: accepted and stored, never injected. The agent asks for it
        # with `comms inbox`. Not a failure, so nothing is retried and no
        # attempt is consumed; the message simply stays queued and readable.
        store.record_wake_by_hub_id(mid, "held", str(held))
        store.record("info", f"held: {held}")
        return Woken(Woken.HELD, f"held: {held}")
    except WakeError as exc:
        # The seat could not be invoked at all — a different fault from anything
        # the seat reports. The message stays ours and stays queued.
        store.record_wake_by_hub_id(mid, "failed", str(exc))
        store.record("warn", f"delivery could not be attempted for {mid}: {exc}")
        _announce_held(settings, store, mention, str(exc), transport_factory)
        return Woken(Woken.QUEUED, f"queued: {exc}")

    store.record_wake_by_hub_id(mid, result.status, result.summary())

    if result.success:
        store.mark_delivered(mid) if mid is not None else None
        store.set_sleeping(False)
        # **Millisecond stamp on the delivery line.** `events.log` is
        # second-granularity, which was enough until agent-seat asked how long
        # after a session appeared a lost probe was typed — a question the
        # existing logs could not answer at all. The suspected cause is an
        # async redraw of the input line landing on the same keystroke, so the
        # interval that matters is sub-second and a whole-second stamp cannot
        # show it. Added on arch's word, 2026-09-28 (hub 3647).
        store.record("info", f"wake: {result.summary()} "
                             f"[at {datetime.now(tz=timezone.utc).isoformat(timespec='milliseconds')}]")
        return _woken(result)

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
        return _woken(result)

    if not result.retryable:
        # exit 2 — we called the seat wrongly. Ours to fix, and loud about it.
        store.record("warn", f"NOT retrying {mid}: {result.status} exit {result.exit_code} "
                             "is a usage error in this client, not a seat fault")
        _tell_sender(settings, store, mention,
                     f"could not deliver that to my agent: {result.message}",
                     transport_factory)
        return _woken(result)

    _announce_held(settings, store, mention, result.message, transport_factory)
    # Retryable means the seat accepted no durable ownership of the body. Keep
    # the local row pending and tell every caller — including `_flush_pending`
    # and the CLI's exit-code mapping — that this remains queued here.
    return Woken(Woken.QUEUED, f"queued: {result.summary()}")


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
        result = wake({"id": 0, "sender": "agent-comms", "channel": settings.channel,
                       "topic": "retired mail", "content": text,
                       "timestamp": int(time.time()), "permalink": "", "read": False,
                       "reason": "summary", "delivered": False, "attempts": 0,
                       "authorised": True, "retired": ""})
        store.record_wake(None, result.status, result.summary())
    except (WakeError, Exception) as exc:  # noqa: BLE001 — a courtesy must not break a pass
        store.record_wake(None, "failed", str(exc))
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
        except Held as held:
            # `hold`: accepted and stored, never injected. NOT a failure, so no
            # attempt is consumed and the bound is not walked towards -- a held
            # message that burned attempts would be abandoned for obeying its
            # own declared mode. It stays queued and readable; the age bound
            # still applies, so it cannot wait forever.
            store.record_wake_by_hub_id(mention.id, "held", str(held))
            store.record("info", f"held: {held}")
            continue
        except WakeError as exc:
            store.record_wake_by_hub_id(mention.id, "failed", str(exc))
            break  # the seat is not reachable at all; nothing else will land either
        store.record_wake_by_hub_id(mention.id, result.status, result.summary())
        # **The bound is applied HERE, in the same transaction as the attempt.**
        # This used to call a shim that added 1 to the counter and enforced
        # nothing, so `max_attempts` governed no live path at all.
        try:
            if result.success:
                # One accepted handoff is one attempt. The old path first
                # recorded it as a failed attempt and then marked delivery,
                # incrementing the counter twice and shortening the retry
                # budget for later messages.
                store.mark_delivered(mention.id)
                attempts = 0  # inspected only on the failure path below
            elif getattr(result, "deferred_without_attempt", False):
                # Agent-seat 2.9.0 accepted no body. These stable JSON statuses
                # are backpressure or operational state, never parsed prose.
                # Keep the message pending without walking it toward ABANDONED.
                attempts = store.attempt_by_hub_id(
                    mention.id, result.summary(), consumes_attempt=False)
            else:
                attempts = store.attempt_by_hub_id(mention.id, result.summary())
        except ForwardOnly as refused:
            # **Another pass got there first, and that is not this pass's
            # problem.** Two passes overlap whenever `comms daemon --once` runs
            # beside the running daemon -- a normal thing to do, and documented
            # as such. The loser selected this message as undelivered, the
            # winner delivered it, and the store then correctly refuses to move
            # a `delivered` message to `abandoned`.
            #
            # The guard is right; stopping is not. Measured on test-claude
            # 2026-09-28: this exception left the pass, left the daemon loop,
            # and killed the detached daemon, after which the seat read "NOT
            # RECEIVING -- messages sent to this seat are being lost, not
            # queued". A refused write must never cost a seat its mail.
            store.record("info", f"retry: message {mention.id} was already "
                                 f"settled by another pass — {refused}")
            continue
        if not result.success:
            if not result.retryable:
                # broken, or our own usage error. Leave it stored and stop: both
                # need attention rather than another attempt.
                store.record("warn", f"retry: giving up on {mention.id} — "
                                     f"{result.summary()} is not retryable")
                break
            if attempts >= store.max_attempts:
                # Retryable by status, but not in fact. Say so once, loudly, and
                # leave it stored — a person can see it in `comms inbox`, and the
                # queue behind it stops being held hostage.
                store.record(
                    "warn",
                    f"retry: message {mention.id} has failed {attempts} times and is "
                    f"no longer being retried — {result.summary()}. It is still "
                    "stored and visible in `comms inbox`.",
                )
                # `record_attempt` has already moved it to `abandoned`, in the
                # same transaction as the attempt. Nothing to do here but stop.
            break
        landed += 1

    if landed:
        store.set_sleeping(False)
        store.record("info", f"retry: delivered {landed} held message(s)")
    return landed


def _refuse_sender(
    settings: Settings,
    store: Store,
    mention: Mention,
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
        f"refused message {mention.id} from "
        f"{(mention.sender_fqn or mention.sender)!r}: "
        f"{why_refused(mention, settings.state_dir)}",
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
        f"{settings.identity.seat}.** "
        f"{why_refused(mention, settings.state_dir, in_project=in_project)}"
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
    """Start a supervised daemon in the background, detached from this shell.

    A double fork with `setsid` between: the first fork lets this command
    return, `setsid` puts the daemon in its own session so it has no controlling
    terminal, and the second fork means it can never acquire one. The practical
    effect is the one that matters on a seat — **closing the shell, or losing the
    tmux session, no longer takes the daemon with it**, which is how this seat's
    daemon has died more than once.

    The detached process is the supervisor. A recoverable daemon crash is
    logged and restarted with bounded backoff; an invalid configuration still
    fails loudly rather than looping forever.
    """
    settings = load_settings(**kw)
    # The parent must not touch SQLite before forking. sqlite connections are
    # process-local; inheriting one into a detached child is undefined and was
    # observed as open DB/WAL/SHM descriptors in every released daemon.
    store = Store(settings.state_dir)
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
    os.close(devnull)
    os.close(handle)
    os.chdir("/")

    try:
        supervise_daemon(**kw)
    except (SystemExit, KeyboardInterrupt):
        # SIGTERM is the normal `comms daemon --stop` path. The signal handler
        # raises SystemExit so atexit can record the stop; it is not a crash and
        # must not leave a traceback or a fatal warning behind.
        os._exit(0)
    except BaseException as exc:  # noqa: BLE001 - last line before the process ends
        try:
            traceback.print_exc()
            store.record("warn", f"detached daemon exited: {_exception_detail(exc)}")
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
    """Stop the running daemon, if any, then start a supervised detached one.

    Returns `(replaced, pid)` — `replaced` says whether something was actually
    stopped, so the caller can tell "restarted" from "there was nothing running,
    so I started one".

    The detached process supervises recoverable daemon crashes. A host-side
    service is still required to start it after a container or host restart;
    nothing inside this process can survive its container disappearing.

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


def _exception_detail(exc: BaseException) -> str:
    """One useful line for events.log; the full traceback goes to daemon.out."""
    detail = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, sqlite3.Error):
        code = getattr(exc, "sqlite_errorcode", None)
        name = getattr(exc, "sqlite_errorname", None)
        extras = [str(value) for value in (name, code) if value is not None]
        if extras:
            detail += f" [SQLite {' / '.join(extras)}]"
    return detail


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
            traceback.print_exc()
            store.record("warn", f"daemon exited ({_exception_detail(exc)}); "
                         "supervisor restarting")
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
    _reconnects: int = 0,
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

    store.record("info", "comms policy: per agent, from the directory "
                         "(permissions.comms.partners / blocked). This build reads "
                         "no ~/.comms/comms.yml — a seat is not a party to a "
                         "conversation, so it is not the subject of one of these rules.")

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
    store.record_build(build_id())
    store.save_position(registration.queue_id, registration.last_event_id)

    stored, iterations, backoff = 0, 0, 1
    last_backstop = time.monotonic()
    last_config = 0.0
    gap_recovery = False
    startup_recovery = True
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
            # closes it. An undetermined permission is stored, not delivered
            # and not bounced. The current state machine records it as refused;
            # say that truthfully rather than claiming a hold state that was
            # never written. The operator has deferred changing this edge-case
            # state machine.
            undetermined = False
            legacy = False

            def _permitted(name: str, sender_fqn: str) -> bool:
                nonlocal undetermined, legacy
                # **Bot delivery is FQN-only.** A missing or malformed sender
                # claim is legacy, not an invitation to derive an agent from
                # the seat bot. Humans have no FQN by design and stay on their
                # existing hub-human path.
                if not sender_fqn:
                    try:
                        _in_channel, is_human = hub.in_channel(name)
                    except Exception as exc:  # noqa: BLE001 - any hub failure
                        undetermined = True
                        store.record(
                            "warn",
                            f"could not determine whether {name!r} is a human hub "
                            f"sender ({exc}); storing the message as refused and not "
                            "delivering it. No refusal notice was sent because the "
                            "policy answer was unavailable.",
                        )
                        return False
                    if is_human:
                        return True
                    legacy = True
                    return False
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
                        n.strip().casefold() for n in settings.identity.known_names()}:
                    return True
                # The ADDRESSED AGENT's own policy, from the directory, first.
                addressed = addressed_agent(
                    (event.get("message") or {}).get("subject") or "",
                    config_sync.agent_set(settings.state_dir),
                    body=(event.get("message") or {}).get("content") or "")
                try:
                    # **One decision, from the directory.** No seat file is
                    # consulted: comms 2.x carries none.
                    return may_write_to(hub, addressed, name, sender_fqn,
                                        settings.state_dir)
                except Exception as exc:  # noqa: BLE001 - any hub failure
                    undetermined = True
                    store.record(
                        "warn",
                        f"could not determine whether {name!r} may message this seat "
                        f"({exc}); storing the message as refused and not delivering it. "
                        "No refusal notice was sent because the policy answer was unavailable.",
                    )
                    return False

            mention = mention_from_event(
                credential.site, event, settings, credential.email,
                permitted=_permitted,
            )
            if mention is None:
                return False
            if legacy:
                store.record(
                    "info",
                    f"ignored legacy message {mention.id}: no FQN sender",
                )
                return True
            # The addressed agent's declared mode decides the arrival state:
            # `hold` lands in HELD, which is the only state RETRIEVED can be
            # reached from. Read once here rather than at every delivery pass.
            from .wake import holds
            store.append(mention, held=holds(mention.agent or "", settings.state_dir))
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
                    return True  # stored as refused, already logged; no bounce
                _refuse_sender(settings, store, mention, hub,
                               bounced, transport_factory)
                return True
            _notify(settings, store, mention)
            if on_mention is not None:
                on_mention(mention)
            return True

        for event in events:
            handle_event(event)

        # The doorbell rang, but the queue is not the record. Anything that
        # arrived while it was down is in channel history and nowhere else, so
        # read forward from the last message actually handled.
        if startup_recovery or gap_recovery:
            reason = "startup" if startup_recovery else "queue was replaced"
            startup_recovery = False
            gap_recovery = False
            _catch_up(hub, store, handle_event, reason)

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

            # **A seat that had no channel must JOIN when one is assigned.**
            #
            # The daemon reads its channel once, at startup, from its own
            # assignment rows. A seat installed before the estate assigned it
            # anything correctly has none and idles -- but it then idled
            # FOREVER, because nothing re-read the channel when assignments
            # arrived. Measured on test-claude 2026-09-28 (UC-12's
            # zero-assignment control, on fresh drives): generation 48 brought
            # four agents and a channel, and the daemon kept polling with
            # `channel ''`, so every inbound message failed its permission
            # check with "this bot is not subscribed to channel ''".
            #
            # "Idles on refresh until the estate assigns, then joins" is the
            # ruled behaviour (ansible-platform's retire-comms-yml need); the
            # idling half shipped and the joining half did not.
            fresh = load_settings(**kw)
            if (fresh.channel or "").strip() != (settings.channel or "").strip():
                store.record(
                    "info",
                    f"channel changed from {settings.channel or '(none)'!r} to "
                    f"{fresh.channel or '(none)'!r} — reconnecting. A seat with no "
                    "assignments has no channel to join; this is the join.")
                # Bounded: a channel that keeps changing is a fault to report,
                # not something to chase round a loop. Three reconnects is more
                # than a first assignment needs and fewer than a flap costs.
                # **Reconnect in place. Do NOT re-enter `run_daemon`.**
                # This first recursed, and the recursive call tried to take the
                # daemon lock the outer one already held: `DaemonAlreadyRunning`,
                # which would have ended the daemon on the very transition it
                # exists to handle. Caught by this case's own test before it
                # reached a seat.
                _reconnects += 1
                if _reconnects > 3:
                    store.record(
                        "warn",
                        f"channel changed {_reconnects} times this run (now "
                        f"{fresh.channel or '(none)'!r}). Not reconnecting again — "
                        "a channel that will not settle is a directory fault, and "
                        "chasing it would hide that behind a busy daemon.")
                else:
                    settings = fresh
                    hub = Hub(transport_factory(credential), settings, credential)
                    hub.verify_subscription()
                    registration = _register(hub, store)
                    gap_recovery = True   # read history forward into the new channel

        if time.monotonic() - last_backstop >= BACKSTOP_SECS:
            last_backstop = time.monotonic()
            # The queue is ours, so something has to work it. Without this,
            # "stored and will be retried" is a claim nothing honours — the shape
            # this estate has spent a fortnight removing. Same timer as the
            # backstop: both ask "what did the fast path miss?".
            try:
                retry_undelivered(transport_factory=transport_factory, **kw)
            except Exception as exc:                 # noqa: BLE001 — see below
                # **Nothing a pass can raise is worth the daemon.** This caught
                # `CommsError` only, and `ForwardOnly` — the store's own
                # integrity guard — is a plain Exception, so it escaped here and
                # ended the process. A daemon that exits stops the seat
                # receiving *silently*: `comms status` then says "NOT RECEIVING
                # — messages sent to this seat are being lost, not queued",
                # which is the worst outcome this component has.
                #
                # So the net is deliberately wide, and loud to compensate: the
                # exception TYPE is recorded, because a bare message makes a new
                # fault look like a known one. Narrowing this to the exceptions
                # we have already seen is how it was wrong the first time.
                store.record("warn", f"retry pass failed: "
                                     f"{type(exc).__name__}: {exc} — the pass stopped, "
                                     "the daemon did not")
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
#: **The attempt bound lives in ONE place: `Queue.max_attempts`, which is 3.**
#: There were three definitions of it here and in the store, and a shim that
#: counted attempts while enforcing none of them. A message on test-codex
#: reached its EIGHTH attempt against a cap of three — measured by UC-05,
#: 2026-09-25. A bound written down three times and applied nowhere is worse
#: than no bound, because everyone who reads it believes it.

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
    recovered = sum(bool(handle(_event_from_message(msg))) for msg in missed)
    store.record("info",
                 f"{reason}: backfilled {recovered} addressed message(s) from "
                 f"{len(missed)} history row(s) "
                 f"after id {since}")
    return recovered


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
        # Exact match on a named outcome; was a prefix-match until 2026-09-25.
        if outcome.outcome == Woken.QUEUED:
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
    # `retired` still wins: it is the FACT that nobody saw it, where `expired`
    # is only the mechanism that got it there. Reading them the other way round
    # is the conflation that made the 1.0.0 store ambiguous.
    if m.retired:
        return "retired"
    # **The store's own word, when there is one.** Deriving it from the booleans
    # below collapsed nine states into two: `retrieved`, `expired` and
    # `abandoned` all set `delivered`, so a message given up on after three
    # attempts read as delivered and a HELD message read as queued -- hiding
    # exactly the distinctions UC-04 and UC-05 turn on. Measured 2026-09-26.
    if getattr(m, "state", ""):
        return m.state
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
    last_wake = store.last_wake(successful=True)
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
        "last_successful_wake": last_wake["at"] if last_wake else "",
        "last_successful_wake_outcome": last_wake["outcome"] if last_wake else "never",
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


def partners(from_fqn: str = "", **kw) -> list[str]:
    """Who the directory says a claimed sender may address. DISCOVERY ONLY.

    **This is not a permission check and nothing may treat it as one.** `?from`
    is a claim, not identity — anyone may ask about anyone — so this answers
    *what the directory says about a claimed sender*, and the header says so.
    `POST /v0/resolve` at send time remains the authorisation, and the send path
    does not consult this.

    Nothing is cached. A cache of this would be a second copy of a graph only
    the directory can evaluate per caller, and the first thing a reader would do
    is trust it.
    """
    from . import addressable

    answer = addressable.fetch(from_fqn)
    who = answer.claimed_from or "(no sender claimed — the whole estate)"
    out = [
        f"addressable as claimed by {who}",
        "  the directory's answer about a CLAIMED sender — discovery, not permission.",
        "  a send is authorised when it is resolved, not by appearing here.",
        "",
    ]
    if not answer.entries:
        out.append("  nobody. The directory answered, and the set is empty.")
    else:
        out.append(f"  {'FQN':<44} {'LIFECYCLE':<12} {'DELIVERY':<7} CHANNEL/BOT")
        out.extend(e.line() for e in answer.entries)
        out.append("")
        out.append(f"  {len(answer.entries)} addressable")
    out.extend(f"  WARN  {w}" for w in answer.warnings)
    return out


def resolve_name(name: str, from_fqn: str = "", **kw) -> list[str]:
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
    # **`resolve` states its agent too.** It prints a permission verdict, and
    # the directory decides permissions PER CALLER -- so asking as the wrong
    # agent prints the wrong verdict, confidently. Same rule as a send.
    me = sending_agent(from_fqn or "", settings.state_dir)
    answer = Resolver(local_agents=config_sync.agent_set(settings.state_dir)).resolve(
        name, caller=me)

    out = [f"{name}", f"  asked as   {me}", f"  answer     {answer.status}",
           f"  source     {answer.source}" + ("  (degraded)" if answer.degraded else ""),
           f"  because    {answer.reason or answer.message or '—'}"]
    if answer.near_misses:
        out.append(f"  did you mean  {', '.join(answer.near_misses[:5])}")
    if not answer.success:
        # **A refusal and a typo are two situations, so they get two
        # sentences.** Both used to end "NO — nothing would be delivered",
        # which is true of each and tells the reader nothing about which one
        # they are in: one is fixed by correcting the name, the other by
        # asking the estate to change a permission, and the last line sent
        # both readers the same way. One generic sentence serving two
        # situations is what UC-08 exists to kill; ruled 2026-09-28 (arch
        # 3548). The cause is on the `because` line either way — this makes
        # the verdict line carry it too.
        out.append("  would send  NO — the directory refuses this sender"
                   if answer.status == "not-permitted" else
                   "  would send  NO — nothing would be delivered")
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
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET state=?, attempts=0, retired_reason='' WHERE id=?",
            (QUEUED, row["id"]))
        store._log(row["id"], row["state"], QUEUED, f"requeued by hand: {reason}",
                   rule="operator", db=db)
    store.record("info", f"requeued {message_id} by hand: {reason}")
    # **Say the word `trace` says.** `row['state']` is the raw column, and a
    # hand retirement is stored as EXPIRED with a reason beside it -- so this
    # line announced "from expired" about a message the operator had retired
    # by hand thirty seconds earlier, contradicting the `trace` two lines
    # above it. `_state_of` already holds the rule (retired is the FACT,
    # expired only the mechanism that got it there); printing the column
    # instead of asking is how the two surfaces came apart.
    # Measured on UC-07 step 5, 2026-09-27.
    was = "retired" if (row["retired_reason"] or "").strip() else row["state"]
    return (f"requeued {message_id} from {was} — {reason}\n"
            "Attempts reset. The age bound still applies: if it is past the bound "
            "it will retire again on the next pass rather than deliver.")
