"""Finding a live codex session — verified on a healthy seat, 2026-09-06.

`thread/loaded/list` is the better answer in principle and is in codex's own
published schema. It returns nothing on 0.153.4, tested against a *healthy*
app-server with a session loaded, through `codex app-server proxy` under both
framings and against the control socket directly.

The writer-lock directory does work: the id it yields is the one `codex queue`
accepts. These tests pin that, and pin the ordering so the app-server route takes
over automatically if it ever starts answering.
"""

from __future__ import annotations

import pytest

from agent_comms import wake as wake_mod
from agent_comms.wake import (
    WakeError,
    codex_live_threads,
    codex_lock_threads,
    wake,
)


class _Ok:
    returncode = 0
    stderr = ""
    stdout = "> "


MENTION = {"id": 1, "sender": "arch", "topic": "t", "content": "go"}


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    (home / "thread-writer-locks").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def _lock(home, thread_id):
    (home / "thread-writer-locks" / f"{thread_id}.lock").write_text("", encoding="utf-8")


# -- the writer locks --------------------------------------------------------

def test_a_writer_lock_is_a_live_thread(codex_home):
    _lock(codex_home, "01a07571-e326-7f12-9514-5cae8429f404")
    assert codex_lock_threads() == ["01a07571-e326-7f12-9514-5cae8429f404"]


def test_the_coordination_lock_is_not_a_thread(codex_home):
    """`.coordination.lock` sits in the same directory and is not a session."""
    (codex_home / "thread-writer-locks" / ".coordination.lock").write_text("", encoding="utf-8")
    assert codex_lock_threads() == []


def test_no_locks_means_no_live_threads(codex_home):
    assert codex_lock_threads() == []


def test_a_missing_lock_directory_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nothing-here"))
    assert codex_lock_threads() == []


# -- route selection ---------------------------------------------------------

def test_locks_are_preferred_and_the_route_is_named(codex_home, monkeypatch):
    _lock(codex_home, "abc")

    def boom(timeout=6):
        raise AssertionError("the app-server must not be called when locks answer")

    monkeypatch.setattr(wake_mod, "codex_loaded_threads", boom)
    assert codex_live_threads() == (["abc"], "writer locks")


def test_the_app_server_takes_over_if_it_ever_answers(codex_home, monkeypatch):
    """Ordering is deliberate: no locks, so the better route gets its chance."""
    monkeypatch.setattr(wake_mod, "codex_loaded_threads", lambda timeout=6: ["from-server"])
    assert codex_live_threads() == (["from-server"], "app-server")


def test_an_unreachable_app_server_is_not_fatal(codex_home, monkeypatch):
    def refuses(timeout=6):
        raise WakeError("no response")

    monkeypatch.setattr(wake_mod, "codex_loaded_threads", refuses)
    assert codex_live_threads() == ([], "writer locks")


# -- through wake ------------------------------------------------------------

def test_wake_discovers_and_names_the_route(codex_home, monkeypatch):
    _lock(codex_home, "01a07571")
    monkeypatch.setattr(wake_mod, "codex_daemon_running", lambda: True)
    monkeypatch.setattr(wake_mod.shutil, "which", lambda n: "/usr/bin/codex")
    ran = []
    monkeypatch.setattr(wake_mod, "_run", lambda cmd: (ran.append(cmd), _Ok())[1])

    outcome = wake(MENTION, model="codex")
    assert outcome == "delivered to codex session 01a07571 (discovered via writer locks)"
    assert ran[0][:4] == ["codex", "queue", "--thread", "01a07571"]


def test_two_live_threads_refuse_rather_than_pick(codex_home, monkeypatch):
    _lock(codex_home, "one")
    _lock(codex_home, "two")
    monkeypatch.setattr(wake_mod, "codex_daemon_running", lambda: True)
    monkeypatch.setattr(wake_mod.shutil, "which", lambda n: "/usr/bin/codex")
    with pytest.raises(WakeError, match="UNREACHABLE"):
        wake(MENTION, model="codex")


def test_no_live_thread_queues(codex_home, monkeypatch):
    monkeypatch.setattr(wake_mod, "codex_daemon_running", lambda: True)
    monkeypatch.setattr(wake_mod.shutil, "which", lambda n: "/usr/bin/codex")
    monkeypatch.setattr(wake_mod, "codex_loaded_threads", lambda timeout=6: [])
    outcome = wake(MENTION, model="codex")
    assert outcome.startswith("queued")
    assert "never starts an agent" in outcome


# -- P1: the refusal must be loud, and never a guess --------------------------

def test_the_refusal_names_the_threads_their_age_and_the_remedy(codex_home, monkeypatch):
    """§9: the refusal was right; its silence was the defect. A seat was
    unreachable for 21 minutes because nobody watching it learned that."""
    _lock(codex_home, "aaa")
    _lock(codex_home, "bbb")
    monkeypatch.setattr(wake_mod, "codex_daemon_running", lambda: True)
    monkeypatch.setattr(wake_mod.shutil, "which", lambda n: "/usr/bin/codex")
    with pytest.raises(WakeError) as exc:
        wake(MENTION, model="codex")
    text = str(exc.value)
    assert "UNREACHABLE" in text
    assert "aaa" in text and "bbb" in text
    assert "last active" in text
    assert "codex archive" in text


def test_most_recent_is_only_used_when_declared(codex_home, monkeypatch):
    """Declared, not inferred. §7g one layer down is still §7g."""
    import os
    import time

    _lock(codex_home, "older")
    time.sleep(0.02)
    _lock(codex_home, "newer")
    os.utime(codex_home / "thread-writer-locks" / "newer.lock", (time.time(), time.time()))

    monkeypatch.setattr(wake_mod, "codex_daemon_running", lambda: True)
    monkeypatch.setattr(wake_mod.shutil, "which", lambda n: "/usr/bin/codex")
    monkeypatch.setattr(wake_mod, "codex_lock_threads", lambda: ["older", "newer"])
    ran = []
    monkeypatch.setattr(wake_mod, "_run", lambda cmd: (ran.append(cmd), _Ok())[1])

    outcome = wake(MENTION, model="codex", selection="most-recent")
    assert "CHOSE most-recent" in outcome
    assert "was a choice, not a lookup" in outcome
    assert ran[0][3] == "newer"
