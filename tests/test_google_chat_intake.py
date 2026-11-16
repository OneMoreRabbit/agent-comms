"""Stand-in Google intake: no live Google service or test seat is touched."""

from datetime import datetime, timedelta, timezone
import json
import sqlite3
from threading import Event
from types import SimpleNamespace

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


def _raw_message(name: str, *, text: str = "Hello") -> dict:
    return {"type": "MESSAGE", "eventTime": "2026-11-16T11:59:00Z",
            "message": {"name": name, "text": text},
            "space": {"name": "spaces/dm", "spaceType": "DIRECT_MESSAGE",
                      "singleUserBotDm": True},
            "user": {"name": "users/123", "email": "person@example.org", "type": "HUMAN"}}


def _received(raw: dict, ack_id: str):
    return SimpleNamespace(message=SimpleNamespace(data=json.dumps(raw).encode()), ack_id=ack_id)


def test_non_message_event_is_acked_once_without_content_and_next_message_runs(tmp_path, monkeypatch):
    config = google_chat.AgentConfig(AGENT, "projects/fake/subscriptions/fake",
                                     tmp_path / "credential.json", "fake-folder")
    monkeypatch.setattr(google_chat, "load_settings", lambda *_: {AGENT: config})
    records, acks, delivered = [], [], []
    non_message = {"type": "ADDED_TO_SPACE", "eventTime": "2026-11-16T11:59:00Z",
                   "user": {"name": "users/123"}, "text": "must not appear in log"}

    class Subscriber:
        def pull(self, **_):
            return SimpleNamespace(received_messages=[
                _received(non_message, "non-message"),
                _received(_raw_message("spaces/dm/messages/next"), "message")])
        def acknowledge(self, *, request):
            acks.extend(request["ack_ids"])
        def close(self):
            pass

    monkeypatch.setattr(google_intake, "deliver_queued",
                        lambda store, row: delivered.append(row.id))
    count = google_worker.run_once(tmp_path, "agent-eco", "test-claude",
                                   subscriber_factory=lambda _: Subscriber(),
                                   private_rows={AGENT: _private()},
                                   record=lambda level, message: records.append((level, message)))
    assert count == 1 and acks == ["non-message", "message"] and delivered == ["gc-1"]
    assert len(records) == 2 and all(level == "info" for level, _ in records)
    assert "form=interaction type=ADDED_TO_SPACE space_type=unknown" in records[0][1]
    assert "user.name=True user.email=False message=False outcome=ignored_no_message" in records[0][1]
    assert "form=interaction type=MESSAGE space_type=DIRECT_MESSAGE" in records[1][1]
    assert "user.name=True user.email=True message=True outcome=stored" in records[1][1]
    assert not any("must not appear" in line or "person@example.org" in line or "users/123" in line
                   for _, line in records)


def test_added_to_space_with_message_is_refused_acked_and_logged(tmp_path, monkeypatch):
    config = google_chat.AgentConfig(AGENT, "projects/fake/subscriptions/fake",
                                     tmp_path / "credential.json", "fake-folder")
    monkeypatch.setattr(google_chat, "load_settings", lambda *_: {AGENT: config})
    raw = _raw_message("spaces/dm/messages/added", text="private content")
    raw["type"] = "ADDED_TO_SPACE"
    acks, records = [], []

    class Subscriber:
        def pull(self, **_):
            return SimpleNamespace(received_messages=[_received(raw, "added")])
        def acknowledge(self, *, request):
            acks.extend(request["ack_ids"])
        def close(self):
            pass

    count = google_worker.run_once(tmp_path, "agent-eco", "test-claude",
                                   subscriber_factory=lambda _: Subscriber(),
                                   private_rows={AGENT: _private()},
                                   record=lambda level, message: records.append((level, message)))
    assert count == 1 and acks == ["added"]
    row = _store(tmp_path).get("gc-1")
    assert row.state == "refused" and row.reason == "unsupported event type"
    assert row.payload == {"sender_account_id": "users/123", "sender_address": "person@example.org"}
    assert len(records) == 1 and "type=ADDED_TO_SPACE" in records[0][1]
    assert "private content" not in records[0][1] and "person@example.org" not in records[0][1]


def test_event_metadata_rejects_untrusted_label_text():
    raw = {"type": "MESSAGE person@example.org", "space": {"spaceType": "DM private text"},
           "user": {"name": "users/123", "email": "person@example.org"}, "message": {}}
    line = google_worker._event_metadata(raw, "malformed")
    assert line == ("Google Chat event: form=interaction type=unknown space_type=unknown "
                    "user.name=True user.email=True message=True outcome=malformed")


