"""Per-runtime dispatch on the seat's declared model — ADR-0009 §7g.

The pilot proved that inferring the delivery target from tmux fails twice over:
it missed a headless codex, and it missed a Claude launched through a wrapper.
These tests pin the declared-not-scanned behaviour that replaced it.
"""

from __future__ import annotations

import pytest

from agent_comms import wake as wake_mod
from agent_comms.wake import Pane, WakeError, wake


def _panes(*specs):
    return [Pane(target=t, command=c, path="/home/dev/work", pid=p)
            for t, c, p in specs]


class _Ok:
    returncode = 0
    stderr = ""
    stdout = "> "


MENTION = {"id": 1, "sender": "arch", "topic": "t", "content": "go"}


# -- claude: declared, and found despite a wrapper ---------------------------

def test_declared_target_is_used_directly_without_searching(monkeypatch):
    """The declared path: no list-panes, no process walk, no candidates."""
    calls = []

    def boom():
        raise AssertionError("a declared target must not trigger a search")

    monkeypatch.setattr(wake_mod, "list_panes", boom)
    monkeypatch.setattr(wake_mod, "_tmux", lambda *a: (calls.append(a), _Ok())[1])
    outcome = wake(MENTION, model="claude", session="rc")
    assert outcome == "delivered to rc (claude, declared target)"
    assert [c[0] for c in calls] == ["list-panes", "capture-pane", "send-keys", "send-keys"]
    assert calls[0][:3] == ("list-panes", "-t", "rc"), "existence check, not a search"


def test_a_stale_declaration_loses_to_reality(monkeypatch):
    """Someone started Claude somewhere else. Reality wins, and says why.

    A declared session is a hint about runtime state, not a fact the estate owns
    outright. Treating it as authoritative would queue messages forever while an
    agent sat in another session — worse than the searching it replaced.
    """
    def tmux(*a):
        class R:
            # the declared target does not exist; everything else works
            returncode = 1 if a[0] == "list-panes" and "-t" in a else 0
            stdout = "> "
            stderr = "can't find session"
        return R()

    monkeypatch.setattr(wake_mod, "list_panes", lambda: _panes(("work:0.0", "claude", 10)))
    monkeypatch.setattr(wake_mod, "_tmux", tmux)
    outcome = wake(MENTION, model="claude", session="rc")
    assert "delivered to work:0.0" in outcome
    assert "declared target 'rc' does not exist" in outcome


def test_a_stale_declaration_with_nothing_running_still_queues(monkeypatch):
    def tmux(*a):
        class R:
            returncode = 1
            stdout = ""
            stderr = "no"
        return R()

    monkeypatch.setattr(wake_mod, "list_panes", lambda: _panes(("rc:0.0", "bash", 10)))
    monkeypatch.setattr(wake_mod, "_descendants", lambda pid, limit=200: [10])
    monkeypatch.setattr(wake_mod, "_process_matches", lambda pid, rt: False)
    monkeypatch.setattr(wake_mod, "_tmux", tmux)
    outcome = wake(MENTION, model="claude", session="rc")
    assert outcome.startswith("queued")


def test_a_declaration_settles_what_a_search_cannot(monkeypatch):
    """Two Claude sessions: the search refuses, the declaration decides."""
    monkeypatch.setattr(
        wake_mod, "list_panes",
        lambda: _panes(("rc:0.0", "claude", 10), ("work:0.0", "claude", 20)),
    )
    monkeypatch.setattr(wake_mod, "_tmux", lambda *a: _Ok())

    with pytest.raises(WakeError, match="no single answer"):
        wake(MENTION, model="claude")

    assert wake(MENTION, model="claude", session="rc").endswith("(claude, declared target)")


