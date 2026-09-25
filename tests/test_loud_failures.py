"""The five conditions contract §3 forbids from passing quietly.

Each test asserts the *behaviour the contract promises*, not the implementation:
a consumer pinning 0.1 is entitled to exactly these.
"""

from __future__ import annotations

import click
import pytest

import time as _time

NOW = int(_time.time())

from agent_comms import operations
from agent_comms.config import Settings, load_credential, load_settings
from agent_comms.errors import (
    DaemonAlreadyRunning,
    CommsDisabled,
    CredentialMissing,
    CredentialUnreadable,
    InsecureTransportRefused,
    NotSubscribed,
    QueueGapError,
)
from agent_comms.hub import Hub, Registration
from tests.conftest import FakeTransport, without_wake_trigger


# -- 1. credential missing vs comms disabled ---------------------------------

def test_disabled_seat_is_not_an_error(seat):
    """No config, no credential: a seat without comms, not a broken seat."""
    (seat / ".comms" / "config.toml").unlink()
    st = operations.status()
    assert st.enabled is False
    assert st.tag == "disabled"
    assert "normal resting state" in st.detail


def test_enabled_without_credential_is_loud(seat):
    """Enabled and no credential is broken, and says so — the §3 distinction."""
    (seat / ".secrets" / "zuliprc-agent-eco-agent-comms").unlink()
    st = operations.status()
    assert st.enabled is True and st.ready is False
    assert st.tag == "credential-missing"
    assert "indistinguishable from 'comms disabled'" in st.detail


def test_the_two_states_are_never_conflated(seat):
    """The same filesystem shape must not produce the same answer."""
    cred = seat / ".secrets" / "zuliprc-agent-eco-agent-comms"
    cred.unlink()
    broken = operations.status()
    (seat / ".comms" / "config.toml").unlink()
    quiet = operations.status()
    assert broken.tag != quiet.tag
    assert broken.enabled and not quiet.enabled


def test_world_readable_credential_refused(seat):
    cred = seat / ".secrets" / "zuliprc-agent-eco-agent-comms"
    cred.chmod(0o644)
    with pytest.raises(CredentialUnreadable, match="must be 0600"):
        load_credential(load_settings().identity)


def test_credential_missing_api_section(seat):
    cred = seat / ".secrets" / "zuliprc-agent-eco-agent-comms"
    cred.write_text("[wrong]\nemail=a\n", encoding="utf-8")
    cred.chmod(0o600)
    with pytest.raises(CredentialUnreadable, match="no \\[api\\] section"):
        load_credential(load_settings().identity)


# -- 2. untrusted TLS --------------------------------------------------------

def test_insecure_flag_in_credential_refused(seat):
    """The estate could deliver one; we will not honour it."""
    cred = seat / ".secrets" / "zuliprc-agent-eco-agent-comms"
    cred.write_text(
        "[api]\nemail=a@b.c\nkey=k\nsite=https://agent.onemorerabbit.co.uk\ninsecure=true\n",
        encoding="utf-8",
    )
    cred.chmod(0o600)
    with pytest.raises(InsecureTransportRefused, match="no insecure mode"):
        load_credential(load_settings().identity)


def test_insecure_env_var_refused(seat, monkeypatch):
    monkeypatch.setenv("ZULIP_ALLOW_INSECURE", "1")
    with pytest.raises(InsecureTransportRefused, match="ZULIP_ALLOW_INSECURE"):
        load_credential(load_settings().identity)


def test_plain_http_site_refused(seat):
    cred = seat / ".secrets" / "zuliprc-agent-eco-agent-comms"
    cred.write_text("[api]\nemail=a@b.c\nkey=k\nsite=http://agent.onemorerabbit.co.uk\n", encoding="utf-8")
    cred.chmod(0o600)
    with pytest.raises(Exception, match="must be https"):
        load_credential(load_settings().identity)


# -- 3. bot not subscribed ---------------------------------------------------

def test_unsubscribed_bot_refuses_to_start(seat):
    """Registers fine, polls fine, receives nothing — so we refuse at connect."""
    settings = load_settings()
    credential = load_credential(settings.identity)
    hub = Hub(FakeTransport(subscriptions=["some-other-channel"]), settings, credential)
    with pytest.raises(NotSubscribed, match="indistinguishable from a quiet day"):
        hub.verify_subscription()


def test_subscribed_bot_passes(seat):
    settings = load_settings()
    credential = load_credential(settings.identity)
    hub = Hub(FakeTransport(subscriptions=["agent-eco"]), settings, credential)
    hub.verify_subscription()


def test_daemon_refuses_to_run_unsubscribed(seat):
    with pytest.raises(NotSubscribed):
        operations.run_daemon(
            transport_factory=lambda c: FakeTransport(subscriptions=["nope"]),
            max_iterations=1,
        )


# -- 4. lifespan not honoured ------------------------------------------------

def test_lifespan_is_requested_at_3600(seat):
    settings = load_settings()
    credential = load_credential(settings.identity)
    transport = FakeTransport()
    Hub(transport, settings, credential).register_queue()
    assert transport.register_calls[0]["lifespan_secs"] == 3600


def test_estates_pinned_server_does_not_warn(seat):
    """Zulip 10.4 cannot echo the lifespan, but the estate source-verified it.

    Warning here fired on every connect on every seat and could not be acted on —
    the exact noise §3 exists to prevent, produced by §3's own machinery.
    """
    settings = load_settings()
    credential = load_credential(settings.identity)
    reg = Hub(FakeTransport(), settings, credential).register_queue()
    assert reg.warnings == []
    assert any("Honoured" in n for n in reg.notes)


def test_unknown_old_server_still_warns(seat):
    """A server nobody has checked is the case the warning is actually for."""
    settings = load_settings()
    credential = load_credential(settings.identity)
    transport = FakeTransport(register_result={
        "result": "success", "queue_id": "q1", "last_event_id": 0,
        "zulip_version": "9.1", "zulip_feature_level": 300,
    })
    reg = Hub(transport, settings, credential).register_queue()
    assert any("Nobody has checked this combination" in w for w in reg.warnings)


def test_new_server_mismatch_warns(seat):
    settings = load_settings()
    credential = load_credential(settings.identity)
    transport = FakeTransport(register_result={
        "result": "success", "queue_id": "q1", "last_event_id": 0,
        "zulip_version": "12.0", "zulip_feature_level": 500,
        "idle_queue_timeout_secs": 600,
    })
    reg = Hub(transport, settings, credential).register_queue()
    assert any("lifespan mismatch" in w and "600s" in w for w in reg.warnings)


def test_new_server_honouring_lifespan_is_silent(seat):
    settings = load_settings()
    credential = load_credential(settings.identity)
    transport = FakeTransport(register_result={
        "result": "success", "queue_id": "q1", "last_event_id": 0,
        "zulip_version": "12.0", "zulip_feature_level": 500,
        "idle_queue_timeout_secs": 3600,
    })
    reg = Hub(transport, settings, credential).register_queue()
    assert reg.warnings == []


def test_new_server_missing_echo_is_not_assumed_good(seat):
    """A level that should echo, but did not, is unverified — not assumed honoured."""
    settings = load_settings()
    credential = load_credential(settings.identity)
    transport = FakeTransport(register_result={
        "result": "success", "queue_id": "q1", "last_event_id": 0,
        "zulip_version": "12.0", "zulip_feature_level": 500,
    })
    reg = Hub(transport, settings, credential).register_queue()
    assert any("no value came back" in w for w in reg.warnings)


# -- 5. BAD_EVENT_QUEUE_ID ---------------------------------------------------

def test_collected_queue_raises_with_its_window(seat):
    settings = load_settings()
    credential = load_credential(settings.identity)
    transport = FakeTransport(event_batches=[
        {"result": "error", "code": "BAD_EVENT_QUEUE_ID", "queue_id": "q1"},
    ])
    hub = Hub(transport, settings, credential)
    reg = Registration(queue_id="q1", last_event_id=0)
    with pytest.raises(QueueGapError, match="3600s of inactivity"):
        hub.get_events(reg)


def test_daemon_reregisters_and_records_the_gap(seat):
    """The silence is the danger, not the gap: it must reach the durable log."""
    transport = FakeTransport(event_batches=[
        {"result": "error", "code": "BAD_EVENT_QUEUE_ID", "queue_id": "q1"},
        {"result": "success", "events": []},
    ])
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=2)
    log = (seat / ".comms" / "events.log").read_text(encoding="utf-8")
    assert "garbage-collected" in log and "Re-registering" in log
    assert len(transport.register_calls) == 2


