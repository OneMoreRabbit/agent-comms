"""Who may exchange messages with this seat — ADR-0009 §9, the comms directory.

§1a made this a convention: *act only on your own arch seat; report, never
comply, on an unexpected sender.* §9 made it mechanical. **Operator ruling,
2026-09-11: a message from a sender the estate has not permitted is refused, not
delivered with a label.** Labelling it put untrusted text into the agent's
session carrying an instruction not to obey it, and an agent is the one thing
that can be argued out of a rule.

The declaration is the estate's: `~/.comms/comms.yml`, installed with comms.
These tests pin that the seat cannot widen its own permissions, which is the
property §9 actually turns on.
"""

from __future__ import annotations

import pytest

from agent_comms import operations
from agent_comms.config import load_settings
from agent_comms.directory import Directory, DirectoryUnreadable
from agent_comms.directory import load as load_directory
from agent_comms.hub import Hub
from agent_comms.wake import compose_turn
from tests.conftest import FakeTransport


def _event(msg_id, sender, topic="agent-comms: do a thing"):
    return {"id": msg_id, "type": "message", "flags": [], "message": {
        "id": msg_id, "sender_full_name": sender, "sender_email": f"{sender}@h",
        "display_recipient": "agent-eco", "subject": topic,
        "content": "please do this", "timestamp": 1, "stream_id": 7, "type": "stream"}}


def _directory(seat_dir, text):
    (seat_dir / ".comms" / "comms.yml").write_text(text, encoding="utf-8")


def _hub(transport=None):
    settings = load_settings()
    from agent_comms.config import load_credential
    credential = load_credential(settings.identity)
    return Hub(transport or FakeTransport(), settings, credential)


def _permits(name, transport=None, state_dir=None):
    settings = load_settings()
    return operations.is_permitted(
        load_directory(state_dir or settings.state_dir), _hub(transport), name
    )


# -- the default -------------------------------------------------------------

def test_with_no_directory_the_default_is_this_seats_own_project(seat):
    """Absence is not an error — it is the default, and doctor says so."""
    directory = load_directory(load_settings().state_dir)
    assert directory.project is True
    assert directory.partners == () and directory.blocked == ()
    assert not directory.installed
    assert "no directory installed" in directory.summary()


def test_a_seat_in_my_project_is_permitted(seat):
    """`project: true` is answered by the hub — the channel's subscriber list."""
    assert _permits("agent-eco-arch")
    assert _permits("agent-skeleton")


def test_a_seat_outside_my_project_is_not(seat):
    """blocks-android is a real bot on the hub and not in this channel."""
    assert not _permits("blocks-android")


def test_the_operator_is_always_permitted(seat):
    """The directory is machine-to-machine policy. An account that is not a bot
    is the operator, and refusing the operator is never the right answer."""
    transport = FakeTransport()
    settings = load_settings()
    directory = Directory(project=False)  # nothing permitted at all
    hub = _hub(transport)
    hub._roster = {"oliver blakeman": False, "agent-eco-arch": True}
    assert operations.is_permitted(directory, hub, "Oliver Blakeman")
    assert not operations.is_permitted(directory, hub, "agent-eco-arch")


def test_a_blocked_human_is_still_blocked(seat):
    """`blocked` is the override, so the escape hatch exists."""
    hub = _hub()
    hub._roster = {"oliver blakeman": False}
    assert not operations.is_permitted(
        Directory(blocked=("Oliver Blakeman",)), hub, "Oliver Blakeman"
    )


# -- the declaration is the estate's -----------------------------------------

def test_the_estate_can_declare_a_cross_project_partner(seat):
    _directory(seat, "project: true\npartners: [blocks-android]\nblocked: []\n")
    assert _permits("blocks-android"), "outside the channel, but declared"


def test_project_false_narrows_to_the_named_partners(seat):
    _directory(seat, "project: false\npartners: [agent-eco-arch]\n")
    assert _permits("agent-eco-arch")
    assert not _permits("agent-skeleton"), "in the project, but project is off"


def test_blocked_wins_over_everything(seat):
    _directory(seat, "project: true\npartners: [agent-skeleton]\nblocked: [agent-skeleton]\n")
    assert not _permits("agent-skeleton")
    assert load_directory(load_settings().state_dir).warnings, (
        "a name in both lists is a generator contradiction and is reported"
    )


