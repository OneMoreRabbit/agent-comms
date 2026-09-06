"""One agent instance per seat, kept alive by a supervisor — not by delivery.

The line these tests defend: `comms wake` never starts a session (ADR-0009 §7e),
and `comms session ensure` does, when a supervisor calls it. Both have to stay
true together, so the tests assert the second without ever letting the first
acquire the power.
"""

from __future__ import annotations

import pytest

from agent_comms import session as session_mod
from agent_comms import wake as wake_mod
from agent_comms.config import Identity, Settings
from agent_comms.session import SessionError, ensure, status
from agent_comms.wake import Pane


def _settings(model="claude", model_session=None):
    return Settings(
        identity=Identity(project="agent-eco", seat="agent-comms"),
        channel="agent-eco",
        model=model,
        model_session=model_session,
    )


class _Ok:
    returncode = 0
    stderr = ""
    stdout = "%0"


class _Fail:
    returncode = 1
    stderr = "no such session"
    stdout = ""


def _panes(*specs):
    return [Pane(target=t, command=c, path="/home/dev/work", pid=p) for t, c, p in specs]


# -- claude ------------------------------------------------------------------

def test_a_running_claude_session_is_live(monkeypatch):
    monkeypatch.setattr(session_mod, "tmux_available", lambda: True)
    monkeypatch.setattr(session_mod, "list_panes", lambda: _panes(("rc:0.0", "claude", 10)))
    st = status(_settings())
    assert st.live and st.target == "rc:0.0"


def test_no_session_is_not_live(monkeypatch):
    monkeypatch.setattr(session_mod, "tmux_available", lambda: True)
    monkeypatch.setattr(session_mod, "list_panes", lambda: _panes(("rc:0.0", "bash", 10)))
    monkeypatch.setattr(session_mod, "find_runtime_panes", lambda p, r: [])
    assert status(_settings()).live is False


def test_two_sessions_is_not_live_and_will_not_be_fixed_automatically(monkeypatch):
    """One instance per seat is the requirement; a supervisor adding a third
    would break the thing it exists to maintain."""
    monkeypatch.setattr(session_mod, "tmux_available", lambda: True)
    monkeypatch.setattr(
        session_mod, "list_panes",
        lambda: _panes(("a:0.0", "claude", 10), ("b:0.0", "claude", 20)),
    )
    st = status(_settings())
    assert st.live is False
    with pytest.raises(SessionError, match="More than one instance"):
        ensure(_settings())


def test_ensure_starts_a_claude_session_when_there_is_none(monkeypatch):
    calls = []
    monkeypatch.setattr(session_mod, "tmux_available", lambda: True)
    monkeypatch.setattr(session_mod, "list_panes", lambda: [])
    monkeypatch.setattr(session_mod, "find_runtime_panes", lambda p, r: [])
    monkeypatch.setattr(session_mod.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(session_mod, "_run", lambda cmd: (calls.append(cmd), _Ok())[1])

    out = ensure(_settings(), workdir="/home/dev/work")
    assert "started claude session" in out
    assert calls[-1][:4] == ["tmux", "new-session", "-d", "-s"]
    assert calls[-1][-1] == "claude"


def test_ensure_is_idempotent(monkeypatch):
    monkeypatch.setattr(session_mod, "tmux_available", lambda: True)
    monkeypatch.setattr(session_mod, "list_panes", lambda: _panes(("rc:0.0", "claude", 10)))

    def boom(cmd):
        raise AssertionError("must not start anything when a session is already live")

    monkeypatch.setattr(session_mod, "_run", boom)
    assert ensure(_settings()).startswith("already live")


# -- codex -------------------------------------------------------------------

def test_codex_designated_thread_open_is_live(monkeypatch):
    monkeypatch.setattr(session_mod, "codex_daemon_running", lambda: True)
    monkeypatch.setattr(session_mod, "codex_lock_threads", lambda: ["uuid-1"])
    st = status(_settings(model="codex", model_session="uuid-1"))
    assert st.live and st.target == "uuid-1"


def test_codex_designated_thread_closed_is_not_live(monkeypatch):
    monkeypatch.setattr(session_mod, "codex_daemon_running", lambda: True)
    monkeypatch.setattr(session_mod, "codex_lock_threads", lambda: [])
    assert status(_settings(model="codex", model_session="uuid-1")).live is False


def test_codex_ensure_resumes_the_designated_thread_never_a_new_one(monkeypatch):
    """`resume` joins; starting fresh would create the second instance R1 forbids."""
    calls = []
    monkeypatch.setattr(session_mod, "codex_daemon_running", lambda: True)
    monkeypatch.setattr(session_mod, "codex_lock_threads", lambda: [])
    monkeypatch.setattr(session_mod, "tmux_available", lambda: True)
    monkeypatch.setattr(session_mod.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(session_mod, "_run", lambda cmd: (calls.append(cmd), _Ok())[1])

    out = ensure(_settings(model="codex", model_session="uuid-1"))
    assert "codex resume uuid-1" in out
    assert calls[-1][-1] == "codex resume uuid-1"


def test_codex_without_a_designated_thread_refuses_to_invent_one(monkeypatch):
    monkeypatch.setattr(session_mod, "codex_daemon_running", lambda: True)
    monkeypatch.setattr(session_mod, "codex_lock_threads", lambda: [])
    monkeypatch.setattr(session_mod, "tmux_available", lambda: True)
    with pytest.raises(SessionError, match="would create a second instance"):
        ensure(_settings(model="codex"))


def test_codex_without_a_daemon_will_not_start_one(monkeypatch):
    """The app-server is the estate's to run, not this client's."""
    monkeypatch.setattr(session_mod, "codex_daemon_running", lambda: False)
    monkeypatch.setattr(session_mod, "tmux_available", lambda: True)
    with pytest.raises(SessionError, match="estate's to run"):
        ensure(_settings(model="codex", model_session="uuid-1"))


# -- the line that must not move ---------------------------------------------

def test_wake_still_cannot_start_a_session(monkeypatch):
    """§7e. If delivery ever gains this power the rule is gone, whatever the
    comments say — so it is asserted here rather than trusted."""
    monkeypatch.setattr(wake_mod, "list_panes", lambda: _panes(("rc:0.0", "bash", 10)))
    monkeypatch.setattr(wake_mod, "_descendants", lambda pid, limit=200: [10])
    monkeypatch.setattr(wake_mod, "_process_matches", lambda pid, rt: False)

    def boom(*a, **k):
        raise AssertionError("wake must never start a session")

    monkeypatch.setattr(session_mod, "ensure", boom)
    outcome = wake_mod.wake(
        {"id": 1, "sender": "arch", "topic": "t", "content": "go"}, model="claude"
    )
    assert outcome.startswith("queued")