def test_connect_findings_reach_the_durable_log(seat):
    """A finding that only ever hit a daemon's stderr is the silence §3 forbids."""
    operations.run_daemon(transport_factory=lambda c: FakeTransport(), max_iterations=1)
    log = (seat / ".comms" / "events.log").read_text(encoding="utf-8")
    assert "Honoured" in log


# -- doctor must not report the resting state as a failure -------------------

def test_doctor_treats_disabled_as_a_state_not_a_failure(seat):
    """Our own output must not repeat the conflation §3 forbids."""
    (seat / ".comms" / "config.toml").unlink()
    report = operations.preflight()
    assert report.disabled is True
    assert all(passed for _, passed, _ in report.checks)


def test_doctor_reports_every_check_not_just_the_first(running_daemon, monkeypatch):
    # The seat build is a real subprocess otherwise, so this test would report
    # whatever build the machine running it happens to carry.
    monkeypatch.setattr("agent_comms.operations.seat_state_now",
                        lambda: __import__("agent_comms.seat", fromlist=["x"]).SeatState(
                            answer="yes", reason="a message sent now would reach the agent",
                            runtime="claude", sessions=1, version="1.0.2", contract="1.0"))
    """An operator debugging a seat wants the whole picture."""
    report = operations.preflight(transport_factory=lambda c: FakeTransport())
    names = [n for n, _, _ in report.checks]
    assert names == ["enabled", "credential", "identity", "subscription",
                     # "reachable channels" added 2026-09-22: a routing record naming a
                     # channel this bot is not subscribed to is silent non-delivery
                     # waiting to happen, and doctor is where it is found out.
                     "reachable channels",
                     "event queue", "deliverable", "directory", "seat build", "daemon",
                     "wake trigger"]
    assert report.ok
    assert report.warnings == []
    assert any("Honoured" in n for n in report.notes)


# -- attribution: the bot must be who the vault thinks it is -----------------

def test_component_bot_name_is_accepted_silently(running_daemon, monkeypatch):
    monkeypatch.setattr("agent_comms.operations.seat_state_now",
                        lambda: __import__("agent_comms.seat", fromlist=["x"]).SeatState(
                            answer="yes", reason="a message sent now would reach the agent",
                            runtime="claude", sessions=1, version="1.0.2", contract="1.0"))
    """ADR-0009 §7a: a component bot appears only in its project's channel, so
    the seat name alone is unambiguous there. Warning about it would be noise."""
    report = operations.preflight(
        transport_factory=lambda c: FakeTransport(full_name="agent-comms")
    )
    assert report.ok
    assert not any("bot is named" in w for w in report.warnings)


def test_arch_shaped_bot_name_is_also_accepted(seat):
    """An arch bot carries its project because it appears in several channels."""
    report = operations.preflight(
        transport_factory=lambda c: FakeTransport(full_name="agent-eco-agent-comms")
    )
    assert not any("bot is named" in w for w in report.warnings)


def test_unrecognisable_bot_name_is_reported(seat):
    """§7a's requirement is unambiguity: a name that traces back to no seat fails it."""
    report = operations.preflight(
        transport_factory=lambda c: FakeTransport(full_name="zulip-bot-3")
    )
    assert any("does not identify the seat" in w for w in report.warnings)


def test_human_account_credential_is_reported(seat):
    report = operations.preflight(
        transport_factory=lambda c: FakeTransport(is_bot=False)
    )
    assert any("human account" in w for w in report.warnings)


def test_seat_named_credential_is_the_primary_path(seat):
    """What a component seat actually gets, per §7a — and it is not a divergence."""
    contracted = seat / ".secrets" / "zuliprc-agent-eco-agent-comms"
    fallback = seat / ".secrets" / "zuliprc-agent-comms"
    contracted.rename(fallback)
    fallback.chmod(0o600)
    cred = load_credential(load_settings().identity)
    assert cred.source == str(fallback)
    assert cred.notices == []


def test_project_named_credential_still_works(seat):
    """What an arch seat gets. Both shapes are live in the estate."""
    cred = load_credential(load_settings().identity)
    assert cred.source.endswith("zuliprc-agent-eco-agent-comms")
    assert cred.notices == []


# -- the daemon ---------------------------------------------------------------

def test_daemon_resumes_a_stored_queue_rather_than_re_registering(seat):
    """Re-registering when a queue was held silently forfeits the gap."""
    from agent_comms.store import Store
    store = operations.message_store(seat / ".comms")
    store.save_position("q-existing", 42)
    transport = FakeTransport()
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1)
    assert transport.register_calls == []
    assert "resuming queue q-existing" in (seat / ".comms" / "events.log").read_text()


def test_resume_does_not_discard_events(seat):
    """A resume probe that fetched and dropped events would lose them silently."""
    from agent_comms.store import Store
    Store(seat / ".comms").save_position("q-existing", 42)
    transport = FakeTransport(event_batches=[{"result": "success", "events": [
        {"id": 43, "type": "message", "flags": ["mentioned"], "message": {
            "id": 401, "sender_full_name": "agent-eco-arch", "display_recipient": "agent-eco",
            "subject": "t", "content": "must not be dropped",
            "timestamp": 1, "stream_id": 7}},
    ]}])
    stored = operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1)
    assert stored == 1, "the first batch after a resume must reach the store"


def test_dead_stored_queue_falls_back_to_registering(seat):
    from agent_comms.store import Store
    Store(seat / ".comms").save_position("q-dead", 1)
    transport = FakeTransport(event_batches=[
        {"result": "error", "code": "BAD_EVENT_QUEUE_ID", "queue_id": "q-dead"},
        {"result": "success", "events": []},
    ])
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=2)
    assert len(transport.register_calls) == 1
    assert "garbage-collected" in (seat / ".comms" / "events.log").read_text()


def test_daemon_stores_only_mentions(seat):
    """A project channel carries every conversation; only ours is ours."""
    transport = FakeTransport(event_batches=[{"result": "success", "events": [
        {"id": 1, "type": "message", "flags": ["mentioned"], "message": {
            "id": 101, "sender_full_name": "agent-eco-arch", "display_recipient": "agent-eco",
            "subject": "agent-comms: build it", "content": "please proceed",
            "timestamp": 1756900000, "stream_id": 7}},
        {"id": 2, "type": "message", "flags": [], "message": {
            "id": 102, "sender_full_name": "someone", "display_recipient": "agent-eco",
            "subject": "other", "content": "chatter", "timestamp": 1756900001, "stream_id": 7}},
    ]}])
    stored = operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1)
    assert stored == 1
    rows = operations.inbox()
    assert len(rows) == 1 and rows[0].id == 101
    assert "/#narrow/channel/7-agent-eco/topic/" in rows[0].permalink


def test_notify_command_receives_the_mention(seat, tmp_path):
    """The hand-off to the comms conversation — never the working session."""
    out = tmp_path / "notified.json"
    (seat / ".comms" / "config.toml").write_text(
        f'enabled = true\nnotify_command = "cat > {out}"\n', encoding="utf-8"
    )
    transport = FakeTransport(event_batches=[{"result": "success", "events": [
        {"id": 1, "type": "message", "flags": ["mentioned"], "message": {
            "id": 201, "sender_full_name": "agent-eco-arch", "display_recipient": "agent-eco",
            "subject": "agent-comms: ping", "content": "hello",
            "timestamp": 1756900000, "stream_id": 7}},
    ]}])
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1)
    assert '"id": 201' in out.read_text(encoding="utf-8")


def test_failing_notify_command_is_recorded_not_swallowed(seat):
    (seat / ".comms" / "config.toml").write_text(
        'enabled = true\nnotify_command = "exit 7"\n', encoding="utf-8"
    )
    transport = FakeTransport(event_batches=[{"result": "success", "events": [
        {"id": 1, "type": "message", "flags": ["mentioned"], "message": {
            "id": 202, "sender_full_name": "agent-eco-arch", "display_recipient": "agent-eco",
            "subject": "t", "content": "c", "timestamp": 1, "stream_id": 7}},
    ]}])
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1)
    assert "notify_command exited 7" in (seat / ".comms" / "events.log").read_text()