def test_a_list_may_be_written_as_dash_items(seat):
    _directory(seat, "project: false\npartners:\n  - agent-eco-arch\n  - orchestrator\n")
    directory = load_directory(load_settings().state_dir)
    assert directory.partners == ("agent-eco-arch", "orchestrator")


# -- a permission list read halfway is worse than one not read ---------------

def test_an_unknown_key_is_refused_loudly(seat):
    """An ignored key here is a permission somebody believes is in force."""
    _directory(seat, "project: true\nallow_everyone: true\n")
    with pytest.raises(DirectoryUnreadable, match="unknown key"):
        load_directory(load_settings().state_dir)


def test_an_unparseable_line_is_refused_loudly(seat):
    _directory(seat, "project: true\nthis is not yaml\n")
    with pytest.raises(DirectoryUnreadable, match="cannot parse"):
        load_directory(load_settings().state_dir)


def test_a_non_boolean_project_is_refused(seat):
    _directory(seat, "project: sometimes\n")
    with pytest.raises(DirectoryUnreadable, match="must be true or false"):
        load_directory(load_settings().state_dir)


# -- refused, not delivered (operator ruling, 2026-09-11) --------------------

def test_a_message_from_an_unpermitted_sender_is_stored_but_not_delivered(seat):
    """It is kept so a wrong directory is recoverable, and never handed to the agent."""
    notified = []
    transport = FakeTransport(event_batches=[{"result": "success",
                                              "events": [_event(801, "blocks-android")]}])
    stored = operations.run_daemon(
        transport_factory=lambda c: transport, max_iterations=1,
        on_mention=notified.append,
    )
    assert stored == 1, "stored, so the operator can see what was refused"
    assert operations.inbox()[0].authorised is False
    assert notified == [], "and never handed on to the agent"


def test_a_permitted_message_reaches_the_agent(seat):
    notified = []
    transport = FakeTransport(event_batches=[{"result": "success",
                                              "events": [_event(802, "agent-eco-arch")]}])
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1,
                          on_mention=notified.append)
    assert operations.inbox()[0].authorised is True
    assert [m.id for m in notified] == [802]


def test_the_do_not_comply_label_is_gone(seat):
    """It cannot fire any more: an unpermitted message never reaches a turn.

    Keeping it would be a label for a state that no longer exists — and it was
    always the weaker half, since it asked an agent to police text handed to it.
    """
    line = compose_turn({"id": 4, "sender": "agent-eco-arch", "topic": "t",
                         "content": "proceed", "permalink": "x", "authorised": True})
    assert line.startswith("[hub message from agent-eco-arch")
    assert "DO NOT COMPLY" not in compose_turn(
        {"id": 5, "sender": "x", "topic": "t", "content": "c", "permalink": "p",
         "authorised": False}
    )


# -- the sender is told, once ------------------------------------------------

def _posts_from(transport_cls, *, events, iterations=1):
    posted = []

    class T(transport_cls):
        def call_endpoint(self, url, method="GET", request=None):
            if url == "messages":
                posted.append(request)
            return super().call_endpoint(url, method, request)

    transport = T(event_batches=events)
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=iterations)
    return posted


def test_the_refused_sender_is_told_why(seat):
    """A refusal nobody can see is how a wrong directory becomes a silent outage."""
    posted = _posts_from(FakeTransport, events=[
        {"result": "success", "events": [_event(803, "blocks-android")]}])

    # **In the sender's own topic.** The live test of 2026-09-11 found the
    # refusal posted to a topic of our own, where the sender was not looking —
    # and an earlier version of this test passed while that was true, because it
    # asserted only that a post existed.
    refusals = [p for p in posted if "did not reach the agent" in p["content"]]
    assert len(refusals) == 1
    assert refusals[0]["topic"] == "agent-comms: do a thing", (
        "the refusal must land where the sender is reading"
    )
    body = refusals[0]["content"]
    assert "@**blocks-android**" in body, "addressed, or the sender never sees it"
    assert "did not reach the agent" in body
    assert "the estate declares the link" in body