def test_add_on_payload_is_acked_dropped_and_visible_in_google_log(tmp_path, monkeypatch):
    config = google_chat.AgentConfig(AGENT, "projects/fake/subscriptions/fake",
                                     tmp_path / "credential.json", "fake-folder")
    monkeypatch.setattr(google_chat, "load_settings", lambda *_: {AGENT: config})
    raw = {"chat": {"messagePayload": {"message": {"text": "private content"}}}}
    acks, records = [], []

    class Subscriber:
        def pull(self, **_):
            return SimpleNamespace(received_messages=[_received(raw, "add-on")])
        def acknowledge(self, *, request):
            acks.extend(request["ack_ids"])
        def close(self):
            pass

    count = google_worker.run_once(tmp_path, "agent-eco", "test-claude",
                                   subscriber_factory=lambda _: Subscriber(),
                                   private_rows={AGENT: _private()},
                                   record=lambda level, message: records.append((level, message)))
    assert count == 0 and acks == ["add-on"]
    assert _store(tmp_path).list() == []
    assert records == [("info", "Google Chat event: form=add_on type=unknown "
                      "space_type=unknown user.name=False user.email=False "
                      "message=False outcome=ignored_add_on_form")]
    assert "private content" not in records[0][1]


def test_google_log_shows_only_content_free_shape_lines(seat):
    root = google_chat.settings_dir()
    root.mkdir(mode=0o700)
    (root / "settings.json").write_text("{}")
    state_dir = root.parent.parent / ".comms"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "events.log").write_text(
        "2026-11-16T12:00:00+00:00 INFO Google Chat event: form=add_on "
        "type=unknown space_type=unknown user.name=False user.email=False "
        "message=False outcome=ignored_add_on_form\n"
        "2026-11-16T12:00:01+00:00 WARN unrelated private content person@example.org\n")
    result = CliRunner().invoke(cli.main, ["google-chat", "log"])
    assert result.exit_code == 0, result.output
    assert "form=add_on" in result.output
    assert "private content" not in result.output and "person@example.org" not in result.output


def test_pull_and_handoff_failure_do_not_skip_other_agent_or_reply(tmp_path, monkeypatch):
    other = "bakehouse.agent-eco.test-codex"
    configured = {agent: google_chat.AgentConfig(agent, "projects/fake/subscriptions/fake",
                                                tmp_path / "credential.json", "fake-folder")
                  for agent in (AGENT, other)}
    monkeypatch.setattr(google_chat, "load_settings", lambda *_: configured)
    store = _store(tmp_path)
    previous, _ = google_intake.ingest(store, _event(message_name="spaces/dm/messages/previous",
                                                  agent=other, app_agent=other),
                                       {**_private(), "agent": other}, now=NOW)
    previous = store.transition(previous.id, from_state="queued", to_state="delivered")
    outbound = store.queue_reply(previous, "reply", "request-previous")
    acks, records, retried = [], [], []

    class Subscriber:
        def __init__(self, agent):
            self.agent = agent
        def pull(self, **_):
            if self.agent == AGENT:
                raise OSError("first agent pull failed")
            return SimpleNamespace(received_messages=[
                _received(_raw_message("spaces/dm/messages/new"), "new")])
        def acknowledge(self, *, request):
            acks.extend(request["ack_ids"])
        def close(self):
            pass

    def failed_handoff(store, row):
        raise OSError("seat unavailable")

    def retry(store, outgoing, *_):
        retried.append(outgoing.id)
        store.mark_sent(outgoing.id)
        return store.get_outbound(outgoing.id)

    monkeypatch.setattr(google_intake, "deliver_queued", failed_handoff)
    monkeypatch.setattr(google_reply, "retry_pending", retry)
    count = google_worker.run_once(tmp_path, "agent-eco", "test-claude",
                                   subscriber_factory=lambda config: Subscriber(config.agent),
                                   private_rows={agent: {**_private(), "agent": agent}
                                                 for agent in configured},
                                   record=lambda level, message: records.append((level, message)),
                                   raise_on_error=False)
    assert count == 1 and acks == ["new"] and retried == [outbound.id]
    assert store.get_outbound(outbound.id).state == "sent"
    assert any("pull failed" in message for _, message in records)
    assert any("handoff failed" in message for _, message in records)