def test_reply_goes_to_the_mentions_own_topic(seat):
    transport = FakeTransport(event_batches=[{"result": "success", "events": [
        {"id": 1, "type": "message", "flags": ["mentioned"], "message": {
            "id": 301, "sender_full_name": "agent-eco-arch", "display_recipient": "agent-eco",
            "subject": "agent-comms: a question", "content": "?",
            "timestamp": 1, "stream_id": 7}},
    ]}])
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1)
    operations.reply(301, "answered", transport_factory=lambda c: transport)
    assert transport.sent[-1]["topic"] == "agent-comms: a question"
    assert operations.inbox() == []


# -- one daemon per seat ------------------------------------------------------

def test_second_daemon_refuses_to_start(seat):
    """Two daemons on one bot means every mention is processed twice."""
    from agent_comms.errors import DaemonAlreadyRunning
    from agent_comms.store import Store

    held = Store(seat / ".comms").acquire_daemon_lock()
    try:
        with pytest.raises(DaemonAlreadyRunning, match="two event queues"):
            operations.run_daemon(
                transport_factory=lambda c: FakeTransport(), max_iterations=1
            )
    finally:
        held.close()


def test_lock_is_released_when_the_holder_goes(seat):
    """An flock dies with the process, so a killed daemon leaves nothing to clear."""
    from agent_comms.store import Store

    store = operations.message_store(seat / ".comms")
    store.acquire_daemon_lock().close()
    operations.run_daemon(transport_factory=lambda c: FakeTransport(), max_iterations=1)


def test_already_running_has_its_own_exit_code(seat):
    """An idempotent installer must be able to tell 'already up' from 'broken'."""
    from click.testing import CliRunner

    from agent_comms import cli
    from agent_comms.store import Store

    held = Store(seat / ".comms").acquire_daemon_lock()
    try:
        result = CliRunner().invoke(cli.main, ["daemon", "--once"], standalone_mode=False)
        assert isinstance(result.exception, DaemonAlreadyRunning)
        assert cli.EXIT_ALREADY_RUNNING == 4
        assert cli.EXIT_ALREADY_RUNNING not in (cli.EXIT_OK, cli.EXIT_FAULT, cli.EXIT_DISABLED)
    finally:
        held.close()


# -- who a message is for (0.4) ----------------------------------------------

def _event(msg_id, topic, flags=None, mtype="stream"):
    return {"id": msg_id, "type": "message", "flags": flags or [], "message": {
        "id": msg_id, "sender_full_name": "Oliver Blakeman", "display_recipient": "agent-eco",
        "subject": topic, "content": "hello", "timestamp": 1, "stream_id": 7, "type": mtype}}


def test_topic_named_for_the_seat_reaches_it_without_a_mention(seat):
    """ADR-0009 §1: one topic per arch↔component conversation. The topic addresses."""
    transport = FakeTransport(event_batches=[{"result": "success", "events": [
        _event(501, "agent-comms: please finish the client"),
    ]}])
    stored = operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1)
    assert stored == 1
    assert operations.inbox()[0].reason == "topic addressed to this seat"


def test_project_shaped_topic_prefix_also_matches(seat):
    transport = FakeTransport(event_batches=[{"result": "success", "events": [
        _event(502, "agent-eco-agent-comms: a question"),
    ]}])
    assert operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1) == 1


def test_topic_prefix_is_case_insensitive_and_space_tolerant(seat):
    transport = FakeTransport(event_batches=[{"result": "success", "events": [
        _event(503, "Agent-Comms : mixed case"),
    ]}])
    assert operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1) == 1


def test_another_seats_topic_is_not_ours(seat):
    """One channel per project: matching everything would wake every seat."""
    transport = FakeTransport(event_batches=[{"result": "success", "events": [
        _event(504, "dprox: something for someone else"),
        _event(505, "general chatter"),
    ]}])
    assert operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1) == 0


def test_explicit_mention_still_wins_in_any_topic(seat):
    transport = FakeTransport(event_batches=[{"result": "success", "events": [
        _event(506, "dprox: but they tagged us", flags=["mentioned"]),
    ]}])
    assert operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1) == 1
    assert operations.inbox()[0].reason == "mentioned"


def test_direct_message_reaches_the_seat(seat):
    transport = FakeTransport(event_batches=[{"result": "success", "events": [
        _event(507, "", mtype="private"),
    ]}])
    assert operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1) == 1
    assert operations.inbox()[0].reason == "direct message"


def test_a_seat_never_stores_its_own_messages(seat):
    """Observed live: this seat's store held its own smoke test.

    A seat posts to a topic named after itself, so the topic rule would return
    every one of its own messages — it would read its own words back as an ask.
    """
    own = {"id": 601, "type": "message", "flags": ["mentioned"], "message": {
        "id": 601, "sender_full_name": "agent-comms",
        "sender_email": "agent-eco-agent-comms-bot@example.com",
        "display_recipient": "agent-eco", "subject": "agent-comms: my own post",
        "content": "something I said", "timestamp": 1, "stream_id": 7, "type": "stream"}}
    transport = FakeTransport(event_batches=[{"result": "success", "events": [own]}])
    assert operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1) == 0


def test_someone_else_in_our_topic_still_reaches_us(seat):
    other = {"id": 602, "type": "message", "flags": [], "message": {
        "id": 602, "sender_full_name": "Oliver Blakeman", "sender_email": "ojblakeman@gmail.com",
        "display_recipient": "agent-eco", "subject": "agent-comms: a real ask",
        "content": "please look at this", "timestamp": 1, "stream_id": 7, "type": "stream"}}
    transport = FakeTransport(event_batches=[{"result": "success", "events": [other]}])
    assert operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1) == 1


# -- the permission check must not be able to kill the daemon -----------------

def test_a_hub_failure_in_the_permission_check_holds_rather_than_killing(seat):
    """0.40.2 introduced this and it is the class this client exists to refuse.

    The permission check asks the hub, and the loop it runs in sits outside the
    guard around `get_events`. Unguarded, one transport hiccup ends the daemon —
    silently, until someone notices nothing is arriving.
    """
    class Flaky(FakeTransport):
        def call_endpoint(self, url, method="GET", request=None):
            if url == "users":
                raise ConnectionError("hub said no")
            return super().call_endpoint(url, method, request)

    transport = Flaky(event_batches=[{"result": "success", "events": [
        {"id": 1, "type": "message", "flags": ["mentioned"], "message": {
            "id": 701, "sender_full_name": "agent-eco-arch", "display_recipient": "agent-eco",
            "subject": "agent-comms: ping", "content": "hello",
            "timestamp": 1, "stream_id": 7}},
    ]}])
    notified = []
    stored = operations.run_daemon(transport_factory=lambda c: transport,
                                   max_iterations=1, on_mention=notified.append)

    assert stored == 1, "the daemon survived and stored the message"
    assert notified == [], "held: an undetermined permission is not a yes"
    log = (seat / ".comms" / "events.log").read_text()
    assert "could not determine whether" in log
    assert "holding the message rather than refusing it" in log


def test_an_undetermined_permission_does_not_bounce(seat):
    """A non-answer is not a refusal, and telling the sender otherwise would be
    a false accusation the estate would then try to fix in the directory."""
    posted = []

    class Flaky(FakeTransport):
        def call_endpoint(self, url, method="GET", request=None):
            if url == "users":
                raise ConnectionError("hub said no")
            if url == "messages":
                posted.append(request)
            return super().call_endpoint(url, method, request)

    transport = Flaky(event_batches=[{"result": "success", "events": [
        {"id": 1, "type": "message", "flags": ["mentioned"], "message": {
            "id": 702, "sender_full_name": "agent-eco-arch", "display_recipient": "agent-eco",
            "subject": "agent-comms: ping", "content": "hello",
            "timestamp": 1, "stream_id": 7}},
    ]}])
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1)
    assert not any("did not reach the agent" in (p.get("content") or "") for p in posted)


# -- stopping and replacing the daemon (0.51.0) -------------------------------

def test_stop_reports_when_there_was_nothing_to_stop(seat):
    """Idempotent: an operator may run it twice, and the second is not a failure."""
    stopped, pid = operations.stop_daemon()
    assert (stopped, pid) == (False, None)


