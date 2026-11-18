"""Sprint 1 Google file, notice and stand-in paths never contact live Google."""

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from agent_comms import (cli, google_chat, google_drive, google_intake,
                         google_media, google_notices, google_reply, google_standin,
                         google_worker, seat as seat_api)
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


@pytest.mark.parametrize("status,expected", [
    (403, "Drive API not enabled"),
    (404, "not found or not shared"),
    (503, "not reachable"),
])
def test_drive_folder_explains_http_failure(tmp_path, status, expected):
    class HttpError(Exception):
        response = SimpleNamespace(status_code=status)

    class Session:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, *args, **kwargs):
            raise HttpError()

    with pytest.raises(google_drive.DriveError, match=expected):
        google_drive.check_reply_folder(_config(tmp_path), lambda _: Session())


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


def test_agent_file_out_then_file_in_and_long_answer_repair_owned_directories(seat, tmp_path):
    root = seat / "comms-files"
    assert not root.exists()
    store = GoogleStore(tmp_path / "first-turn.db")
    row, _ = google_intake.ingest(store, _event(), _private())
    turns = []
    delivered = google_intake.deliver_queued(
        store, row,
        lambda body, *, agent: (turns.append(body) or
                                seat_api.Delivery(True, seat_api.DELIVERED)))
    output = root / "out" / AGENT
    assert delivered.state == "delivered" and str(output) in turns[0]
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700
               for path in (root, root / "out", output))

    # An agent can create its file and make these folders group-writable.
    # Both a reply and the next inbound file must tighten them again.
    for path in (root, root / "out", output):
        path.chmod(0o775)
    reply_file = output / "answer.txt"
    reply_file.write_text("answer", encoding="utf-8")
    assert google_drive.validate_outbound_file(AGENT, reply_file) == reply_file
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700
               for path in (root, root / "out", output))

    root.chmod(0o775)
    attachment = {"source": "UPLOADED_CONTENT", "contentType": "image/png",
                  "attachmentDataRef": {"resourceName": "spaces/dm/attachments/photo"}}
    incoming, _ = google_intake.ingest(
        store, _event(name="spaces/dm/messages/photo", attachments=(attachment,)), _private())
    prepared = google_media.prepare_queued(store, incoming, _config(tmp_path),
                                           download=lambda *_: [b"photo"])
    assert prepared.payload["files"][0]["bytes"] == 5
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "in").stat().st_mode) == 0o700

    output.chmod(0o775)
    answer = google_drive.create_long_answer(AGENT, "complete long answer")
    assert answer.read_text(encoding="utf-8") == "complete long answer"
    assert stat.S_IMODE(output.stat().st_mode) == 0o700


def test_google_file_directories_refuse_links_and_other_owners(seat, tmp_path, monkeypatch):
    root = seat / "comms-files"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(google_drive.DriveError, match="symlink"):
        google_drive.ensure_outbound_root(AGENT)
    store = GoogleStore(tmp_path / "linked-turn.db")
    row, _ = google_intake.ingest(store, _event(), _private())
    refused = google_intake.deliver_queued(
        store, row, lambda *_a, **_kw: pytest.fail("linked root reached the seat"))
    assert refused.state == "refused" and "symlink" in refused.reason
    assert store.notices()[0].payload["text"] == google_notices.NOT_DELIVERED
    root.unlink()
    root.mkdir(mode=0o775)
    root.chmod(0o775)
    real_uid = os.getuid()
    monkeypatch.setattr("agent_comms.google_paths.os.getuid", lambda: real_uid + 1)
    with pytest.raises(google_drive.DriveError, match="not owned"):
        google_drive.ensure_outbound_root(AGENT)
    assert stat.S_IMODE(root.stat().st_mode) == 0o775


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


