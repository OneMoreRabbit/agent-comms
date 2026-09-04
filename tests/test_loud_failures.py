"""The five conditions contract §3 forbids from passing quietly.

Each test asserts the *behaviour the contract promises*, not the implementation:
a consumer pinning 0.1 is entitled to exactly these.
"""

from __future__ import annotations

import pytest

from agent_comms import operations
from agent_comms.config import Settings, load_credential, load_settings
from agent_comms.errors import (
    CommsDisabled,
    CredentialMissing,
    CredentialUnreadable,
    InsecureTransportRefused,
    NotSubscribed,
    QueueGapError,
)
from agent_comms.hub import Hub, Registration
from tests.conftest import FakeTransport


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


def test_old_server_reports_lifespan_unverifiable(seat):
    """Zulip 10.4 (level 372) cannot echo it. Say so; never imply we checked."""
    settings = load_settings()
    credential = load_credential(settings.identity)
    reg = Hub(FakeTransport(), settings, credential).register_queue()
    assert len(reg.warnings) == 1
    assert "lifespan unverified" in reg.warnings[0]
    assert "feature level 372" in reg.warnings[0]


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


def test_connect_warnings_reach_the_durable_log(seat):
    """A warning that only ever hit a daemon's stderr is the silence §3 forbids."""
    operations.run_daemon(transport_factory=lambda c: FakeTransport(), max_iterations=1)
    log = (seat / ".comms" / "events.log").read_text(encoding="utf-8")
    assert "lifespan unverified" in log


# -- doctor must not report the resting state as a failure -------------------

def test_doctor_treats_disabled_as_a_state_not_a_failure(seat):
    """Our own output must not repeat the conflation §3 forbids."""
    (seat / ".comms" / "config.toml").unlink()
    report = operations.preflight()
    assert report.disabled is True
    assert all(passed for _, passed, _ in report.checks)


def test_doctor_reports_every_check_not_just_the_first(seat):
    """An operator debugging a seat wants the whole picture."""
    report = operations.preflight(transport_factory=lambda c: FakeTransport())
    names = [n for n, _, _ in report.checks]
    assert names == ["enabled", "credential", "identity", "subscription", "event queue"]
    assert report.ok
    assert any("lifespan unverified" in w for w in report.warnings)


# -- attribution: the bot must be who the vault thinks it is -----------------

def test_bot_name_divergence_is_reported_not_fatal(seat):
    """A name is not a safety property; blocking the critical path over one is wrong.

    But ADR-0009 §1a picked <project>-<seat> so a sender identifies its project,
    and the estate minted 'agent-comms'. Report it.
    """
    report = operations.preflight(
        transport_factory=lambda c: FakeTransport(full_name="agent-comms")
    )
    assert report.ok
    assert any("not 'agent-eco-agent-comms'" in w for w in report.warnings)


def test_human_account_credential_is_reported(seat):
    report = operations.preflight(
        transport_factory=lambda c: FakeTransport(is_bot=False)
    )
    assert any("human account" in w for w in report.warnings)


def test_fallback_credential_path_is_accepted_and_reported(seat):
    """The estate delivered zuliprc-<seat>; work, but say the paths diverged."""
    contracted = seat / ".secrets" / "zuliprc-agent-eco-agent-comms"
    fallback = seat / ".secrets" / "zuliprc-agent-comms"
    contracted.rename(fallback)
    fallback.chmod(0o600)
    cred = load_credential(load_settings().identity)
    assert cred.source == str(fallback)
    assert any("not the contracted" in n for n in cred.notices)


def test_contracted_path_wins_when_both_exist(seat):
    fallback = seat / ".secrets" / "zuliprc-agent-comms"
    fallback.write_text(
        "[api]\nemail=x@y.z\nkey=k\nsite=https://agent.onemorerabbit.co.uk\n", encoding="utf-8"
    )
    fallback.chmod(0o600)
    cred = load_credential(load_settings().identity)
    assert cred.source.endswith("zuliprc-agent-eco-agent-comms")
    assert cred.notices == []


# -- the daemon ---------------------------------------------------------------

def test_daemon_resumes_a_stored_queue_rather_than_re_registering(seat):
    """Re-registering when a queue was held silently forfeits the gap."""
    from agent_comms.store import Store
    store = Store(seat / ".comms")
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
            "id": 401, "sender_full_name": "arch", "display_recipient": "agent-eco",
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
            "id": 101, "sender_full_name": "arch", "display_recipient": "agent-eco",
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
            "id": 201, "sender_full_name": "arch", "display_recipient": "agent-eco",
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
            "id": 202, "sender_full_name": "arch", "display_recipient": "agent-eco",
            "subject": "t", "content": "c", "timestamp": 1, "stream_id": 7}},
    ]}])
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1)
    assert "notify_command exited 7" in (seat / ".comms" / "events.log").read_text()


def test_reply_goes_to_the_mentions_own_topic(seat):
    transport = FakeTransport(event_batches=[{"result": "success", "events": [
        {"id": 1, "type": "message", "flags": ["mentioned"], "message": {
            "id": 301, "sender_full_name": "arch", "display_recipient": "agent-eco",
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

    store = Store(seat / ".comms")
    store.acquire_daemon_lock().close()
    operations.run_daemon(transport_factory=lambda c: FakeTransport(), max_iterations=1)