def test_stop_waits_for_the_lock_not_the_signal(seat, monkeypatch):
    """The lock is what refuses the replacement, so the lock is what we wait on.

    Signalling and returning would make --restart a race that usually works.
    Here the holder releases only after several polls; stop must not return
    until it does.
    """
    from agent_comms.store import Store

    # Store, not the message store: this test is about the daemon LOCK, which
    # lives in its own file and was never part of the message store.
    store = Store(seat / ".comms")
    held = store.acquire_daemon_lock()
    polls = {"n": 0}
    real_state = store.daemon_state

    def releasing_state():
        polls["n"] += 1
        if polls["n"] == 4:
            held.close()  # the "process" finally ends, freeing the flock
        return real_state()

    monkeypatch.setattr(Store, "daemon_state", lambda self: releasing_state())
    monkeypatch.setattr(operations.os, "kill", lambda pid, sig: None)

    stopped, pid = operations.stop_daemon(timeout=5.0)
    assert stopped is True
    assert polls["n"] >= 4, "returned before the lock was actually released"


def test_stop_refuses_rather_than_reporting_a_hopeful_stop(seat, monkeypatch):
    """A daemon that outlives SIGTERM is wedged; saying 'stopped' would be a lie."""
    from agent_comms.errors import DaemonWillNotStop
    from agent_comms.store import Store

    held = Store(seat / ".comms").acquire_daemon_lock()
    try:
        monkeypatch.setattr(operations.os, "kill", lambda pid, sig: None)
        with pytest.raises(DaemonWillNotStop, match="kill -9"):
            operations.stop_daemon(timeout=0.3)
    finally:
        held.close()


def test_restart_says_whether_it_replaced_anything(seat, monkeypatch):
    """'restarted' and 'started, nothing was running' are different facts."""
    monkeypatch.setattr(operations, "detach_daemon", lambda log=None, **kw: 4242)
    replaced, pid = operations.restart_daemon()
    assert (replaced, pid) == (False, 4242)


def test_restart_stops_before_it_starts(seat, monkeypatch):
    """Start-then-stop would kill the replacement; the order is the whole command."""
    calls = []
    monkeypatch.setattr(operations, "stop_daemon",
                        lambda **kw: (calls.append("stop"), (True, 111))[1])
    monkeypatch.setattr(operations, "detach_daemon",
                        lambda log=None, **kw: (calls.append("start"), 222)[1])
    replaced, pid = operations.restart_daemon()
    assert calls == ["stop", "start"]
    assert (replaced, pid) == (True, 222)


def test_daemon_flags_that_ask_for_different_things_are_refused(seat):
    """--stop --restart together has no single meaning; guessing one would be wrong."""
    from click.testing import CliRunner

    from agent_comms import cli

    result = CliRunner().invoke(cli.main, ["daemon", "--stop", "--restart"],
                                standalone_mode=False)
    assert isinstance(result.exception, click.UsageError)
    assert "Pick one" in str(result.exception)


# -- the last mile: a seat that receives and wakes nobody (0.51.0) -------------

def test_doctor_fails_when_no_wake_trigger_is_configured(seat):
    """The defect arch found on 2026-09-13: every check passed on a seat that
    stored every mention and woke nobody. A green doctor over that is §9's first
    failure — a check that declines to run under the condition it exists to catch.
    """
    without_wake_trigger()
    report = operations.preflight(transport_factory=lambda c: FakeTransport())
    wake = [c for c in report.checks if c[0] == "wake trigger"]
    assert wake, "doctor does not check the wake trigger at all"
    name, passed, detail = wake[0]
    assert passed is False
    assert report.ok is False, "doctor passed overall on a seat that wakes nobody"
    assert "notify_command" in detail and "comms wake" in detail, \
        "the failure must name the remedy, not just the symptom"


def test_doctor_passes_once_a_trigger_is_set(seat, monkeypatch):
    """And it must go quiet when satisfied — §9's other half."""
    monkeypatch.setenv("AGENT_COMMS_NOTIFY", "comms wake")
    report = operations.preflight(transport_factory=lambda c: FakeTransport())
    name, passed, detail = [c for c in report.checks if c[0] == "wake trigger"][0]
    assert passed is True
    assert "comms wake" in detail


def test_status_is_not_ready_when_nothing_is_woken(seat):
    """`status` answers 'is comms working?'. Receiving and never waking is not."""
    without_wake_trigger()
    st = operations.status()
    assert st.wake_trigger is None


def test_status_exit_code_distinguishes_woken_from_not(seat, monkeypatch):
    """An installer's verification must be able to catch this mechanically."""
    from click.testing import CliRunner

    from agent_comms import cli
    from agent_comms.store import DaemonState

    # A healthy daemon, so the only thing under test is the wake trigger.
    monkeypatch.setattr(
        operations, "status",
        lambda **kw: operations.Status(
            enabled=True, detail="", tag="ready", identity="i", channel="c",
            credential="x", ready=True,
            daemon=DaemonState(running=True, pid=1,
                               last_tick=__import__("datetime").datetime.now(
                                   tz=__import__("datetime").timezone.utc)),
            wake_trigger=None,
        ),
    )
    result = CliRunner().invoke(cli.main, ["status"])
    assert result.exit_code == cli.EXIT_FAULT
    assert "NOTHING IS WOKEN" in result.output
    assert "comms wake" in result.output


def test_daemon_records_the_missing_trigger_at_startup(seat):
    """The log must say it once, at the top, since it governs every later line."""
    without_wake_trigger()
    from agent_comms.store import Store

    operations.run_daemon(transport_factory=lambda c: FakeTransport(), max_iterations=1)
    events = (seat / ".comms" / "events.log").read_text()
    assert "no wake trigger configured" in events


def test_daemon_still_runs_without_a_trigger(seat):
    """Degraded is not broken: refusing to start would take away the half that
    works, and `comms inbox` is a real fallback."""
    without_wake_trigger()
    stored = operations.run_daemon(
        transport_factory=lambda c: FakeTransport(), max_iterations=1
    )
    assert stored == 0  # ran to completion rather than raising


# -- --supervise: restart what a restart can fix, and only that (0.51.1) -------

def test_supervise_restarts_a_daemon_that_exits(seat, monkeypatch):
    """The whole point: a daemon that dies comes back without a person."""
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        raise RuntimeError("connection reset")

    monkeypatch.setattr(operations, "run_daemon", flaky)
    monkeypatch.setattr(operations.time, "sleep", lambda s: None)
    restarts = operations.supervise_daemon(max_restarts=3, backoff_start=0)
    assert restarts == 3
    assert calls["n"] == 4  # the original run plus three restarts


def test_supervise_refuses_to_loop_on_a_fault_a_restart_cannot_fix(seat, monkeypatch):
    """A crash loop buries the reason and reports activity while nothing is received."""
    from agent_comms.errors import CredentialMissing

    def broken(**kw):
        raise CredentialMissing("no credential for this seat")

    monkeypatch.setattr(operations, "run_daemon", broken)
    with pytest.raises(CredentialMissing):
        operations.supervise_daemon(max_restarts=5, backoff_start=0)