def test_long_answer_then_file_are_distinct_and_each_repeat_is_inert(seat, tmp_path):
    store = GoogleStore(tmp_path / "two-replies.db")
    row = _delivered(store)
    posted, uploads = [], []
    def upload(config, path):
        uploads.append(path)
        return f"https://drive.google.com/file/d/{len(uploads)}/view"
    post = lambda outbound, _: posted.append(outbound)
    long_text = "L" * 35000
    first = google_reply.send_text(store, row, long_text, _config(tmp_path), _private(),
                                   post=post, upload=upload)
    same_first = google_reply.send_text(store, row, long_text, _config(tmp_path), _private(),
                                        post=post, upload=upload)
    file_path = Path.home() / "comms-files" / "out" / AGENT / "answer.txt"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("file answer")
    second = google_reply.send_text(store, row, "File follows", _config(tmp_path), _private(),
                                    post=post, upload=upload, file_path=file_path)
    same_second = google_reply.send_text(store, row, "File follows", _config(tmp_path), _private(),
                                         post=post, upload=upload, file_path=file_path)
    assert first.id == same_first.id and second.id == same_second.id
    assert first.id != second.id and first.request_id != second.request_id
    assert len(posted) == len(uploads) == 2
    assert len(store.replies()) == 2


def test_reply_folder_preflight_precedes_outbox_and_text_can_replace(seat, tmp_path):
    store = GoogleStore(tmp_path / "preflight.db")
    row = _delivered(store)
    with pytest.raises(google_drive.DriveError, match="not shared"):
        google_reply.send_text(store, row, "A" * 35000, _config(tmp_path), _private(),
                               check_folder=lambda _: (_ for _ in ()).throw(
                                   google_drive.DriveError("reply folder not shared")))
    assert store.reply_for_message(row.id) is None
    posted = []
    result = google_reply.send_text(store, row, "Text fallback", _config(tmp_path), _private(),
                                    post=lambda outbound, _: posted.append(outbound))
    assert result.state == "sent" and len(posted) == 1


def test_upload_is_bounded_and_failed_file_reply_can_be_replaced(seat, tmp_path):
    store = GoogleStore(tmp_path / "upload-bound.db")
    row = _delivered(store)
    calls = []

    def failed_upload(config, path):
        calls.append(path)
        raise OSError("offline")

    first = google_reply.send_text(store, row, "A" * 35000, _config(tmp_path), _private(),
                                   post=lambda *a: pytest.fail("post before upload"),
                                   upload=failed_upload)
    assert first.state == "pending" and first.upload_attempts == 1
    second = google_reply.retry_pending(store, first, _config(tmp_path), _private(),
                                        post=lambda *a: pytest.fail("post before upload"),
                                        upload=failed_upload)
    third = google_reply.retry_pending(store, second, _config(tmp_path), _private(),
                                       post=lambda *a: pytest.fail("post before upload"),
                                       upload=failed_upload)
    assert len(calls) == 3 and third.state == "failed"
    assert third.failure_reason == "Google Drive is not reachable"
    recovered = google_reply.send_text(store, row, "Text instead", _config(tmp_path), _private(),
                                       post=lambda *a: None)
    assert recovered.state == "sent" and recovered.request_id != third.request_id


def test_failed_file_reply_same_text_and_file_gets_a_new_attempt(seat, tmp_path):
    store = GoogleStore(tmp_path / "retry-same-file.db")
    row = _delivered(store)
    file_path = Path.home() / "comms-files" / "out" / AGENT / "answer.txt"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("reply file")
    uploads, posts = [], []

    def upload(config, path):
        uploads.append(path)
        if len(uploads) <= 3:
            raise OSError("offline")
        return "https://drive.google.com/file/d/recovered/view"

    first = google_reply.send_text(store, row, "Same text", _config(tmp_path), _private(),
                                   file_path=file_path, upload=upload,
                                   post=lambda out, _: posts.append(out))
    second = google_reply.retry_pending(store, first, _config(tmp_path), _private(),
                                        upload=upload, post=lambda out, _: posts.append(out))
    failed = google_reply.retry_pending(store, second, _config(tmp_path), _private(),
                                        upload=upload, post=lambda out, _: posts.append(out))
    assert failed.state == "failed" and len(uploads) == 3 and posts == []
    recovered = google_reply.send_text(store, row, "Same text", _config(tmp_path), _private(),
                                       file_path=file_path, upload=upload,
                                       post=lambda out, _: posts.append(out))
    assert recovered.state == "sent" and recovered.id != failed.id
    assert recovered.request_id != failed.request_id
    assert len(uploads) == 4 and len(posts) == 1
    again = google_reply.send_text(store, row, "Same text", _config(tmp_path), _private(),
                                   file_path=file_path, upload=upload,
                                   post=lambda out, _: posts.append(out))
    assert again.id == recovered.id and len(uploads) == 4 and len(posts) == 1


