"""Voice notes use a separate command without delaying other Google turns."""

from __future__ import annotations

from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_comms import google_chat, google_intake, google_media, google_transcribe, google_worker
from agent_comms.google_store import GoogleStore


AGENT = "bakehouse.agent-eco.test-claude"


def _message(store: GoogleStore, name: str, *, audio: bool, content_type: str | None = None):
    event = google_intake.IntakeEvent(
        agent=AGENT, message_name=name, space_name="spaces/dm",
        event_time=datetime.now(timezone.utc).isoformat(), text="A made-up test",
        event_type="MESSAGE", space_type="DIRECT_MESSAGE", sender_type="HUMAN",
        sender_account_id="users/123", sender_address="person@example.org",
        app_agent=AGENT, has_attachments=audio,
        attachments=({"source": "UPLOADED_CONTENT", "contentType": content_type,
                      "attachmentDataRef": {"resourceName": "media/voice"}},) if audio else (),
    )
    row, _ = google_intake.ingest(store, event, {
        "agent": AGENT, "delivery": "inject",
        "google_chat": {"people": [{"address": "person@example.org", "account_id": "users/123"}]},
    })
    return row


def test_voice_success_uses_command_without_shell_and_one_line_turn(seat, monkeypatch):
    store = GoogleStore(Path.home() / ".comms" / "comms.db")
    row = _message(store, "spaces/dm/messages/voice", audio=True)
    audio = Path.home() / "work" / "comms-files" / "in" / row.id / "file-01.mp3"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"made-up audio")
    row = store.set_files(row, [{"path": str(audio), "content_type": "audio/mpeg", "bytes": 13}])
    calls = []

    def command(args, **kwargs):
        calls.append((args, kwargs))
        transcript = Path(str(audio) + ".txt")
        transcript.write_text("This is a made-up transcript.\nNo real voice.", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=str(transcript) + "\n", stderr="")

    monkeypatch.setattr(google_transcribe.subprocess, "run", command)
    prepared, ready = google_transcribe.prepare_queued(store, row)
    assert ready and prepared.payload["files"][0]["transcription"]["status"] == "ok"
    body = google_intake.turn(prepared)
    assert "Transcript: This is a made-up transcript. No real voice." in body
    assert "Transcript file: " + str(audio) + ".txt" in body
    assert "File: " + str(audio) in body
    assert "Say what you heard before acting on anything that matters." in body
    assert "\n" not in body and "\r" not in body
    assert "made-up transcript" not in str(store.get(row.id).payload)
    assert calls == [(["agent-transcribe", str(audio)], {
        "capture_output": True, "text": True, "timeout": 960, "check": False})]


@pytest.mark.parametrize("exit_code", [10, 11, 12, 13, 14, 15])
def test_voice_failure_keeps_audio_and_fixed_line(seat, monkeypatch, exit_code):
    store = GoogleStore(Path.home() / ".comms" / "comms.db")
    row = _message(store, "spaces/dm/messages/fail", audio=True)
    audio = Path.home() / "work" / "file-01.mp3"
    row = store.set_files(row, [{"path": str(audio), "content_type": "audio/mpeg", "bytes": 12}])
    monkeypatch.setattr(google_transcribe.subprocess, "run", lambda *_a, **_kw:
                        SimpleNamespace(returncode=exit_code, stdout="", stderr="sensitive service error"))
    prepared, ready = google_transcribe.prepare_queued(store, row)
    body = google_intake.turn(prepared)
    assert ready and "Audio was not transcribed." in body
    assert str(audio) in body
    assert "sensitive service error" not in body
    assert prepared.payload["files"][0]["transcription"] == {
        "status": "failed", "exit_code": exit_code}