def test_supervise_records_each_restart(seat, monkeypatch):
    """A restart nobody can see is a daemon that looks like it never died."""
    monkeypatch.setattr(operations, "run_daemon",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(operations.time, "sleep", lambda s: None)
    operations.supervise_daemon(max_restarts=1, backoff_start=0)
    events = (seat / ".comms" / "events.log").read_text()
    assert "supervisor restarting" in events


def test_supervise_backs_off_and_resets_after_a_long_run(seat, monkeypatch):
    """Fast repeated failures slow down; a daemon that ran for ages then died
    starts again promptly — those are different events."""
    slept = []
    monkeypatch.setattr(operations.time, "sleep", slept.append)
    monkeypatch.setattr(operations, "run_daemon",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    operations.supervise_daemon(max_restarts=4, backoff_start=1.0, backoff_max=8.0)
    assert slept == [1.0, 2.0, 4.0, 8.0], slept


def test_supervise_conflicts_with_the_other_daemon_flags(seat):
    from click.testing import CliRunner

    from agent_comms import cli

    result = CliRunner().invoke(cli.main, ["daemon", "--supervise", "--detach"],
                                standalone_mode=False)
    assert isinstance(result.exception, click.UsageError)


# -- the queue is a doorbell; history is the record (0.52.2) -------------------

def _history_msg(mid, ts, topic="agent-comms: from history", sender="agent-eco-arch"):
    return {"id": mid, "sender_full_name": sender, "display_recipient": "agent-eco",
            "subject": topic, "content": "body", "timestamp": ts, "stream_id": 7,
            "flags": ["mentioned"]}


class HistoryTransport(FakeTransport):
    """A hub that also answers `GET messages`, like the real one."""

    def __init__(self, history=None, **kw):
        super().__init__(**kw)
        self.history = history or []
        self.history_calls = []

    def get_events(self, **kwargs):
        """Raise a queued exception, so a gap can be simulated as it really is."""
        if self.event_batches and isinstance(self.event_batches[0], Exception):
            raise self.event_batches.pop(0)
        return super().get_events(**kwargs)

    def call_endpoint(self, url, method="GET", request=None):
        if url == "messages" and method == "GET":
            self.history_calls.append(request)
            anchor = int(request["anchor"])
            return {"result": "success",
                    "messages": [m for m in self.history if m["id"] >= anchor]}
        return super().call_endpoint(url, method, request)


def test_last_message_id_is_the_durable_marker(seat):
    """last_event_id dies with the queue; a message id does not."""
    from agent_comms.store import Store

    store = operations.message_store(seat / ".comms")
    assert store.last_message_id() == 0
    transport = HistoryTransport(event_batches=[{"result": "success", "events": [
        {"id": 1, "type": "message", "flags": ["mentioned"],
         "message": _history_msg(500, 1)},
    ]}])
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1)
    assert store.last_message_id() == 500


def test_a_lost_queue_no_longer_loses_messages(seat):
    """The whole point. Queue dies, history still has them, so nothing is lost."""
    from agent_comms.errors import QueueGapError
    from agent_comms.store import Store

    store = operations.message_store(seat / ".comms")
    transport = HistoryTransport(
        history=[_history_msg(601, 10), _history_msg(602, 11)],
        event_batches=[{"result": "success", "events": [
            {"id": 1, "type": "message", "flags": ["mentioned"],
             "message": _history_msg(600, 9)}]}],
    )
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1)
    assert store.last_message_id() == 600

    # Now the queue is collected; the daemon re-registers and backfills.
    transport.event_batches = [QueueGapError("queue gone."),
                               {"result": "success", "events": []}]
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=2)
    ids = {m.id for m in store.all()}
    assert {601, 602} <= ids, "messages sent while the queue was dead were lost"
    assert "backfilled 2 message(s)" in (seat / ".comms" / "events.log").read_text()


def test_the_anchor_message_is_not_handled_twice(seat):
    """`anchor` is inclusive. Re-handling it would re-notify the agent."""
    from agent_comms.store import Store

    store = operations.message_store(seat / ".comms")
    transport = HistoryTransport(
        history=[_history_msg(700, 10)],
        event_batches=[{"result": "success", "events": [
            {"id": 1, "type": "message", "flags": ["mentioned"],
             "message": _history_msg(700, 10)}]}],
    )
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1)
    before = len(store.all())
    operations._catch_up(
        operations.Hub(transport, operations.load_settings(),
                       operations.load_credential(operations.load_settings().identity)),
        store, lambda e: None, "test")
    assert len(store.all()) == before


def test_a_fresh_seat_does_not_replay_all_history(seat):
    """Backfilling from id 0 would notify the agent about every message ever sent."""
    from agent_comms.store import Store

    store = operations.message_store(seat / ".comms")
    transport = HistoryTransport(history=[_history_msg(i, 1) for i in range(800, 900)])
    hub = operations.Hub(transport, operations.load_settings(),
                         operations.load_credential(operations.load_settings().identity))
    assert operations._catch_up(hub, store, lambda e: None, "fresh") == 0
    assert transport.history_calls == [], "a fresh seat must not read history at all"


def test_the_backstop_catches_a_queue_that_stopped_delivering(seat, monkeypatch):
    """A hung connection errors nothing and looks exactly like a quiet channel.
    Only reading the doorstep tells the difference."""
    import time as _time

    from agent_comms.store import Store

    store = operations.message_store(seat / ".comms")
    old = _time.time() - 600  # comfortably past MISSED_AFTER_SECS
    transport = HistoryTransport(
        history=[_history_msg(901, old)],
        event_batches=[{"result": "success", "events": [
            {"id": 1, "type": "message", "flags": ["mentioned"],
             "message": _history_msg(900, old)}]}],
    )
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1)

    # The queue now says nothing, but history has a message it never delivered.
    monkeypatch.setattr(operations, "BACKSTOP_SECS", 0)
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1)

    events = (seat / ".comms" / "events.log").read_text()
    assert 901 in {m.id for m in store.all()}, "the backstop did not recover it"
    assert "is not delivering" in events, "a dead queue must be replaced, not trusted"


def test_the_backstop_does_not_cry_wolf_on_a_timing_race(seat, monkeypatch):
    """A message arriving between the doorbell and the backstop is normal.
    Tearing down a healthy queue for that would make things worse."""
    import time as _time

    from agent_comms.store import Store

    store = operations.message_store(seat / ".comms")
    transport = HistoryTransport(
        history=[_history_msg(1001, _time.time())],  # just now
        event_batches=[{"result": "success", "events": [
            {"id": 1, "type": "message", "flags": ["mentioned"],
             "message": _history_msg(1000, _time.time())}]}],
    )
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1)
    monkeypatch.setattr(operations, "BACKSTOP_SECS", 0)
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1)

    events = (seat / ".comms" / "events.log").read_text()
    assert 1001 in {m.id for m in store.all()}, "it should still be recovered"
    assert "is not delivering" not in events, "a fresh message is a race, not a fault"


def test_a_losing_daemon_does_not_erase_the_winners_pid(seat):
    """Measured on this seat 2026-09-17: a second daemon started by the estate's
    install left a 0-byte lock. `status` then said "pid None" and
    `comms daemon --stop` could not signal what it could not name.

    Cause: the lock was opened with "w", which truncates BEFORE the flock is
    attempted — so the contender that loses destroys the winner's identity on its
    way out. Take the lock first; write only once it is ours."""
    from agent_comms.errors import DaemonAlreadyRunning
    from agent_comms.store import Store

    store = operations.message_store(seat / ".comms")
    held = store.acquire_daemon_lock()
    try:
        recorded = (seat / ".comms" / "daemon.lock").read_text().strip()
        assert recorded, "the holder must record its pid"

        with pytest.raises(DaemonAlreadyRunning):
            store.acquire_daemon_lock()

        after = (seat / ".comms" / "daemon.lock").read_text().strip()
        assert after == recorded, "a refused contender must not erase the holder's pid"
        assert store.daemon_state().pid == int(recorded)
    finally:
        held.close()


# -- the empty lock, and never advising a pattern kill (orchestrator need) -----
#
# Filed 2026-09-21: `comms daemon --stop` refuses when daemon.lock is present
# but empty, and the remedy it printed was `pgrep -f 'comms daemon'`. A seat's
# tmux server carries `comms daemon` in its OWN argv, so the pattern matches the
# server; the orchestrator's pkill fallback took the sessions of thirteen seats.
# These four fail against the pre-change client.

def test_empty_lock_does_not_stop_stop_from_working(seat, monkeypatch):
    """The lock file is a COPY of the pid. The kernel is the owner of the fact.

    Pre-change this raised DaemonWillNotStop with nothing signalled.
    """
    from agent_comms.store import Store

    store = operations.message_store(seat / ".comms")
    held = store.acquire_daemon_lock()
    holder = int((seat / ".comms" / "daemon.lock").read_text())
    (seat / ".comms" / "daemon.lock").write_text("")  # the defect, exactly

    signalled = []

    def release(pid, sig):
        signalled.append(pid)
        held.close()

    monkeypatch.setattr(operations.os, "kill", release)
    stopped, pid = operations.stop_daemon(timeout=5.0)

    assert stopped is True
    assert pid == holder
    assert signalled == [holder], "signalled the wrong process, or none at all"


def test_status_names_the_pid_when_the_lock_file_is_empty(seat):
    """`running (pid None)` is the state that makes a person reach for pkill."""
    from agent_comms.store import Store

    store = operations.message_store(seat / ".comms")
    held = store.acquire_daemon_lock()
    try:
        expected = int((seat / ".comms" / "daemon.lock").read_text())
        (seat / ".comms" / "daemon.lock").write_text("")
        state = store.daemon_state()
        assert state.running is True
        assert state.pid == expected
        assert "lock file is empty" in state.detail
    finally:
        held.close()


