"""Stand-in Google intake: no live Google service or test seat is touched."""

from datetime import datetime, timezone
import json
from threading import Event

import pytest
from click.testing import CliRunner

from agent_comms import cli, google_chat, google_intake, google_reply, google_worker, seat as seat_api
from agent_comms.google_store import GoogleStore

AGENT = "bakehouse.agent-eco.agent-comms"
NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)


def _event(**changes):
    data = dict(agent=AGENT, message_name="spaces/dm/messages/one",
                space_name="spaces/dm", event_time="2026-11-16T11:59:00Z",
                text="Hello", event_type="MESSAGE", space_type="DIRECT_MESSAGE",
                sender_type="HUMAN", sender_account_id="users/123",
                sender_address="person@example.org", app_agent=AGENT)
    data.update(changes)
    return google_intake.IntakeEvent(**data)


def _private(people=None, delivery="inject", **extra):
    return {"agent": AGENT, "google_chat": {"people": people if people is not None else [
        {"address": "person@example.org", "account_id": "users/123"}]},
        "delivery": delivery, **extra}


def _store(tmp_path):
    return GoogleStore(tmp_path / "comms.db")


def test_unconfigured_read_commands_do_not_create_google_tables(seat):
    for args in (["inbox", "--from", AGENT], ["show", "--from", AGENT, "gc-1"],
                 ["log"], ["daemon", "--once"]):
        result = CliRunner().invoke(cli.main, ["google-chat", *args])
        assert result.exit_code == 0, result.output
        assert "not configured" in result.output
    assert not (google_chat.settings_dir().parent.parent / ".comms" / "comms.db").exists()


def test_bounded_collector_uses_standin_and_acks_after_store(seat, monkeypatch):
    root = google_chat.settings_dir()
    root.mkdir(mode=0o700)
    credential = root / f"{AGENT}.json"
    credential.write_text("{}")
    credential.chmod(0o600)
    config_file = root / "settings.json"
    config_file.write_text(json.dumps({AGENT: {
        "subscription": "projects/fake/subscriptions/fake",
        "credential_file": str(credential), "reply_drive_folder_id": "fake"}}))
    config_file.chmod(0o600)
    state_dir = root.parent.parent / ".comms"
    monkeypatch.setattr(google_chat, "private_assignments", lambda *a: {AGENT: _private()})
    raw = {"type": "MESSAGE", "eventTime": "2026-11-16T11:59:00Z",
           "message": {"name": "spaces/dm/messages/one", "text": "Hello"},
           "space": {"name": "spaces/dm", "spaceType": "DIRECT_MESSAGE",
                     "singleUserBotDm": True},
           "user": {"name": "users/123", "email": "person@example.org", "type": "HUMAN"}}
    acks = []
    delivered = []

    class Message:
        data = json.dumps(raw).encode()

    class Received:
        message = Message()
        ack_id = "delivery-1"

    class Response:
        received_messages = [Received()]

    class Subscriber:
        def pull(self, *, request, timeout):
            assert request["subscription"] == "projects/fake/subscriptions/fake"
            return Response()
        def acknowledge(self, *, request):
            assert GoogleStore(state_dir / "comms.db").get("gc-1").state == "queued"
            acks.extend(request["ack_ids"])
        def close(self):
            pass

    def handoff(store, row):
        delivered.append((row.id, row.agent))

    monkeypatch.setattr(google_intake, "deliver_queued", handoff)
    count = google_worker.run_once(state_dir, "agent-eco", "agent-comms",
                                   subscriber_factory=lambda config: Subscriber())
    assert count == 1 and acks == ["delivery-1"] and delivered == [("gc-1", AGENT)]


def test_private_account_id_form_and_precedence(tmp_path):
    people = [{"address": "person@example.org", "account_id": "bad/123"}]
    with pytest.raises(google_chat.GoogleChatConfigError, match="account_id"):
        google_chat.people_for_agent({AGENT: _private(people)}, AGENT)
    with pytest.raises(google_chat.GoogleChatConfigError, match="duplicate"):
        google_chat.people_for_agent({AGENT: _private([
            {"address": "person@example.org", "account_id": "users/123"},
            {"address": "person@example.org"}])}, AGENT)
    store = _store(tmp_path)
    row, _ = google_intake.ingest(store, _event(sender_account_id="users/other"),
                                  _private(), now=NOW)
    assert row.state == "refused" and "users/other" in row.reason
    row, _ = google_intake.ingest(store, _event(message_name="spaces/dm/messages/two",
                                                 sender_address=""), _private(), now=NOW)
    assert row.state == "queued"  # exact account ID wins without an address
    row, _ = google_intake.ingest(store, _event(message_name="spaces/dm/messages/three",
                                                 sender_account_id=""),
                                  _private([{"address": "person@example.org"}]), now=NOW)
    assert row.state == "queued"  # address-only directory entry