def test_long_transcript_keeps_path_without_shortening_store(seat, monkeypatch):
    store = GoogleStore(Path.home() / ".comms" / "comms.db")
    row = _message(store, "spaces/dm/messages/long", audio=True)
    audio = Path.home() / "work" / "long.mp3"
    transcript = Path(str(audio) + ".txt")
    transcript.write_text("word " * 20_000, encoding="utf-8")
    row = store.set_files(row, [{"path": str(audio), "content_type": "audio/mpeg", "bytes": 12,
                                 "transcription": {"status": "ok", "path": str(transcript)}}])
    body = google_intake.turn(row)
    assert "Transcript file: " + str(transcript) in body
    assert "Transcript: " not in body
    assert len(body.encode("utf-8")) <= 65_536
    assert transcript.stat().st_size > 65_536


def test_voice_transcript_is_removed_with_audio_after_retention(seat):
    store = GoogleStore(Path.home() / ".comms" / "comms.db")
    row = _message(store, "spaces/dm/messages/old", audio=True)
    audio = Path.home() / "work" / "comms-files" / "in" / row.id / "file-01.mp3"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"made-up audio")
    transcript = Path(str(audio) + ".txt")
    transcript.write_text("made-up words", encoding="utf-8")
    row = store.set_files(row, [{"path": str(audio), "content_type": "audio/mpeg", "bytes": 13,
                                 "transcription": {"status": "ok", "path": str(transcript)}}])
    future = datetime.now(timezone.utc) + timedelta(days=31)
    assert google_media.cleanup_inbound(store, now=future) == 2
    assert not audio.exists() and not transcript.exists()
    assert store.get(row.id) is not None


def test_mpeg_download_uses_audio_extension(seat):
    store = GoogleStore(Path.home() / ".comms" / "comms.db")
    row = _message(store, "spaces/dm/messages/media", audio=True,
                   content_type="audio/mpeg")
    prepared = google_media.prepare_queued(store, row,
        google_chat.AgentConfig(AGENT, "projects/fake/subscriptions/fake",
                                Path.home() / "credential.json", "fake"),
        download=lambda *_: [b"made-up audio"])
    assert prepared.payload["files"][0]["path"].endswith(".mp3")


def test_pending_transcription_does_not_hold_text_handoff(seat, monkeypatch):
    store = GoogleStore(Path.home() / ".comms" / "comms.db")
    voice = _message(store, "spaces/dm/messages/voice", audio=True)
    audio = Path.home() / "work" / "voice.mp3"
    store.set_files(voice, [{"path": str(audio), "content_type": "audio/mpeg", "bytes": 12}])
    text = _message(store, "spaces/dm/messages/text", audio=False)
    future = Future()
    jobs = {}

    class Executor:
        def submit(self, files):
            return future

    config = google_chat.AgentConfig(AGENT, "projects/fake/subscriptions/fake",
                                     Path.home() / "credential.json", "fake")
    monkeypatch.setattr(google_chat, "load_settings", lambda *_: {AGENT: config})
    handed = []
    def handoff(message_store, row):
        handed.append(row.id)
        message_store.transition(row.id, from_state="queued", to_state="delivered")
    monkeypatch.setattr(google_intake, "deliver_queued", handoff)

    class Subscriber:
        def pull(self, **_):
            return SimpleNamespace(received_messages=[])
        def close(self):
            pass

    kwargs = dict(subscriber_factory=lambda _config: Subscriber(),
                  private_rows={AGENT: {"agent": AGENT, "delivery": "inject",
                                        "google_chat": {"people": []}}},
                  cleanup_files=False, transcription_jobs=jobs,
                  transcription_executor=Executor())
    google_worker.run_once(store.path.parent, "agent-eco", "test-claude", **kwargs)
    assert handed == [text.id] and voice.id in jobs
    future.set_result([{"path": str(audio), "content_type": "audio/mpeg", "bytes": 12,
                        "transcription": {"status": "failed", "exit_code": 12}}])
    google_worker.run_once(store.path.parent, "agent-eco", "test-claude", **kwargs)
    assert handed == [text.id, voice.id]