def test_lock_holder_is_never_guessed(seat, monkeypatch):
    """No holder findable → None. A guess here is what kills sessions."""
    from pathlib import Path

    from agent_comms.store import Store

    store = operations.message_store(seat / ".comms")
    store.ensure()
    monkeypatch.setattr(Path, "read_text",
                        lambda self, *a, **k: (_ for _ in ()).throw(OSError("no /proc/locks")))
    assert store.lock_holder_pid() is None


def test_the_refusal_never_recommends_a_pattern_kill(seat, monkeypatch):
    """Both sources exhausted: say so, name the file, forbid the pattern."""
    from agent_comms.errors import DaemonWillNotStop
    from agent_comms.store import Store

    held = Store(seat / ".comms").acquire_daemon_lock()
    try:
        monkeypatch.setattr(Store, "lock_holder_pid", lambda self: None)
        (seat / ".comms" / "daemon.lock").write_text("")
        with pytest.raises(DaemonWillNotStop) as caught:
            operations.stop_daemon(timeout=0.3)
    finally:
        held.close()

    said = str(caught.value)
    assert "pgrep" not in said, "still recommending a pattern match"
    assert "NEVER match on the command line" in said
    assert "fuser" in said or "lsof" in said, "must name an exact way to find it"


# -- cross-project: subscription IS the routing mechanism (§5a) ----------------
#
# Measured 2026-09-22: a message posted in a channel this bot does not hold
# produces NO EVENT AT ALL — not a refusal, not a stored record, not a log line.
# Silence at the transport layer, before any permission check runs. These gates
# are the only place in the estate that fact is visible.

def test_a_send_refuses_a_channel_whose_replies_we_could_not_read(seat, monkeypatch):
    """Posting there would start a topic we cannot follow."""
    from agent_comms.errors import ChannelNotReachable
    from agent_comms.hub import Hub

    monkeypatch.setattr(Hub, "subscribed_channels", lambda self: frozenset({"agent-eco"}))
    monkeypatch.setattr(operations, "_resolve_recipient", lambda s, h, n: n)

    with pytest.raises(ChannelNotReachable) as caught:
        operations.send("hello", to="orch-arch", subject="x", channel="orchestrator",
                        transport_factory=lambda c: FakeTransport())

    said = str(caught.value)
    assert "not subscribed to 'orchestrator'" in said
    assert "agent-eco" in said, "must say what it CAN reach, not only what it cannot"
    assert "§5a" in said or "RECIPIENT's channel" in said


def test_a_send_to_a_held_channel_is_not_refused(seat, monkeypatch):
    """The gate must not fire on the ordinary case, or it stops being read."""
    from agent_comms.hub import Hub

    monkeypatch.setattr(Hub, "subscribed_channels",
                        lambda self: frozenset({"agent-eco", "orchestrator"}))
    monkeypatch.setattr(operations, "_resolve_recipient", lambda s, h, n: n)
    posted = operations.send("hello", to="orch-arch", subject="x", channel="orchestrator",
                             transport_factory=lambda c: FakeTransport())
    assert posted.response


def test_doctor_fails_when_a_routed_channel_is_unreachable(seat, monkeypatch):
    """A transports record naming a channel we do not hold is a message that
    will post and never be answered. Doctor is where that is found out."""
    import json

    from agent_comms.hub import Hub

    (seat / ".comms").mkdir(parents=True, exist_ok=True)
    (seat / ".comms" / "routes.json").write_text(json.dumps({"routes": [
        {"id": "bakehouse.orchestrator.arch",
         "transports": {"comms": {"channel": "orchestrator", "bot": "orch-arch"}}}]}))

    monkeypatch.setattr(Hub, "subscribed_channels", lambda self: frozenset({"agent-eco"}))
    monkeypatch.setattr("agent_comms.operations.seat_state_now",
                        lambda: __import__("agent_comms.seat", fromlist=["x"]).SeatState(
                            answer="yes", reason="r", runtime="claude", sessions=1,
                            version="1.0.2", contract="1.0"))
    report = operations.preflight(transport_factory=lambda c: FakeTransport())

    check = next(c for c in report.checks if c[0] == "reachable channels")
    assert check[1] is False
    assert "orchestrator" in check[2]


def test_doctor_passes_when_every_routed_channel_is_held(seat, monkeypatch):
    import json

    from agent_comms.hub import Hub

    (seat / ".comms").mkdir(parents=True, exist_ok=True)
    (seat / ".comms" / "routes.json").write_text(json.dumps({"routes": [
        {"transports": {"comms": {"channel": "agent-eco"}}}]}))

    monkeypatch.setattr(Hub, "subscribed_channels", lambda self: frozenset({"agent-eco"}))
    monkeypatch.setattr("agent_comms.operations.seat_state_now",
                        lambda: __import__("agent_comms.seat", fromlist=["x"]).SeatState(
                            answer="yes", reason="r", runtime="claude", sessions=1,
                            version="1.0.2", contract="1.0"))
    report = operations.preflight(transport_factory=lambda c: FakeTransport())

    assert next(c for c in report.checks if c[0] == "reachable channels")[1] is True


def test_doctor_names_grant_without_subscription_as_drift(seat, monkeypatch):
    """§5a provisions grant and subscription together, so a routed channel we
    do not hold is DRIFT — not a state anyone chose — and the remedy is orch's
    idempotent provisioning replay, not a hand-edit."""
    import json

    from agent_comms.hub import Hub

    (seat / ".comms").mkdir(parents=True, exist_ok=True)
    (seat / ".comms" / "routes.json").write_text(json.dumps({"routes": [
        {"transports": {"comms": {"channel": "orchestrator"}}}]}))
    monkeypatch.setattr(Hub, "subscribed_channels", lambda self: frozenset({"agent-eco"}))
    monkeypatch.setattr("agent_comms.operations.seat_state_now",
                        lambda: __import__("agent_comms.seat", fromlist=["x"]).SeatState(
                            answer="yes", reason="r", runtime="claude", sessions=1,
                            version="1.0.2", contract="1.0"))
    report = operations.preflight(transport_factory=lambda c: FakeTransport())

    check = next(c for c in report.checks if c[0] == "reachable channels")
    assert check[1] is False
    assert "GRANT WITHOUT SUBSCRIPTION" in check[2]
    assert "replay provisioning" in check[2]


def test_subscription_without_grant_is_a_note_not_a_warning(seat, monkeypatch):
    """The other direction loses nothing — ungranted mail is refused by the
    permission graph, which is the graph working.

    A warning here would fire on every seat holding a test channel, and one
    that fires every time is learned into invisibility (§9) — it would take the
    grant-without-subscription failure down with it.
    """
    import json

    from agent_comms.hub import Hub

    (seat / ".comms").mkdir(parents=True, exist_ok=True)
    (seat / ".comms" / "routes.json").write_text(json.dumps({"routes": [
        {"transports": {"comms": {"channel": "agent-eco"}}}]}))
    monkeypatch.setattr(Hub, "subscribed_channels",
                        lambda self: frozenset({"agent-eco", "seat-testing"}))
    monkeypatch.setattr("agent_comms.operations.seat_state_now",
                        lambda: __import__("agent_comms.seat", fromlist=["x"]).SeatState(
                            answer="yes", reason="r", runtime="claude", sessions=1,
                            version="1.0.2", contract="1.0"))
    report = operations.preflight(transport_factory=lambda c: FakeTransport())

    assert next(c for c in report.checks if c[0] == "reachable channels")[1] is True
    assert any("seat-testing" in n for n in report.notes)