@pytest.mark.parametrize("change,reason", [
    ({"space_type": "SPACE"}, "not a direct message"),
    ({"sender_type": "BOT"}, "not a person"),
    ({"app_agent": "other.app.agent"}, "another Chat app"),
    ({"event_type": "ADDED_TO_SPACE"}, "unsupported event"),
    ({"sender_address": "intruder@example.net", "sender_account_id": "users/other"}, "permitted domain"),
    ({"sender_address": "intruder@example.net", "sender_account_id": "users/123"}, "permitted domain"),
    ({"has_attachments": True}, "no partial text handoff"),
    ({"text": ""}, "empty message"),
])
def test_wrong_source_is_stored_but_never_queued(tmp_path, change, reason):
    row, inserted = google_intake.ingest(_store(tmp_path), _event(**change), _private(), now=NOW)
    assert inserted and row.state == "refused" and reason in row.reason


def test_duplicate_source_name_is_inert_even_with_new_pubsub_delivery(tmp_path):
    store = _store(tmp_path)
    first, inserted = google_intake.ingest(store, _event(), _private(), now=NOW)
    second, inserted_again = google_intake.ingest(
        store, _event(text="different", sender_account_id="users/other"), _private(), now=NOW)
    assert inserted and not inserted_again and first.id == second.id
    assert second.state == "queued" and second.payload["text"] == "Hello"


def test_redelivery_completes_a_crashed_received_state(tmp_path):
    store = _store(tmp_path)
    stored, _ = store.receive(AGENT, "spaces/dm/messages/one", "spaces/dm",
                              "2026-11-16T11:59:00Z", {"text": "Hello"})
    assert stored.state == "received"
    recovered, inserted = google_intake.ingest(store, _event(), _private(), now=NOW)
    assert not inserted and recovered.id == stored.id and recovered.state == "queued"


def test_acknowledgement_follows_durable_commit(tmp_path):
    store = _store(tmp_path)
    checked = []

    def ack():
        assert store.get("gc-1").state == "queued"
        checked.append(True)

    google_intake.collect_one(store, _event(), _private(), ack, now=NOW)
    assert checked == [True]
    with pytest.raises(google_chat.GoogleChatConfigError):
        google_intake.collect_one(store, _event(message_name="spaces/dm/messages/two"),
                                       {"agent": AGENT}, ack, now=NOW)
    assert checked == [True]


def test_scalar_modes_ignore_hub_overrides(tmp_path):
    store = _store(tmp_path)
    for number, mode, expected in [("one", "inject", "queued"),
                                   ("two", "hold", "held"), ("three", "none", "refused")]:
        row, _ = google_intake.ingest(store, _event(message_name=f"spaces/dm/messages/{number}"),
                                      _private(delivery=mode,
                                               delivery_overrides={"users/123": "inject"}), now=NOW)
        assert row.state == expected


def test_delivery_outcomes_and_attempt_bound(tmp_path):
    store = _store(tmp_path)
    row, _ = google_intake.ingest(store, _event(), _private(), now=NOW)
    calls = []

    def handoff(body, *, agent):
        calls.append((body, agent))
        if len(calls) == 1:
            return seat_api.Delivery(False, seat_api.QUEUE_AT_CAPACITY, exit_code=5)
        return seat_api.Delivery(False, seat_api.FAILED, exit_code=10)

    row = google_intake.deliver_queued(store, row, handoff, now=NOW)
    assert row.state == "queued" and row.attempts == 0
    assert "comms google-chat reply --from" in calls[0][0]
    assert "comms-files/out" in calls[0][0]
    for attempts in (1, 2, 3):
        row = google_intake.deliver_queued(store, row, handoff, now=NOW)
        assert row.attempts == attempts
    assert row.state == "abandoned" and len(calls) == 4


def test_ambiguous_handoff_does_not_retry_automatically(tmp_path):
    store = _store(tmp_path)
    row, _ = google_intake.ingest(store, _event(), _private(), now=NOW)

    def uncertain(*args, **kwargs):
        raise seat_api.SeatUnavailable("timed out")

    row = google_intake.deliver_queued(store, row, uncertain, now=NOW)
    assert row.state == "uncertain"
    assert google_intake.deliver_queued(store, row, uncertain, now=NOW).state == "uncertain"


def test_old_event_expires_without_seat_handoff(tmp_path):
    store = _store(tmp_path)
    row, _ = google_intake.ingest(store, _event(event_time="2026-11-15T11:00:00Z"),
                                  _private(), now=NOW)
    assert row.state == "expired"


