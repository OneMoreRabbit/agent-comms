"""2.9.3 opt-in Google foundation; no real seat or Google service is contacted."""

import json
import sqlite3

import pytest
from click.testing import CliRunner

from agent_comms import cli, google_chat
from agent_comms.google_store import GoogleStore

AGENT = "bakehouse.agent-eco.agent-comms"


def _configured(seat):
    root = google_chat.settings_dir()
    root.mkdir(mode=0o700)
    credential = root / f"{AGENT}.json"
    credential.write_text("{}", encoding="utf-8")
    credential.chmod(0o600)
    settings = root / "settings.json"
    settings.write_text(json.dumps({AGENT: {
        "subscription": "projects/fake/subscriptions/fake",
        "credential_file": str(credential),
        "reply_drive_folder_id": "fake-folder",
    }}), encoding="utf-8")
    settings.chmod(0o600)
    return settings


def test_no_settings_means_no_private_read_or_google_connection(seat, monkeypatch):
    monkeypatch.setattr(google_chat, "private_assignments",
                        lambda *a, **k: pytest.fail("unexpected private directory read"))
    for command in ("status", "doctor"):
        result = CliRunner().invoke(cli.main, ["google-chat", command])
        assert result.exit_code == 0, result.output
        assert "not configured" in result.output
    assert not google_chat.settings_file().exists()


def test_strict_settings_and_private_assignment(seat, monkeypatch):
    path = _configured(seat)
    state = path.parent.parent.parent / ".comms"
    parsed = google_chat.load_settings(state)
    assert parsed[AGENT].credential_file.name == f"{AGENT}.json"

    calls = []
    def private(*args, **kwargs):
        calls.append((args, kwargs))
        return {AGENT: {"agent": AGENT, "google_chat": {"people": []}}}
    monkeypatch.setattr(google_chat, "private_assignments", private)
    monkeypatch.setattr(google_chat, "google_dependencies_available", lambda: True)
    result = CliRunner().invoke(cli.main, ["google-chat", "doctor"])
    assert result.exit_code == 0, result.output
    assert "prepared" not in result.output
    assert "check the running daemon" in result.output
    assert len(calls) == 1

    raw = json.loads(path.read_text())
    raw[AGENT]["people"] = ["not allowed here"]
    path.write_text(json.dumps(raw))
    with pytest.raises(google_chat.GoogleChatConfigError, match="expected only"):
        google_chat.load_settings(state)

    raw[AGENT].pop("people")
    raw[AGENT]["credential_file"] = "/tmp/fake.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(google_chat.GoogleChatConfigError, match="credential_file must be"):
        google_chat.load_settings(state)


def test_unknown_agent_and_missing_private_record_fail_closed(seat, monkeypatch):
    path = _configured(seat)
    state = path.parent.parent.parent / ".comms"
    raw = json.loads(path.read_text())
    raw["bakehouse.agent-eco.other-seat"] = raw.pop(AGENT)
    path.write_text(json.dumps(raw))
    with pytest.raises(google_chat.GoogleChatConfigError, match="unassigned"):
        google_chat.load_settings(state)
    raw[AGENT] = raw.pop("bakehouse.agent-eco.other-seat")
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(google_chat, "private_assignments", lambda *a, **k: {AGENT: {}})
    result = CliRunner().invoke(cli.main, ["google-chat", "doctor"])
    assert result.exit_code != 0
    assert "no private google_chat.people" in result.output


def test_private_read_asks_for_03_and_never_touches_hub_cache(seat, monkeypatch):
    from pathlib import Path
    from urllib.parse import urlsplit
    cache = Path.home() / ".comms" / "routes.json"
    before = cache.read_bytes()
    requests = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return json.dumps({"contract": "0.3", "assignments": [
                {"agent": AGENT, "google_chat": {"people": []}}
            ]}).encode()
    def open_fake(request, timeout):
        requests.append(request.full_url)
        return Response()
    monkeypatch.setattr(google_chat.urllib.request, "urlopen", open_fake)
    rows = google_chat.private_assignments("agent-eco", "agent-comms")
    assert rows[AGENT]["google_chat"]["people"] == []
    assert len(requests) == 1
    assert urlsplit(requests[0]).path == "/v0/seats/agent-eco/agent-comms/assignments"
    assert urlsplit(requests[0]).query == "contract=0.3"
    assert cache.read_bytes() == before


def test_google_store_dedupes_without_changing_hub_rows(seat):
    db_path = google_chat.settings_dir().parent.parent / ".comms" / "comms.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
        db.execute("INSERT INTO messages(body) VALUES ('hub stays')")
    store = GoogleStore(db_path)
    first, inserted = store.receive(AGENT, "spaces/a/messages/b", "spaces/a",
                                    "2026-11-16T10:00:00Z", {"text": "first"})
    again, inserted_again = store.receive(AGENT, "spaces/a/messages/b", "spaces/a",
                                           "2026-11-16T10:00:00Z", {"text": "different"})
    assert inserted and not inserted_again
    assert first.id == again.id and again.payload == {"text": "first"}
    assert len(store.list(AGENT)) == 1
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT body FROM messages").fetchone()[0] == "hub stays"