@pytest.mark.parametrize("contract,warns", [
    ("1.0", False), ("1.1-draft", False), ("2.0", False), ("2.0-draft", False),
    ("0.5.1", True), ("3.0", True), ("10.0", True),
])
def test_doctor_uses_the_one_contract_predicate(seat, monkeypatch, contract, warns):
    """THE SAME TRAP, IN A SECOND HOME. `doctor` carried its own
    `contract.startswith("1.")` — the major-10 trap pinned against in the
    delivery gate — and it survived the {1,2} widening, firing on every healthy
    2.0 seat and calling it *older* than the client. Found by agent-skeleton on
    their install, in shipped 2.0.

    A predicate with two implementations has two behaviours. There is one now,
    and this test is what stops a third appearing.
    """
    from agent_comms.hub import Hub
    from agent_comms.seat import SeatState

    monkeypatch.setattr(Hub, "subscribed_channels", lambda self: frozenset({"agent-eco"}))
    monkeypatch.setattr("agent_comms.operations.seat_state_now",
                        lambda: SeatState(answer="yes", reason="r", runtime="claude",
                                          sessions=1, version="2.0.0", contract=contract))
    report = operations.preflight(transport_factory=lambda c: FakeTransport())

    said = " ".join(report.warnings)
    assert ("does not speak" in said) is warns, f"contract {contract}: warnings={report.warnings}"
    assert "cannot deliver to an older seat" not in said, "the backwards wording is back"


# -- R13: the periodic config sync -------------------------------------------

def test_a_refresh_replaces_the_set_and_never_merges(seat, monkeypatch):
    """The directory is authoritative (master ruling, 2026-09-23). Merging would
    make this seat the second authority and keep a withdrawn agent alive locally
    forever."""
    import json

    from agent_comms import config_sync

    d = seat / ".comms"
    d.mkdir(parents=True, exist_ok=True)
    (d / "routes.json").write_text(json.dumps({"generation": 1, "routes": [
        {"agent": "bakehouse.agent-eco.gone", "delivery": "inject"}]}))

    monkeypatch.setattr(config_sync, "directory_address", lambda: "http://d.invalid")
    monkeypatch.setattr(config_sync, "_credential", lambda: "t")
    monkeypatch.setattr(config_sync.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(json.dumps({
                            "contract": "0.2", "generation": 2, "assignments": [
                                {"agent": "bakehouse.agent-eco.kept", "delivery": "inject"}]})))
    got = config_sync.fetch("agent-eco", "s", d)

    assert got.generation == 2 and got.agents == 1
    assert list(config_sync.agent_set(d)) == ["bakehouse.agent-eco.kept"], "withdrawn agent survived"


def test_an_unreachable_directory_runs_from_the_file(seat, monkeypatch):
    """The file is the boot source. An outage must not stop a seat working from
    what it already has."""
    import json

    from agent_comms import config_sync

    d = seat / ".comms"
    d.mkdir(parents=True, exist_ok=True)
    (d / "routes.json").write_text(json.dumps({"generation": 5, "fetched_at": "t", "routes": [
        {"agent": "bakehouse.agent-eco.held"}]}))
    monkeypatch.setattr(config_sync, "directory_address", lambda: "http://d.invalid")
    monkeypatch.setattr(config_sync.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError()))

    got = config_sync.fetch("agent-eco", "s", d)
    assert got.source == "file" and got.generation == 5
    assert "did not answer" in got.reason
    assert list(config_sync.agent_set(d)) == ["bakehouse.agent-eco.held"], "the held set was lost"


def test_an_unreadable_answer_never_replaces_a_readable_set(seat, monkeypatch):
    """Fail closed. A set we cannot read must not overwrite one we can."""
    import json

    from agent_comms import config_sync

    d = seat / ".comms"
    d.mkdir(parents=True, exist_ok=True)
    (d / "routes.json").write_text(json.dumps({"generation": 5, "routes": [{"agent": "keep.me"}]}))
    monkeypatch.setattr(config_sync, "directory_address", lambda: "http://d.invalid")
    monkeypatch.setattr(config_sync.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(json.dumps({"totally": "unexpected"})))

    got = config_sync.fetch("agent-eco", "s", d)
    assert got.source == "file" and "not an assignment set" in got.reason
    assert list(config_sync.agent_set(d)) == ["keep.me"]


def test_the_seat_door_is_project_qualified(seat):
    """Measured: the bare seat name answers 403. /v0/routes is the OPERATOR view."""
    from agent_comms.config_sync import seat_path

    assert seat_path("agent-eco", "test-claude") == "/v0/seats/agent-eco/test-claude/assignments"


class _Resp:
    def __init__(self, text):
        self._t = text.encode()

    def read(self):
        return self._t

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_doctor_names_a_daemon_running_a_different_build(seat, monkeypatch):
    """The one piece of stale state that CANNOT be removed, so it is reported.

    A seat mid-upgrade genuinely has two versions on it: the daemon is a
    process and the CLI is whatever is on disk now. This seat lost two messages
    to exactly that gap while every check read green (catalogue 0.56).
    """
    from agent_comms.hub import Hub
    from agent_comms.seat import SeatState
    from agent_comms.store import Store

    s = Store(seat / ".comms")
    s.ensure()
    s.record_build("1.9.9")
    monkeypatch.setattr(Store, "daemon_state",
                        lambda self: __import__("agent_comms.store", fromlist=["x"]).DaemonState(
                            running=True, pid=1, last_tick=None))
    monkeypatch.setattr(Hub, "subscribed_channels", lambda self: frozenset({"agent-eco"}))
    monkeypatch.setattr("agent_comms.operations.seat_state_now",
                        lambda: SeatState(answer="yes", reason="r", runtime="claude",
                                          sessions=1, version="2.0.0", contract="2.0"))
    report = operations.preflight(transport_factory=lambda c: FakeTransport())

    check = next(c for c in report.checks if c[0] == "daemon build")
    assert check[1] is False
    assert "1.9.9" in check[2] and "comms daemon --restart" in check[2]


def test_doctor_is_quiet_when_the_builds_agree(seat, monkeypatch):
    """It must not fire on the ordinary case, or it stops being read (§9)."""
    from agent_comms import __version__
    from agent_comms.hub import Hub
    from agent_comms.seat import SeatState
    from agent_comms.store import Store

    s = Store(seat / ".comms")
    s.ensure()
    s.record_build(__version__)
    monkeypatch.setattr(Store, "daemon_state",
                        lambda self: __import__("agent_comms.store", fromlist=["x"]).DaemonState(
                            running=True, pid=1, last_tick=None))
    monkeypatch.setattr(Hub, "subscribed_channels", lambda self: frozenset({"agent-eco"}))
    monkeypatch.setattr("agent_comms.operations.seat_state_now",
                        lambda: SeatState(answer="yes", reason="r", runtime="claude",
                                          sessions=1, version="2.0.0", contract="2.0"))
    report = operations.preflight(transport_factory=lambda c: FakeTransport())

    assert next(c for c in report.checks if c[0] == "daemon build")[1] is True


# -- R17: the rest of the §6 surface -----------------------------------------

def test_resolve_prints_the_path_and_sends_nothing(seat, monkeypatch):
    """The command that ends arguments: where would this go, and why.

    Every field failure this client has had looked like "the message went
    nowhere". This is how a person finds out where it WOULD have gone before
    they send it — and it must send nothing while answering.
    """
    import agent_comms.resolve as R

    sent = []
    monkeypatch.setattr(R, "_post", lambda *a, **k: (200, {
        "kind": "resolution-result", "contract": "0.2", "success": True,
        "status": "resolved", "canonical_id": "bakehouse.agent-eco.arch",
        "delivery": "inject", "route_revision": 3}))
    monkeypatch.setattr(R, "directory_address", lambda: "http://d")
    monkeypatch.setattr(operations, "send", lambda *a, **k: sent.append(a))

    out = "\n".join(operations.resolve_name("arch"))
    assert "bakehouse.agent-eco.arch" in out
    # `resolve` tells the truth about what WOULD happen. Since 2026-09-25 that
    # is a refusal for an agent with no declared transport — so it must say so
    # here, before a person sends, rather than printing a transport it would
    # not actually use.
    assert "would send  NO" in out
    assert "no comms transport is declared" in out
    assert sent == [], "resolve sent something"


def test_resolve_says_why_when_it_would_not_send(seat, monkeypatch):
    import agent_comms.resolve as R

    monkeypatch.setattr(R, "directory_address", lambda: "")
    out = "\n".join(operations.resolve_name("nobody"))
    assert "would send  NO" in out and "unknown" in out


def test_queue_shows_what_the_pass_would_send_not_everything(seat):
    """A queue view that shows more than the pass would send teaches the wrong
    expectation — the bounds apply here exactly as they do in the pass."""
    from agent_comms.store import Mention

    store = operations.message_store(seat / ".comms")
    for i in range(7):
        store.append(Mention(id=200 + i, sender="agent-eco-arch", channel="agent-eco",
                             topic="t", content="x", timestamp=NOW, permalink=""))
    rows = operations.queued_now()
    assert len(rows) == 3, f"showed {len(rows)}, the pass sends 3"
    assert rows[0]["of"] == 7, "did not say how many are waiting behind it"