def test_google_worker_failure_is_reported_without_escaping_hub_loop(tmp_path, monkeypatch):
    stop = Event()
    notices = []
    monkeypatch.setattr(google_chat, "load_settings", lambda *a: {AGENT: object()})

    def failed_private(*args):
        raise google_chat.GoogleChatConfigError("0.3 unavailable")

    monkeypatch.setattr(google_chat, "private_assignments", failed_private)

    def record(level, message):
        notices.append((level, message))
        stop.set()

    google_worker.run_forever(tmp_path, "agent-eco", "agent-comms", stop, record)
    assert len(notices) == 1
    assert notices[0][0] == "warn" and "0.3 unavailable" in notices[0][1]


def test_text_reply_uses_original_space_and_stable_request_id(tmp_path):
    store = _store(tmp_path)
    row, _ = google_intake.ingest(store, _event(), _private(), now=NOW)
    row = store.transition(row.id, from_state="queued", to_state="delivered")
    config = google_chat.AgentConfig(AGENT, "projects/fake/subscriptions/fake",
                                     tmp_path / "credential.json", "fake-folder")
    posts = []

    def post(outbound, configured):
        posts.append((outbound.request_id, outbound.payload, configured.agent))

    first = google_reply.send_text(store, row, "Done.", config, _private(), post)
    second = google_reply.send_text(store, row, "Done.", config, _private(), post)
    assert first.state == second.state == "sent"
    assert len(posts) == 1 and posts[0][1]["space"] == "spaces/dm"
    assert posts[0][1]["text"] == "Done."
    with pytest.raises(ValueError, match="different reply"):
        google_reply.send_text(store, row, "Changed.", config, _private(), post)


def test_failed_text_post_stays_pending_and_retries_same_request_id(tmp_path):
    store = _store(tmp_path)
    row, _ = google_intake.ingest(store, _event(), _private(), now=NOW)
    row = store.transition(row.id, from_state="queued", to_state="delivered")
    config = google_chat.AgentConfig(AGENT, "projects/fake/subscriptions/fake",
                                     tmp_path / "credential.json", "fake-folder")
    request_ids = []

    def fail(outbound, configured):
        request_ids.append(outbound.request_id)
        raise OSError("Google unavailable")

    with pytest.raises(OSError):
        google_reply.send_text(store, row, "Done.", config, _private(), fail)
    assert len(store.pending_replies()) == 1
    google_reply.send_text(store, row, "Done.", config, _private(),
                           lambda outbound, configured: request_ids.append(outbound.request_id))
    assert request_ids[0] == request_ids[1]
    assert store.pending_replies() == []


def test_removed_person_cannot_receive_a_late_reply(tmp_path):
    store = _store(tmp_path)
    row, _ = google_intake.ingest(store, _event(), _private(), now=NOW)
    row = store.transition(row.id, from_state="queued", to_state="delivered")
    config = google_chat.AgentConfig(AGENT, "projects/fake/subscriptions/fake",
                                     tmp_path / "credential.json", "fake-folder")
    with pytest.raises(google_chat.GoogleChatConfigError, match="no longer permitted"):
        google_reply.send_text(store, row, "Done.", config, _private([]),
                               lambda *a: pytest.fail("posted to a removed person"))
    assert store.pending_replies() == []


def test_cli_reply_rejects_wrong_message_id_without_post(seat, monkeypatch):
    root = google_chat.settings_dir()
    root.mkdir(mode=0o700)
    credential = root / f"{AGENT}.json"
    credential.write_text("{}")
    credential.chmod(0o600)
    settings_file = root / "settings.json"
    settings_file.write_text(json.dumps({AGENT: {
        "subscription": "projects/fake/subscriptions/fake",
        "credential_file": str(credential), "reply_drive_folder_id": "fake"}}))
    settings_file.chmod(0o600)
    monkeypatch.setattr(google_chat, "private_assignments", lambda *a: {AGENT: _private()})
    monkeypatch.setattr(google_reply, "send_text", lambda *a, **k:
                        pytest.fail("posted without an owned local message"))
    result = CliRunner().invoke(cli.main, ["google-chat", "reply", "--from", AGENT,
                                            "gc-999", "Done."])
    assert result.exit_code != 0
    assert "no Google Chat message gc-999" in result.output


def test_chat_event_normalizer_requires_app_direct_message():
    raw = {"type": "MESSAGE", "eventTime": "2026-11-16T11:59:00Z",
           "message": {"name": "spaces/dm/messages/one", "text": "Hello"},
           "space": {"name": "spaces/dm", "spaceType": "DIRECT_MESSAGE",
                     "singleUserBotDm": False},
           "user": {"name": "users/123", "email": "person@example.org", "type": "HUMAN",
                    "displayName": "Do not trust this"}}
    event = google_intake.from_chat_event(raw, AGENT)
    assert event.app_agent == ""
    assert event.sender_account_id == "users/123"
    assert "Do not trust this" not in repr(event)