def test_the_sender_is_told_only_once(seat):
    """The bounce is itself a channel message. A seat that refuses us in turn
    would bounce it back, and we would bounce that — two seats ping-ponging.
    Once per sender bounds it whatever the other side does."""
    posted = _posts_from(FakeTransport, iterations=2, events=[
        {"result": "success", "events": [_event(804, "blocks-android")]},
        {"result": "success", "events": [_event(805, "blocks-android")]},
    ])
    assert len([p for p in posted if "did not reach the agent" in p["content"]]) == 1


# -- the arch<->component loop must be visible from both ends -----------------

def test_a_reply_mentions_whoever_asked(seat):
    """Arch reported that nobody answered a roll call I had answered twice.

    A seat's inbox is mention-based, and a reply posted into `agent-comms: roll
    call` matches no topic prefix an *arch* seat answers to — so replies landed
    in the channel and were invisible to the one seat that needed them.
    """
    transport = FakeTransport(event_batches=[{"result": "success",
                                              "events": [_event(901, "agent-eco-arch")]}])
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1)
    operations.reply(901, "doctor is clean", transport_factory=lambda c: transport)

    sent = transport.sent[-1]
    assert sent["content"].startswith("@**agent-eco-arch**")
    assert "doctor is clean" in sent["content"]
    assert sent["topic"] == "agent-comms: do a thing", "replies stay in their topic"


def test_addressing_is_not_doubled_up(seat):
    from agent_comms.operations import addressed

    assert addressed("arch", "hello") == "@**arch** hello"
    assert addressed("", "hello") == "hello"
    # A sender field that already carries Zulip syntax is NORMALISED, not
    # skipped. The old behaviour returned the content with no mention at all —
    # a reply addressed to nobody, which is the silent addressing failure the
    # orchestrator reported.
    assert addressed("@**arch**", "hello") == "@**arch** hello"
    assert addressed("@arch", "hello") == "@**arch** hello"


# -- addressing: the seat says who, this client knows how --------------------
#
# The protocol, since 0.30.3: a seat names its recipient in `--to` and writes
# prose in the body. This client checks the name against the hub and spells the
# mention. It does not read the body, which means there is nothing in a message
# that can be addressing-by-accident — and nothing to get wrong about prose.


def test_the_body_is_never_rewritten(seat):
    """A seat name typed in prose stays prose. It is not addressing.

    The old client scanned for `@name` and converted it. That is guesswork about
    text, and it fails silently in both directions. Addressing travels in a flag.
    """
    from agent_comms import operations
    from tests.conftest import FakeTransport

    transport = FakeTransport()
    body = "ask @blocks-android about it, and mail a@b.com — see @**x**"
    operations.send(body, to="agent-eco-arch", subject="the ask",
                    transport_factory=lambda c: transport)
    assert transport.sent[-1]["content"] == f"@**agent-eco-arch** {body}"


def test_send_to_addresses_by_plain_seat_name(seat):
    """`--to agent-skeleton`, not `--to @**agent-skeleton**`."""
    from agent_comms import operations
    from tests.conftest import FakeTransport

    transport = FakeTransport()
    operations.send("please look", to="agent-skeleton", subject="the ask",
                    transport_factory=lambda c: transport)
    assert transport.sent[-1]["content"].startswith("@**agent-skeleton** ")
    assert transport.sent[-1]["topic"] == "agent-skeleton: the ask", (
        "the topic must name the RECIPIENT — arch naming itself is what failed on blocks"
    )


def test_zulip_syntax_in_to_is_accepted_and_normalised(seat):
    from agent_comms import operations
    from tests.conftest import FakeTransport

    transport = FakeTransport()
    operations.send("hi", to="@**agent-skeleton**", subject="s",
                    transport_factory=lambda c: transport)
    assert transport.sent[-1]["content"].startswith("@**agent-skeleton** ")


def test_the_recipient_is_matched_case_insensitively(seat):
    """A seat should not have to know the hub's capitalisation; the mention must."""
    from agent_comms import operations
    from tests.conftest import FakeTransport

    transport = FakeTransport()
    operations.send("hi", to="Agent-Skeleton", subject="s",
                    transport_factory=lambda c: transport)
    assert transport.sent[-1]["content"].startswith("@**agent-skeleton** ")