def test_empty_pull_deadline_still_processes_stored_handoff(tmp_path, monkeypatch):
    config = google_chat.AgentConfig(AGENT, "projects/fake/subscriptions/fake",
                                     tmp_path / "credential.json", "fake-folder")
    monkeypatch.setattr(google_chat, "load_settings", lambda *_: {AGENT: config})
    row, _ = google_intake.ingest(_store(tmp_path), _event(), _private(), now=NOW)

    class EmptyDeadline(Exception):
        pass

    class Subscriber:
        def pull(self, **_):
            raise EmptyDeadline()
        def close(self):
            pass

    monkeypatch.setattr(google_worker, "_empty_pull", lambda exc: isinstance(exc, EmptyDeadline))
    handed = []
    monkeypatch.setattr(google_intake, "deliver_queued", lambda store, message: handed.append(message.id))
    records = []
    count = google_worker.run_once(tmp_path, "agent-eco", "test-claude",
                                   subscriber_factory=lambda _: Subscriber(),
                                   private_rows={AGENT: _private()},
                                   record=lambda level, message: records.append((level, message)))
    assert count == 0 and handed == [row.id] and records == []


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
    assert "text" not in row.payload and row.space == ""
    assert row.payload.get("sender_account_id") == change.get("sender_account_id", "users/123")
    with sqlite3.connect(tmp_path / "comms.db") as db:
        stored = db.execute("SELECT payload FROM google_messages WHERE id=1").fetchone()[0]
    assert "Hello" not in stored


def test_refused_show_keeps_reason_but_not_text(seat):
    root = google_chat.settings_dir()
    root.mkdir(mode=0o700)
    (root / "settings.json").write_text("{}")
    store = GoogleStore(root.parent.parent / ".comms" / "comms.db")
    row, _ = google_intake.ingest(
        store, _event(text="PRIVATE REFUSED TEXT", sender_account_id="users/other"),
        _private(), now=NOW)
    assert row.state == "refused"
    result = CliRunner().invoke(cli.main, ["google-chat", "show", "--from", AGENT, row.id])
    assert result.exit_code == 0, result.output
    assert "person is not listed" in result.output
    assert "users/other" in result.output
    assert "PRIVATE REFUSED TEXT" not in result.output


