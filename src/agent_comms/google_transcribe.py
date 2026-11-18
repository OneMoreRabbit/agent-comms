"""Voice-note transcription between media download and seat handoff."""

from __future__ import annotations

import os
import stat
import subprocess
from concurrent.futures import Future
from pathlib import Path
from threading import Lock, Thread

from .google_store import GoogleMessage, GoogleStore

AUDIO_TYPES = frozenset({"audio/mpeg", "audio/mp3", "audio/mp4", "audio/ogg",
                         "audio/webm", "audio/wav", "audio/x-wav"})


def is_audio(item: dict) -> bool:
    return item.get("content_type") in AUDIO_TYPES


def _transcribe_one(item: dict) -> dict:
    """Run the separate command without a shell; never capture transcript words."""
    audio = Path(item["path"])
    expected = audio.with_name(audio.name + ".txt")
    try:
        result = subprocess.run(
            ["agent-transcribe", str(audio)], capture_output=True, text=True,
            timeout=960, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "failed", "exit_code": 12}
    if result.returncode not in {0, 10, 11, 12, 13, 14, 15}:
        return {"status": "failed", "exit_code": 12}
    if result.returncode != 0:
        return {"status": "failed", "exit_code": result.returncode}
    # The command contract returns only the path. Do not trust stdout as text.
    if result.stdout.strip() != str(expected):
        return {"status": "failed", "exit_code": 14}
    try:
        info = expected.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("not an owned regular transcript")
        with expected.open("r", encoding="utf-8") as source:
            source.read(1)
    except (OSError, UnicodeError, ValueError):
        return {"status": "failed", "exit_code": 15}
    return {"status": "ok", "path": str(expected)}


def _transcribe_files(files: list[dict]) -> list[dict]:
    return [{**item, "transcription": _transcribe_one(item)} if is_audio(item) else item
            for item in files]


class TranscriptionPool:
    """At most two daemon threads; a slow command cannot delay worker shutdown."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._active = 0

    def submit(self, files: list[dict]) -> Future | None:
        with self._lock:
            if self._active >= 2:
                return None
            self._active += 1
        future: Future = Future()

        def run() -> None:
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(_transcribe_files(files))
                    except Exception as exc:
                        future.set_exception(exc)
            finally:
                with self._lock:
                    self._active -= 1

        Thread(target=run, name="google-transcribe", daemon=True).start()
        return future


def prepare_queued(store: GoogleStore, row: GoogleMessage,
                   jobs: dict[str, Future] | None = None,
                   executor: TranscriptionPool | None = None) -> tuple[GoogleMessage, bool]:
    """Return (row, ready); a slow service never blocks the daemon pass."""
    if row.state != "queued":
        return row, True
    files = row.payload.get("files", [])
    if not any(is_audio(item) and "transcription" not in item for item in files):
        return row, True
    if jobs is None:
        updated = store.set_files(row, _transcribe_files(files))
        return updated or store.get(row.id), True
    if executor is None:
        raise ValueError("transcription jobs need an executor")
    future = jobs.get(row.id)
    if future is None:
        submitted = executor.submit(files)
        if submitted is not None:
            jobs[row.id] = submitted
        return row, False
    if not future.done():
        return row, False
    del jobs[row.id]
    try:
        result = future.result()
    except Exception:
        result = [{**item, "transcription": {"status": "failed", "exit_code": 12}}
                  if is_audio(item) else item for item in files]
    updated = store.set_files(row, result)
    return updated or store.get(row.id), True