def test_undeclared_target_searches_within_the_declared_runtime(monkeypatch):
    calls = []
    monkeypatch.setattr(wake_mod, "list_panes", lambda: _panes(("rc:0.0", "claude", 10)))
    monkeypatch.setattr(wake_mod, "_tmux", lambda *a: (calls.append(a), _Ok())[1])
    outcome = wake(MENTION, model="claude")
    assert "BY SEARCH" in outcome
    assert "declare it in ~/.seat/seat.yml" in outcome


def test_claude_behind_a_wrapper_is_still_found_by_the_fallback(monkeypatch):
    """The blocks/service failure: pane reports bash, claude is a descendant.

    `pane_current_command` is a property of how the agent was launched. The
    process tree is a property of what is running.
    """
    monkeypatch.setattr(wake_mod, "list_panes", lambda: _panes(("rc:0.0", "bash", 10)))
    monkeypatch.setattr(wake_mod, "_descendants", lambda pid, limit=200: [10, 11])
    monkeypatch.setattr(wake_mod, "_process_matches", lambda pid, rt: pid == 11 and rt == "claude")
    monkeypatch.setattr(wake_mod, "_tmux", lambda *a: _Ok())
    assert wake(MENTION, model="claude").startswith("delivered to rc:0.0")
    # the fallback, not the declared path — see the label


def test_declared_claude_with_none_running_queues(monkeypatch):
    monkeypatch.setattr(wake_mod, "list_panes", lambda: _panes(("rc:0.0", "bash", 10)))
    monkeypatch.setattr(wake_mod, "_descendants", lambda pid, limit=200: [10])
    monkeypatch.setattr(wake_mod, "_process_matches", lambda pid, rt: False)
    outcome = wake(MENTION, model="claude")
    assert outcome.startswith("queued")
    assert "never starts an agent" in outcome


# -- codex: no tmux involved at all ------------------------------------------

def test_declared_codex_never_touches_tmux(monkeypatch):
    """Codex under remote control has no pane. That is why the scan missed it."""
    def boom(*a, **k):
        raise AssertionError("the codex path must not go near tmux")

    monkeypatch.setattr(wake_mod, "list_panes", boom)
    monkeypatch.setattr(wake_mod, "_tmux", boom)
    monkeypatch.setattr(wake_mod, "codex_daemon_running", lambda: True)
    monkeypatch.setattr(wake_mod.shutil, "which", lambda n: "/usr/bin/codex")

    ran = []
    monkeypatch.setattr(wake_mod, "_run", lambda cmd: (ran.append(cmd), _Ok())[1])
    assert wake(MENTION, model="codex", session="blocks-arch") == (
        "delivered to codex session blocks-arch (declared target)"
    )
    assert ran[0][:5] == ["codex", "queue", "--thread", "blocks-arch", "--message"]


def test_codex_with_no_daemon_queues(monkeypatch):
    monkeypatch.setattr(wake_mod, "codex_daemon_running", lambda: False)
    outcome = wake(MENTION, model="codex", session="x")
    assert outcome.startswith("queued")
    assert "never starts an agent" in outcome


def test_codex_discovers_the_live_session_when_none_is_declared(monkeypatch):
    """A codex session id changes every session, so it is discovered, not declared.

    `thread/loaded/list` is the app-server's own answer to which sessions are
    live — authoritative, and self-healing when the session changes.
    """
    monkeypatch.setattr(wake_mod, "codex_daemon_running", lambda: True)
    monkeypatch.setattr(wake_mod.shutil, "which", lambda n: "/usr/bin/codex")
    monkeypatch.setattr(wake_mod, "codex_loaded_threads", lambda: ["01a0739b"])
    ran = []
    monkeypatch.setattr(wake_mod, "_run", lambda cmd: (ran.append(cmd), _Ok())[1])
    assert wake(MENTION, model="codex").endswith("(discovered from the app-server)")
    assert ran[0][3] == "01a0739b"