def test_uncertain_upload_is_not_repeated_after_restart(seat, tmp_path):
    from datetime import timedelta
    store = GoogleStore(tmp_path / "uncertain-upload.db")
    row = _delivered(store)
    first = google_reply.send_text(store, row, "A" * 35000, _config(tmp_path), _private(),
                                   upload=lambda *a: (_ for _ in ()).throw(OSError()),
                                   post=lambda *a: pytest.fail("no post"))
    store.begin_upload(first.id)  # Simulate a crash after Google accepted bytes.
    old_start = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
    with store._open() as db:
        db.execute("UPDATE google_outbound SET upload_started_at=? WHERE id=?",
                   (old_start, first.id))
    uploads = []
    result = google_reply.retry_pending(store, store.get_outbound(first.id), _config(tmp_path),
                                        _private(), upload=lambda *a: uploads.append(True),
                                        post=lambda *a: pytest.fail("no post"))
    assert result.state == "failed" and "uncertain" in result.failure_reason
    assert uploads == []


def test_completed_upload_with_unrecorded_link_is_not_repeated(seat, tmp_path, monkeypatch):
    store = GoogleStore(tmp_path / "unrecorded-link.db")
    row = _delivered(store)
    uploads = []
    monkeypatch.setattr(store, "set_outbound_link", lambda *a: None)

    def upload(config, path):
        uploads.append(path)
        return "https://drive.google.com/file/d/uploaded/view"

    with pytest.raises(google_reply.PermanentReplyError, match="could not be recorded"):
        google_reply.send_text(store, row, "A" * 35000, _config(tmp_path), _private(),
                               upload=upload, post=lambda *a: pytest.fail("no post"))
    result = store.reply_for_message(row.id)
    assert result.state == "failed" and result.upload_attempts == 1
    assert len(uploads) == 1
    google_reply.retry_pending(store, result, _config(tmp_path), _private(),
                               upload=upload, post=lambda *a: pytest.fail("no post"))
    assert len(uploads) == 1


def test_worker_does_not_fail_or_repeat_command_upload_in_progress(seat, tmp_path):
    store = GoogleStore(tmp_path / "comms.db")
    row = _delivered(store)
    uploading, release = Event(), Event()
    posts, outcomes, errors = [], [], []

    def slow_upload(config, path):
        uploading.set()
        assert release.wait(5)
        return "https://drive.google.com/file/d/one/view"

    def command():
        try:
            outcomes.append(google_reply.send_text(
                store, row, "A" * 35000, _config(tmp_path), _private(),
                upload=slow_upload, post=lambda outbound, config: posts.append(outbound)))
        except Exception as exc:
            errors.append(exc)

    thread = Thread(target=command)
    thread.start()
    try:
        assert uploading.wait(5)

        class Subscriber:
            def pull(self, **_):
                return SimpleNamespace(received_messages=[])
            def close(self):
                pass

        google_worker.run_once(
            tmp_path, "agent-eco", "test-claude",
            subscriber_factory=lambda _: Subscriber(),
            configured_override={AGENT: _config(tmp_path)},
            private_rows={AGENT: _private()},
            reply_post=lambda *a: pytest.fail("worker posted during command upload"),
            reply_upload=lambda *a: pytest.fail("worker repeated command upload"))
        pending = store.reply_for_message(row.id)
        assert pending.state == "pending" and pending.upload_in_progress
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive() and errors == []
    assert len(posts) == 1 and outcomes[0].state == "sent"
    assert "https://drive.google.com/file/d/one/view" in posts[0].payload["text"]


