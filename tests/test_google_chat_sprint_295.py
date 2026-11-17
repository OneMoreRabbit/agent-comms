"""Sprint 1 Google file, notice and stand-in paths never contact live Google."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from agent_comms import (cli, google_chat, google_drive, google_intake,
                         google_media, google_notices, google_reply, google_standin)
from agent_comms.google_store import GoogleStore

AGENT = "bakehouse.agent-eco.agent-comms"


def _private():
    return {"agent": AGENT, "delivery": "inject", "google_chat": {
        "people": [{"address": "person@example.org", "account_id": "users/123"}]}}


def _event(*, name="spaces/dm/messages/one", account="users/123", attachments=()):
    return google_intake.IntakeEvent(
        agent=AGENT, message_name=name, space_name="spaces/dm",
        event_time=datetime.now(timezone.utc).isoformat(), text="Look at this",
        event_type="MESSAGE", space_type="DIRECT_MESSAGE", sender_type="HUMAN",
        sender_account_id=account, sender_address="person@example.org",
        app_agent=AGENT, has_attachments=bool(attachments), attachments=tuple(attachments))


def _config(tmp_path):
    return google_chat.AgentConfig(AGENT, "stand-in", tmp_path / "unused.json", "folder")


def _delivered(store):
    row, _ = google_intake.ingest(store, _event(), _private())
    return store.transition(row.id, from_state="queued", to_state="delivered")


def test_media_resource_token_is_opaque_and_jpeg_keeps_extension(seat, tmp_path):
    store = GoogleStore(tmp_path / "media.db")
    row, _ = google_intake.ingest(store, _event(attachments=[{
        "source": "UPLOADED_CONTENT", "contentType": "image/jpeg",
        "attachmentDataRef": {"resourceName": "spaces/x/attachments/a=+.%"}}]), _private())
    names = []

    def download(name, config):
        names.append(name)
        yield b"\xff\xd8\xffphoto"

    prepared = google_media.prepare_queued(store, row, _config(tmp_path), download=download)
    assert prepared.state == "queued" and names == ["spaces/x/attachments/a=+.%"]
    saved = Path(prepared.payload["files"][0]["path"])
    assert saved.suffix == ".jpg" and saved.read_bytes() == b"\xff\xd8\xffphoto"
    assert "File: " + str(saved) in google_intake.turn(prepared)


def test_drive_folder_requires_shared_drive_and_writer(tmp_path):
    class Response:
        def __init__(self, value):
            self.value = value
        def raise_for_status(self):
            pass
        def json(self):
            return self.value
    class Session:
        def __init__(self, value):
            self.value = value
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, *args, **kwargs):
            return Response(self.value)
    config = _config(tmp_path)
    with pytest.raises(google_drive.DriveError, match="not a shared-drive folder"):
        google_drive.check_reply_folder(config, lambda _: Session({"mimeType": "application/vnd.google-apps.folder"}))
    with pytest.raises(google_drive.DriveError, match="not writable"):
        google_drive.check_reply_folder(config, lambda _: Session({
            "mimeType": "application/vnd.google-apps.folder", "driveId": "drive",
            "capabilities": {"canAddChildren": False}}))
    google_drive.check_reply_folder(config, lambda _: Session({
        "mimeType": "application/vnd.google-apps.folder", "driveId": "drive",
        "capabilities": {"canAddChildren": True}}))


def test_drive_upload_uses_shared_drive_folder_and_returns_private_link(seat, tmp_path):
    root = Path.home() / "comms-files" / "out" / AGENT
    root.mkdir(mode=0o700, parents=True)
    file = root / "answer.pdf"
    file.write_bytes(b"PDF body")
    calls = []

    class Response:
        headers = {"Location": "https://www.googleapis.com/upload/session/one"}
        def __init__(self, value):
            self.value = value
        def raise_for_status(self):
            pass
        def json(self):
            return self.value

    class Session:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url, **kwargs):
            calls.append(("get", url, kwargs))
            return Response({"mimeType": "application/vnd.google-apps.folder",
                             "driveId": "shared", "capabilities": {"canAddChildren": True}})
        def post(self, url, **kwargs):
            calls.append(("post", url, kwargs))
            return Response({})
        def put(self, url, **kwargs):
            calls.append(("put", url, kwargs))
            assert kwargs["data"].read() == b"PDF body"
            return Response({"webViewLink": "https://drive.google.com/file/d/private/view"})

    link = google_drive.upload_file(_config(tmp_path), file, lambda config: Session())
    assert link == "https://drive.google.com/file/d/private/view"
    assert calls[1][2]["json"]["parents"] == ["folder"]
    assert calls[1][2]["params"]["supportsAllDrives"] == "true"
    assert calls[2][1] == "https://www.googleapis.com/upload/session/one"
    symlink = root / "shortcut.pdf"
    symlink.symlink_to(file)
    with pytest.raises(google_drive.DriveError, match="symlink"):
        google_drive.validate_outbound_file(AGENT, symlink)
    with pytest.raises(google_drive.DriveError, match="under the agent"):
        google_drive.validate_outbound_file(AGENT, tmp_path / "outside.pdf")


def test_long_reply_uploads_whole_text_before_post(seat, tmp_path):
    store = GoogleStore(tmp_path / "long.db")
    row = _delivered(store)
    posted, uploaded = [], []
    text = "A" * 35000

    def upload(config, path):
        uploaded.append(path)
        assert path.read_text() == text
        return "https://drive.google.com/file/d/stand-in/view"

    outgoing = google_reply.send_text(
        store, row, text, _config(tmp_path), _private(),
        post=lambda outbound, config: posted.append(outbound), upload=upload)
    assert outgoing.state == "sent" and len(uploaded) == 1 and len(posted) == 1
    assert len(posted[0].payload["text"].encode()) < 32000
    assert "Long answer" in posted[0].payload["text"]
    assert "https://drive.google.com/" in posted[0].payload["text"]
    assert posted[0].payload["source_text"] == text


def test_refusal_and_no_session_notices_are_durable_and_deduplicated(tmp_path):
    store = GoogleStore(tmp_path / "notices.db")
    refused, _ = google_intake.ingest(store, _event(account="users/other"), _private())
    assert refused.state == "refused" and refused.space == ""
    notices = store.pending_notices()
    assert len(notices) == 1 and notices[0].payload["text"] == google_notices.NOT_LISTED
    google_reply.retry_notice(store, notices[0], _config(tmp_path),
                              post=lambda outbound, config: None)
    assert store.notices()[0].state == "sent"
    row, _ = google_intake.ingest(store, _event(name="spaces/dm/messages/two"), _private())
    from agent_comms import seat as seat_api
    for _ in range(2):
        row = google_intake.deliver_queued(
            store, row, lambda *a, **k: seat_api.Delivery(False, seat_api.NO_SESSION, exit_code=5))
    assert row.state == "queued"
    no_session = [item for item in store.notices() if item.kind == "notice-no-session"]
    assert len(no_session) == 1 and no_session[0].payload["text"] == google_notices.NO_SESSION


def test_hidden_standin_reply_uses_run_store_and_no_google_client(seat, tmp_path, monkeypatch):
    root = tmp_path / "stand-in"
    root.mkdir(mode=0o700)
    run = root / "one"
    run.mkdir(mode=0o700)
    marker = run / "marker.json"
    marker.write_text(json.dumps({"stand_in_run": 1, "agent": AGENT,
                                  "private_row": _private()}))
    marker.chmod(0o600)
    monkeypatch.setattr(google_standin, "ROOT", root)
    store = GoogleStore(run / "comms.db")
    row = _delivered(store)
    monkeypatch.setattr(google_chat, "load_settings", lambda *a: pytest.fail("live settings read"))
    monkeypatch.setattr(google_reply, "_post", lambda *a: pytest.fail("live Chat post"))
    monkeypatch.setattr(google_drive, "_session", lambda *a: pytest.fail("live Drive client"))
    help_result = CliRunner().invoke(cli.main, ["google-chat", "reply", "--help"])
    assert "stand-in-run" not in help_result.output
    wrong = CliRunner().invoke(cli.main, ["google-chat", "reply", "--from", AGENT,
                                           "--stand-in-run", str(tmp_path), row.id, "Answer"])
    assert wrong.exit_code != 0 and not (run / "posts.jsonl").exists()
    result = CliRunner().invoke(cli.main, ["google-chat", "reply", "--from", AGENT,
                                            "--stand-in-run", str(run), row.id, "Answer"])
    assert result.exit_code == 0, result.output
    posts = [json.loads(line) for line in (run / "posts.jsonl").read_text().splitlines()]
    assert len(posts) == 1 and posts[0]["text"] == "Answer"
    assert posts[0]["space"] == "spaces/dm"


def test_media_download_fault_has_three_attempt_bound(seat, tmp_path):
    store = GoogleStore(tmp_path / "fault.db")
    row, _ = google_intake.ingest(store, _event(attachments=[{
        "source": "UPLOADED_CONTENT", "contentType": "image/png",
        "attachmentDataRef": {"resourceName": "spaces/x/attachments/y"}}]), _private())

    def offline(name, config):
        raise OSError("offline")
        yield b""

    for attempt in (1, 2, 3):
        row = google_media.prepare_queued(store, row, _config(tmp_path), download=offline)
        assert row.attempts == attempt
    assert row.state == "refused" and row.reason == "media download unavailable after three attempts"
    assert store.pending_notices()[0].payload["text"] == google_notices.NOT_DELIVERED


def test_inbound_generated_file_removed_after_30_days_but_metadata_kept(seat, tmp_path):
    store = GoogleStore(tmp_path / "retention.db")
    event = _event(attachments=[{"source": "UPLOADED_CONTENT", "contentType": "image/png",
                                 "attachmentDataRef": {"resourceName": "spaces/x/attachments/y"}}])
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    event = google_intake.IntakeEvent(**{**event.__dict__, "event_time": old.isoformat()})
    row, _ = google_intake.ingest(store, event, _private(), now=old)
    row = google_media.prepare_queued(store, row, _config(tmp_path),
                                      download=lambda name, config: [b"photo"], now=old)
    path = Path(row.payload["files"][0]["path"])
    assert path.exists()
    count = google_media.cleanup_inbound(store, now=datetime(2026, 2, 2, tzinfo=timezone.utc))
    assert count == 1 and not path.exists() and store.get(row.id).payload["files"]


def test_every_fixed_refusal_reason_is_classified_in_google_log(seat):
    settings = google_chat.settings_dir()
    settings.mkdir(mode=0o700)
    (settings / "settings.json").write_text("{}")
    store = GoogleStore(settings.parent.parent / ".comms" / "comms.db")
    reasons = [
        "person is not listed", "attachment reference is unavailable",
        "more than ten attachments", "malformed attachment reference",
        "Drive-backed attachment is not supported", "file too large",
        "inbound file directory is not owner-only", "inbound file path is not a regular file",
        "media download returned non-bytes data", "media download unavailable after three attempts",
        "seat turn exceeds 65536 UTF-8 bytes", "empty message has no text to deliver",
        "unsupported event type", "not a direct message", "message is for another Chat app",
        "sender is not a person", "agent delivery is none",
        "agent delivery mode is unavailable", "failed", "Google event time is in the future",
    ]
    for number, reason in enumerate(reasons):
        row, _ = store.receive(AGENT, "spaces/dm/messages/" + str(number), "spaces/dm",
                               datetime.now(timezone.utc).isoformat(), {})
        store.transition(row.id, from_state="received", to_state="refused", reason=reason)
    output = CliRunner().invoke(cli.main, ["google-chat", "log"])
    assert output.exit_code == 0, output.output
    assert "unclassified" not in output.output
    assert output.output.count("  refused (") == len(reasons)