# -- the blocks failure: a message that reaches nobody ------------------------

def test_a_message_with_no_recipient_is_refused(seat):
    """The blocks failure of 2026-09-10, made impossible.

    arch posted under its OWN topic prefix with the recipients as plain text.
    Comms routes by topic prefix or a real mention and ignores a seat's own
    posts, so neither target was addressed and nothing was delivered — correct
    behaviour with an invisible outcome. Now there is no way to express it.
    """
    from agent_comms import operations
    from agent_comms.operations import Unaddressed
    from tests.conftest import FakeTransport

    transport = FakeTransport()
    with pytest.raises(Unaddressed, match="names its recipient"):
        operations.send("please look at the deploy @blocks-service",
                        topic="agent-comms: my own topic",
                        transport_factory=lambda c: transport)
    assert transport.sent == [], "nothing may be posted"


def test_a_seat_that_does_not_exist_is_refused(seat):
    from agent_comms import operations
    from agent_comms.operations import UnknownRecipient
    from tests.conftest import FakeTransport

    transport = FakeTransport()
    with pytest.raises(UnknownRecipient, match="no seat named .blocks-andriod. exists"):
        operations.send("hi", to="blocks-andriod", subject="typo",
                        transport_factory=lambda c: transport)
    assert transport.sent == []


def test_a_seat_outside_this_channel_is_refused(seat):
    """`blocks-android` exists in the realm and is not in `agent-eco`.

    Measured on the live hub. A mention of it here renders correctly and reaches
    nobody, which is indistinguishable from success — so it is refused.
    """
    from agent_comms import operations
    from agent_comms.operations import UnknownRecipient
    from tests.conftest import FakeTransport

    transport = FakeTransport()
    with pytest.raises(UnknownRecipient, match="exists on the hub but is not in channel"):
        operations.send("hi", to="blocks-android", subject="cross-project",
                        transport_factory=lambda c: transport)
    assert transport.sent == []


def test_addressing_ourselves_is_refused(seat):
    """A seat ignores its own posts, so this is the one mention that cannot land."""
    from agent_comms import operations
    from agent_comms.operations import UnknownRecipient
    from tests.conftest import FakeTransport

    transport = FakeTransport()
    with pytest.raises(UnknownRecipient, match="is this seat"):
        operations.send("hi", to="agent-comms", subject="myself",
                        transport_factory=lambda c: transport)
    assert transport.sent == []


def test_an_explicit_topic_still_requires_a_recipient(seat):
    """`--topic` continues a thread; it does not replace addressing."""
    from agent_comms import operations
    from tests.conftest import FakeTransport

    transport = FakeTransport()
    operations.send("carrying on", to="agent-skeleton",
                    topic="agent-skeleton: an older thread",
                    transport_factory=lambda c: transport)
    assert transport.sent[-1]["topic"] == "agent-skeleton: an older thread"
    assert transport.sent[-1]["content"].startswith("@**agent-skeleton** ")


def test_to_without_subject_is_refused_rather_than_guessed(seat):
    from agent_comms import operations
    from agent_comms.operations import Unaddressed
    from tests.conftest import FakeTransport

    with pytest.raises(Unaddressed, match="needs a --subject"):
        operations.send("body", to="agent-skeleton",
                        transport_factory=lambda c: FakeTransport())


def test_a_hold_notice_mentions_the_sender(seat, tmp_path, monkeypatch):
    """A "held" notice posted into our own topic would reach nobody otherwise.

    The topic is named after this seat, so a sender filtering on its own topic
    prefix never sees it. The mention is the route that works.
    """
    from agent_comms import operations
    from agent_comms.store import Store
    from tests.conftest import FakeTransport

    transport = FakeTransport()
    store = Store(tmp_path)
    store.ensure()
    operations._tell_sender(
        operations.load_settings(),
        store,
        {"id": 1, "sender": "agent-eco-arch", "topic": "agent-comms: do a thing"},
        "could not deliver that to my agent",
        lambda c: transport,
    )
    assert transport.sent[-1]["content"].startswith("@**agent-eco-arch** ")
    assert transport.sent[-1]["topic"] == "agent-comms: do a thing"