def test_command_does_not_fail_worker_upload_in_progress(seat, tmp_path):
    store = GoogleStore(tmp_path / "comms.db")
    row = _delivered(store)
    uploading, release = Event(), Event()
    posts, errors = [], []
    text = "A" * 35000
    source = google_drive.create_long_answer(AGENT, text)
    store.queue_reply(row, "Long answer (complete text in the linked file): " + text[:1000] + "…",
                      "worker-request", str(source), source_text=text, generated_answer=True)

    def slow_upload(config, path):
        uploading.set()
        assert release.wait(5)
        return "https://drive.google.com/file/d/two/view"

    def worker():
        try:
            google_reply.retry_pending(store, store.reply_for_message(row.id),
                                       _config(tmp_path), _private(),
                                       upload=slow_upload,
                                       post=lambda outbound, config: posts.append(outbound))
        except Exception as exc:
            errors.append(exc)

    thread = Thread(target=worker)
    thread.start()
    try:
        assert uploading.wait(5)
        pending = google_reply.send_text(store, row, text, _config(tmp_path), _private(),
                                         upload=lambda *a: pytest.fail("command repeated worker upload"),
                                         post=lambda *a: pytest.fail("command posted during worker upload"))
        assert pending.state == "pending" and pending.upload_in_progress
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive() and errors == []
    assert len(posts) == 1 and store.reply_for_message(row.id).state == "sent"


def test_chat_markdown_fallback_keeps_heading_table_and_code(seat, tmp_path):
    store = GoogleStore(tmp_path / "markup.db")
    row = _delivered(store)
    posts = []
    source = "# Heading\n| Name | Value |\n|---|---|\n| A | 42 |\n```\n# code\n```\n"
    google_reply.send_text(store, row, source, _config(tmp_path), _private(),
                           post=lambda outbound, config: posts.append(outbound))
    wire = posts[0].payload["text"]
    assert "*Heading*" in wire and "Name — Value" in wire and "A — 42" in wire
    assert "|---|---|" not in wire and "```\n# code\n```" in wire
    assert posts[0].payload["source_text"] == source


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


def test_noncontract_refusals_send_no_line_and_old_state_is_not_renotified(seat, tmp_path, monkeypatch):
    store = GoogleStore(tmp_path / "comms.db")
    group_event = _event()
    group_event = google_intake.IntakeEvent(**{**group_event.__dict__, "space_type": "SPACE"})
    refused, _ = google_intake.ingest(store, group_event, _private())
    assert refused.state == "refused" and store.notices() == []
    old, _ = google_intake.ingest(store, _event(name="spaces/dm/messages/old"), _private())
    store.transition(old.id, from_state="queued", to_state="expired", reason="old state")
    monkeypatch.setattr(google_chat, "load_settings", lambda *_: {AGENT: _config(tmp_path)})
    google_worker.run_once(tmp_path, "agent-eco", "test-claude",
                           subscriber_factory=lambda _: SimpleNamespace(
                               pull=lambda **_: SimpleNamespace(received_messages=[]), close=lambda: None),
                           private_rows={AGENT: _private()})
    assert store.notices() == []


def test_retirement_notice_uses_only_fixed_line(tmp_path):
    store = GoogleStore(tmp_path / "expired.db")
    from datetime import timedelta
    old = datetime.now(timezone.utc) - timedelta(days=2)
    event = _event()
    event = google_intake.IntakeEvent(**{**event.__dict__, "event_time": old.isoformat()})
    row, _ = google_intake.ingest(store, event, _private())
    assert row.state == "expired"
    notice = store.pending_notices()[0]
    assert notice.payload["text"] == google_notices.NOT_DELIVERED


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