def test_retire_by_hand_is_logged_with_its_reason(seat):
    """A person removing a message is legitimate; doing it by editing a file is
    how a store stops being evidence. `retired` with no cause is
    indistinguishable from a bug six months later."""
    from agent_comms.store import Mention

    store = operations.message_store(seat / ".comms")
    store.append(Mention(id=301, sender="agent-eco-arch", channel="agent-eco", topic="t",
                         content="x", timestamp=NOW, permalink=""))
    said = operations.retire(301, "superseded by a later instruction")

    assert "retired 301" in said and "superseded" in said
    assert [r["id"] for r in operations.recent(last=9, state="retired")] == [301]
    assert any("superseded" in r["cause"] for r in store.history(store._find(301)["id"]))


def test_requeue_is_the_one_backwards_move_and_needs_a_person(seat):
    """Everything else is forward-only. An operator resurrecting a message is a
    DECISION, not a transition, and the record says which."""
    from agent_comms.store import Mention

    store = operations.message_store(seat / ".comms")
    store.append(Mention(id=302, sender="agent-eco-arch", channel="agent-eco", topic="t",
                         content="x", timestamp=NOW, permalink=""))
    operations.retire(302, "wrongly retired")
    said = operations.requeue(302, "it was wanted after all")

    assert "requeued 302" in said and "age bound still applies" in said
    assert [r["id"] for r in operations.recent(last=9, state="queued")] == [302]
    assert any("requeued by hand" in r["cause"] for r in store.history(store._find(302)["id"]))


def test_retire_and_requeue_refuse_an_unknown_id(seat):
    assert "no message 999" in operations.retire(999, "x")
    assert "no message 999" in operations.requeue(999, "x")


def test_a_derived_bot_the_hub_does_not_have_is_refused_not_posted(seat, monkeypatch):
    """MEASURED 2026-09-25 on the test containers, and it was a silent
    non-delivery of my own making.

    R15 derives the hub identity from the FQN's agent segment, which assumes one
    hub account PER AGENT. The deployed hub has one per SEAT. So
    `bakehouse.agent-eco.test-claude-new001` derived `test-claude-new001`, the
    post mentioned an account that does not exist, `sent` was reported, and the
    message reached nobody.

    It refuses instead — and does NOT fall back to the seat's bot, because
    delivering to the seat's default agent when the caller named a different one
    is the same wrong-recipient failure wearing a helpful face.
    """
    from agent_comms.hub import Hub
    from agent_comms.operations import UnknownRecipient

    monkeypatch.setattr(Hub, "subscribed_channels", lambda self: frozenset({"agent-eco"}))
    monkeypatch.setattr(Hub, "addressable_names", lambda self: ["test-claude", "agent-eco-arch"])
    monkeypatch.setattr(operations, "_route", lambda s, n, **k: operations.Routed(
        fqn="bakehouse.agent-eco.test-claude-new001", channel="agent-eco",
        bot="test-claude-new001", delivery="inject"))

    posted = []
    monkeypatch.setattr(Hub, "send", lambda self, c, t, b: posted.append((c, t)))

    with pytest.raises(UnknownRecipient) as caught:
        operations.send("x", to="bakehouse.agent-eco.test-claude-new001", subject="s",
                        transport_factory=lambda c: FakeTransport())

    said = str(caught.value)
    assert "test-claude-new001" in said and "no such account" in said
    assert "Nothing was posted" in said
    assert posted == [], "it posted anyway"


def test_a_derived_bot_the_hub_does_have_is_posted(seat, monkeypatch):
    """The gate must not fire on the working case, or it stops being read."""
    from agent_comms.hub import Hub

    monkeypatch.setattr(Hub, "subscribed_channels", lambda self: frozenset({"agent-eco"}))
    monkeypatch.setattr(Hub, "addressable_names", lambda self: ["test-claude"])
    monkeypatch.setattr(operations, "_route", lambda s, n, **k: operations.Routed(
        fqn="bakehouse.agent-eco.test-claude", channel="agent-eco",
        bot="test-claude", delivery="inject"))
    posted = []
    monkeypatch.setattr(Hub, "send", lambda self, c, t, b: posted.append((c, t)) or {"id": 1})

    operations.send("x", to="bakehouse.agent-eco.test-claude", subject="s",
                    transport_factory=lambda c: FakeTransport())
    assert posted and posted[0][0] == "agent-eco"


# -- doctor's mirror check: agents assigned here must reach THIS seat --------

def test_an_agent_declaring_another_seats_bot_is_named_as_wrong():
    """The silent case, made loud at the seat. A sender obeying a record naming
    someone else's bot posts where no bot of ours is subscribed, and a post no
    bot holds produces NO EVENT AT ALL — success reported, nothing delivered.
    The seat can see this; the sender cannot."""
    from agent_comms.operations import agents_reaching
    wrong, undeclared = agents_reaching({
        "bakehouse.agent-eco.test-claude-new001":
            {"transports": {"comms": {"bot": "test-claude", "channel": "seat-testing"}}},
        "bakehouse.agent-eco.stray":
            {"transports": {"comms": {"bot": "some-other-seat", "channel": "seat-testing"}}},
    }, ("test-claude", "agent-eco-test-claude"))
    assert wrong == ["bakehouse.agent-eco.stray \u2192 bot 'some-other-seat'"]
    assert undeclared == []


def test_both_canonical_spellings_of_this_seats_bot_are_accepted():
    """ADR-0009 §7a: a component bot is unambiguous as <seat> in its own channel
    and as <project>-<seat> anywhere. BOTH are correct, and the directory
    authors the short one. Accepting only `identity.bot_name` failed every
    correctly-declared agent on every component seat -- measured on test-claude
    2026-09-25, calling a provably working delivery 'delivering to nobody'."""
    from agent_comms.operations import agents_reaching
    assert agents_reaching(
        {"a.b.one": {"transports": {"comms": {"bot": "test-claude"}}},
         "a.b.two": {"transports": {"comms": {"bot": "agent-eco-test-claude"}}}},
        ("test-claude", "agent-eco-test-claude")) == ([], [])


def test_an_undeclared_agent_is_a_note_not_a_failure():
    """Derivation is gone, so an undeclared agent is refused at the sender with
    nothing posted — a missing record, not a silent loss. Failing on it would
    fire on every seat mid-authoring, and a check that always fires is
    constitution §9's speech when it should be silent."""
    from agent_comms.operations import agents_reaching
    wrong, undeclared = agents_reaching(
        {"bakehouse.agent-eco.pending": {"transports": {}}},
        ("test-claude", "agent-eco-test-claude"))
    assert wrong == []
    assert undeclared == ["bakehouse.agent-eco.pending"]


def test_a_seat_whose_agents_all_point_at_it_reports_nothing_wrong():
    from agent_comms.operations import agents_reaching
    assert agents_reaching({
        "a.b.one": {"transports": {"comms": {"bot": "test-claude"}}},
        "a.b.two": {"transports": {"comms": {"bot": "test-claude"}}},
    }, ("test-claude", "agent-eco-test-claude")) == ([], [])


def test_the_wake_outcome_is_a_word_not_a_prefix_of_a_sentence():
    """Write-time gate 1, fixed 2026-09-25. `wake_agent` returned prose and the
    CLI chose an EXIT CODE with `outcome.startswith("queued")` -- a
    prefix-match on a closed word set, picking a consumer surface by guesswork.
    `queued-for-review` would have matched `queued`.

    The near-miss is the point: a word that STARTS WITH the right word is the
    wrong word."""
    from agent_comms.operations import Woken
    w = Woken(Woken.QUEUED, "queued: the seat could not be invoked")
    assert w.outcome == Woken.QUEUED
    # The human line is unchanged for anything that echoes it.
    assert str(w) == "queued: the seat could not be invoked"

    near = Woken("queued-for-review", "queued-for-review: not a real state")
    assert near.outcome != Woken.QUEUED, \
        "an exact match must reject a word that merely starts with the right one"
    assert near.line.startswith("queued"), \
        "...and the near-miss really would have passed the old prefix test"