def test_codex_with_no_loaded_session_queues(monkeypatch):
    monkeypatch.setattr(wake_mod, "codex_daemon_running", lambda: True)
    monkeypatch.setattr(wake_mod.shutil, "which", lambda n: "/usr/bin/codex")
    monkeypatch.setattr(wake_mod, "codex_loaded_threads", lambda: [])
    outcome = wake(MENTION, model="codex")
    assert outcome.startswith("queued")
    assert "never starts an agent" in outcome


def test_codex_with_several_loaded_sessions_refuses(monkeypatch):
    """Same rule as Claude: two answers means no answer, so do not pick one."""
    monkeypatch.setattr(wake_mod, "codex_daemon_running", lambda: True)
    monkeypatch.setattr(wake_mod.shutil, "which", lambda n: "/usr/bin/codex")
    monkeypatch.setattr(wake_mod, "codex_loaded_threads", lambda: ["a", "b"])
    with pytest.raises(WakeError, match="no single answer"):
        wake(MENTION, model="codex")


def test_a_declared_codex_session_wins_over_discovery(monkeypatch):
    monkeypatch.setattr(wake_mod, "codex_daemon_running", lambda: True)
    monkeypatch.setattr(wake_mod.shutil, "which", lambda n: "/usr/bin/codex")

    def boom():
        raise AssertionError("a declared session must not trigger discovery")

    monkeypatch.setattr(wake_mod, "codex_loaded_threads", boom)
    monkeypatch.setattr(wake_mod, "_run", lambda cmd: _Ok())
    assert wake(MENTION, model="codex", session="pinned").endswith("(declared target)")


def test_codex_queue_failure_is_reported(monkeypatch):
    monkeypatch.setattr(wake_mod, "codex_daemon_running", lambda: True)
    monkeypatch.setattr(wake_mod.shutil, "which", lambda n: "/usr/bin/codex")

    class Bad:
        returncode = 1
        stderr = "no such thread"
        stdout = ""

    monkeypatch.setattr(wake_mod, "_run", lambda cmd: Bad())
    with pytest.raises(WakeError, match="codex queue failed"):
        wake(MENTION, model="codex", session="ghost")


# -- an unknown model: report, never fall back -------------------------------

def test_unknown_model_is_reported_not_scanned(monkeypatch):
    """`model` is a free string, so this will happen. Falling back would
    resurrect the guessing §7g removed, on a seat that stated its answer."""
    def boom(*a, **k):
        raise AssertionError("must not fall back to scanning")

    monkeypatch.setattr(wake_mod, "list_panes", boom)
    with pytest.raises(WakeError, match="no delivery adapter"):
        wake(MENTION, model="gemini")


# -- no model: the fallback runs, and says so ---------------------------------

def test_absent_model_falls_back_and_labels_the_path(monkeypatch):
    monkeypatch.setattr(wake_mod, "list_panes", lambda: _panes(("rc:0.0", "claude", 10)))
    monkeypatch.setattr(wake_mod, "_tmux", lambda *a: _Ok())
    outcome = wake(MENTION, model=None)
    assert "VIA PANE SCAN" in outcome
    assert "declare `model`" in outcome


def test_absent_model_with_nothing_running_says_to_declare(monkeypatch):
    monkeypatch.setattr(wake_mod, "list_panes", lambda: _panes(("rc:0.0", "bash", 10)))
    outcome = wake(MENTION, model=None)
    assert outcome.startswith("queued")
    assert "Declare `model`" in outcome


def test_discovery_failure_degrades_to_the_clear_refusal(monkeypatch):
    """Discovery is preferred, not depended on.

    If the app-server will not answer, the reader needs to know what to do —
    not a protocol error they cannot act on.
    """
    monkeypatch.setattr(wake_mod, "codex_daemon_running", lambda: True)
    monkeypatch.setattr(wake_mod.shutil, "which", lambda n: "/usr/bin/codex")

    def refuses():
        raise WakeError("broken pipe")

    monkeypatch.setattr(wake_mod, "codex_loaded_threads", refuses)
    with pytest.raises(WakeError, match="Declare model_session"):
        wake(MENTION, model="codex")