def test_refused_text_is_not_stored_even_if_state_transition_crashes(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(store, "transition", lambda *a, **k: (_ for _ in ()).throw(
        OSError("simulated crash after receive")))
    with pytest.raises(OSError):
        google_intake.ingest(store, _event(text="PRIVATE CRASH TEXT",
                                           sender_account_id="users/other"),
                             _private(), now=NOW)
    with sqlite3.connect(tmp_path / "comms.db") as db:
        stored = db.execute("SELECT payload FROM google_messages WHERE id=1").fetchone()[0]
    assert "PRIVATE CRASH TEXT" not in stored


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
    config = google_chat.AgentConfig(AGENT, "projects/fake/subscriptions/fake",
                                     tmp_path / "credential.json", "fake-folder")
    posts = []
    outgoing = google_reply.send_text(store, row, "I got it", config, _private(),
                                      lambda out, _: posts.append(out.request_id))
    assert outgoing.state == "sent" and len(posts) == 1
    assert store.get(row.id).state == "delivered"
    assert "reply confirms" in store.get(row.id).reason


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
    with pytest.raises(google_reply.PermanentReplyError, match="no longer permitted"):
        google_reply.send_text(store, row, "Done.", config, _private([]),
                               lambda *a: pytest.fail("posted to a removed person"))
    assert store.pending_replies() == []


def test_pending_reply_removed_person_is_failed_once(tmp_path):
    store = _store(tmp_path)
    row, _ = google_intake.ingest(store, _event(), _private(), now=NOW)
    row = store.transition(row.id, from_state="queued", to_state="delivered")
    config = google_chat.AgentConfig(AGENT, "projects/fake/subscriptions/fake",
                                     tmp_path / "credential.json", "fake-folder")
    with pytest.raises(OSError):
        google_reply.send_text(store, row, "Done.", config, _private(),
                               lambda *_: (_ for _ in ()).throw(OSError("offline")))
    pending = store.pending_replies()[0]
    result = google_reply.retry_pending(store, pending, config, _private([]),
                                        lambda *_: pytest.fail("posted to removed person"))
    assert result.state == "failed" and "no longer permitted" in result.failure_reason
    assert store.pending_replies() == []


def test_pending_reply_permanent_google_refusal_and_24_hour_bound(tmp_path):
    store = _store(tmp_path)
    config = google_chat.AgentConfig(AGENT, "projects/fake/subscriptions/fake",
                                     tmp_path / "credential.json", "fake-folder")
    first, _ = google_intake.ingest(store, _event(), _private(), now=NOW)
    first = store.transition(first.id, from_state="queued", to_state="delivered")
    outgoing = store.queue_reply(first, "Done.", "request-1")

    class Refused(Exception):
        response = type("Response", (), {"status_code": 403})()

    result = google_reply.retry_pending(store, outgoing, config, _private(),
                                        lambda *_: (_ for _ in ()).throw(Refused()))
    assert result.state == "failed" and result.failure_reason.endswith("HTTP 403")
    with pytest.raises(google_reply.PermanentReplyError, match="HTTP 403"):
        google_reply.send_text(store, first, "Done.", config, _private(),
                               lambda *_: pytest.fail("retried a failed reply"))
    second, _ = google_intake.ingest(store, _event(message_name="spaces/dm/messages/two"),
                                     _private(), now=NOW)
    second = store.transition(second.id, from_state="queued", to_state="delivered")
    old = store.queue_reply(second, "Later.", "request-2")
    future = datetime.fromisoformat(old.created_at) + timedelta(hours=24, seconds=1)
    result = google_reply.retry_pending(store, old, config, _private(),
                                        lambda *_: pytest.fail("posted an expired reply"), now=future)
    assert result.state == "failed" and "24 hours" in result.failure_reason
    assert store.pending_replies() == []


def test_worker_failed_reply_does_not_block_the_next_reply(tmp_path, monkeypatch):
    config = google_chat.AgentConfig(AGENT, "projects/fake/subscriptions/fake",
                                     tmp_path / "credential.json", "fake-folder")
    monkeypatch.setattr(google_chat, "load_settings", lambda *_: {AGENT: config})
    store = _store(tmp_path)
    removed, _ = google_intake.ingest(
        store, _event(message_name="spaces/dm/messages/removed",
                      sender_account_id="users/removed", sender_address="removed@example.org"),
        _private([{"address": "removed@example.org", "account_id": "users/removed"}]), now=NOW)
    allowed, _ = google_intake.ingest(
        store, _event(message_name="spaces/dm/messages/allowed"), _private(), now=NOW)
    for row in (removed, allowed):
        delivered = store.transition(row.id, from_state="queued", to_state="delivered")
        store.queue_reply(delivered, "Answer", f"request-{row.id}")
    posts = []
    original_retry = google_reply.retry_pending

    def retry(store, outbound, config, private_row):
        return original_retry(store, outbound, config, private_row,
                              lambda posted, _: posts.append(posted.message_id))

    class Subscriber:
        def pull(self, **_):
            return SimpleNamespace(received_messages=[])
        def close(self):
            pass

    monkeypatch.setattr(google_reply, "retry_pending", retry)
    records = []
    google_worker.run_once(tmp_path, "agent-eco", "test-claude",
                           subscriber_factory=lambda _: Subscriber(),
                           private_rows={AGENT: _private()},
                           record=lambda level, message: records.append((level, message)))
    replies = {out.message_id: out for out in store.replies()}
    assert replies[removed.id].state == "failed"
    assert replies[allowed.id].state == "sent" and posts == [allowed.id]
    assert any("reply failed" in message for _, message in records)


def test_pending_reply_expires_even_if_private_directory_read_fails(tmp_path, monkeypatch):
    config = google_chat.AgentConfig(AGENT, "projects/fake/subscriptions/fake",
                                     tmp_path / "credential.json", "fake-folder")
    monkeypatch.setattr(google_chat, "load_settings", lambda *_: {AGENT: config})
    store = _store(tmp_path)
    row, _ = google_intake.ingest(store, _event(), _private(), now=NOW)
    row = store.transition(row.id, from_state="queued", to_state="delivered")
    outbound = store.queue_reply(row, "Answer", "request-old")
    old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    with sqlite3.connect(tmp_path / "comms.db") as db:
        db.execute("UPDATE google_outbound SET created_at=? WHERE id=?", (old_time, outbound.id))
    with pytest.raises(google_chat.GoogleChatConfigError, match="directory down"):
        with monkeypatch.context() as patch:
            patch.setattr(google_chat, "private_assignments",
                          lambda *_: (_ for _ in ()).throw(
                              google_chat.GoogleChatConfigError("directory down")))
            google_worker.run_once(tmp_path, "agent-eco", "test-claude")
    assert store.get_outbound(outbound.id).state == "failed"


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