def test_standin_bounded_pass_names_isolated_reply_and_never_builds_google(seat, tmp_path, monkeypatch):
    root = tmp_path / "stand-in"
    root.mkdir(mode=0o700)
    run = root / "two"
    run.mkdir(mode=0o700)
    marker = run / "marker.json"
    marker.write_text(json.dumps({"stand_in_run": 1, "agent": AGENT,
                                  "private_row": _private()}))
    marker.chmod(0o600)
    monkeypatch.setattr(google_standin, "ROOT", root)
    _, config, private_row, _ = google_standin.open_run(run, AGENT)
    monkeypatch.setattr(google_chat, "load_settings", lambda *a: pytest.fail("live settings"))
    monkeypatch.setattr(google_worker, "_subscriber", lambda *a: pytest.fail("live subscriber"))
    monkeypatch.setattr(google_media, "_download", lambda *a: pytest.fail("live media"))
    monkeypatch.setattr(google_reply, "_post", lambda *a: pytest.fail("live Chat"))
    monkeypatch.setattr(google_drive, "_session", lambda *a: pytest.fail("live Drive"))
    raw = {"type": "MESSAGE", "eventTime": datetime.now(timezone.utc).isoformat(),
           "message": {"name": "spaces/dm/messages/one", "text": "Hello",
                       "attachment": [{"source": "UPLOADED_CONTENT", "contentType": "image/png",
                                       "attachmentDataRef": {"resourceName": "spaces/dm/media/one"}}]},
           "space": {"name": "spaces/dm", "spaceType": "DIRECT_MESSAGE",
                     "singleUserBotDm": True},
           "user": {"name": "users/123", "email": "person@example.org", "type": "HUMAN"}}
    live_file = seat / "comms-files" / "in" / "gc-1" / "file-01.png"
    live_file.parent.mkdir(parents=True, mode=0o700)
    live_file.write_bytes(b"real Google file")
    acks, turns = [], []

    class Subscriber:
        def pull(self, **kwargs):
            message = SimpleNamespace(data=json.dumps(raw).encode())
            return SimpleNamespace(received_messages=[SimpleNamespace(message=message, ack_id="one")])
        def acknowledge(self, *, request):
            acks.extend(request["ack_ids"])
        def close(self):
            pass

    def handoff(body, *, agent):
        turns.append((body, agent))
        return seat_api.Delivery(True, seat_api.DELIVERED, exit_code=0)

    count = google_worker.run_once(
        run, "agent-eco", "test-claude", subscriber_factory=lambda config: Subscriber(),
        private_rows={AGENT: private_row}, configured_override={AGENT: config},
        media_download=lambda name, config: [b"photo"],
        notice_post=google_standin.recorder(run), reply_post=google_standin.recorder(run),
        reply_upload=google_standin.fake_upload(run), seat_handoff=handoff,
        stand_in_run=run)
    assert count == 1 and acks == ["one"]
    assert len(turns) == 1 and turns[0][1] == AGENT
    assert "--stand-in-run " + str(run) in turns[0][0]
    stand_in_file = run / "files" / "in" / "gc-1" / "file-01.png"
    assert stand_in_file.read_bytes() == b"photo"
    assert str(stand_in_file) in turns[0][0]
    assert live_file.read_bytes() == b"real Google file"
    store = GoogleStore(run / "comms.db")
    assert store.get("gc-1").state == "delivered"
    from datetime import timedelta
    assert google_media.cleanup_inbound(
        store, stand_in_run=run, now=datetime.now(timezone.utc) + timedelta(days=31)) == 1
    assert not stand_in_file.exists() and live_file.read_bytes() == b"real Google file"


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


def test_generated_long_answer_removed_after_30_days(seat, tmp_path):
    from datetime import timedelta
    store = GoogleStore(tmp_path / "long-retention.db")
    row = _delivered(store)
    reply = google_reply.send_text(store, row, "A" * 35000, _config(tmp_path), _private(),
                                   upload=lambda *a: "https://drive.google.com/file/d/one/view",
                                   post=lambda *a: None)
    path = Path(reply.payload["file_path"])
    assert path.exists() and reply.payload["generated_answer"] is True
    later = datetime.now(timezone.utc) + timedelta(days=31)
    assert google_drive.cleanup_long_answers(store, now=later) == 1
    assert not path.exists() and store.reply_for_message(row.id).state == "sent"


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
