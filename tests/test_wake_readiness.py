"""A running agent is not necessarily an agent ready for a turn.

Found live on 2026-09-05: a freshly started Claude session reported
`pane_current_command=claude` while sitting on a "Try the new fullscreen
renderer?" menu. The command guard says "agent"; the session would have eaten
the message as menu navigation. ADR-0009 §7f names the trust prompt as this
trap — it turns out not to be the only one.
"""

from __future__ import annotations

import pytest

from agent_comms import wake as wake_mod
from agent_comms.wake import Pane, WakeError, pane_blocked_reason, wake


class _Captured:
    def __init__(self, text, returncode=0):
        self.stdout = text
        self.returncode = returncode
        self.stderr = ""


def _agent_pane():
    return [Pane(target="work:0.0", command="claude", path="/home/dev/work")]


def test_a_selection_prompt_blocks_delivery(monkeypatch):
    """The exact dialog observed on a live seat."""
    pane = "  1. Yes, try it\n    2. Not now\n\n  Enter to confirm - Esc to cancel"
    monkeypatch.setattr(wake_mod, "_tmux", lambda *a: _Captured(pane))
    reason = pane_blocked_reason("work:0.0")
    assert reason is not None
    assert "consumed as menu input" in reason


def test_the_trust_prompt_blocks_delivery(monkeypatch):
    """The one ADR-0009 §7f names."""
    monkeypatch.setattr(
        wake_mod, "_tmux",
        lambda *a: _Captured("Do you trust the files in this folder?"),
    )
    assert pane_blocked_reason("work:0.0") is not None


def test_an_ordinary_prompt_is_not_blocked(monkeypatch):
    monkeypatch.setattr(
        wake_mod, "_tmux",
        lambda *a: _Captured("auto mode on (shift+tab to cycle)\n> "),
    )
    assert pane_blocked_reason("work:0.0") is None


def test_an_unreadable_pane_is_treated_as_not_ready(monkeypatch):
    """Cannot confirm is a refusal, not a hopeful send (constitution §9)."""
    monkeypatch.setattr(wake_mod, "_tmux", lambda *a: _Captured("", returncode=1))
    reason = pane_blocked_reason("work:0.0")
    assert reason is not None
    assert "could not read the pane" in reason


def test_wake_refuses_a_blocked_session_rather_than_typing_into_it(monkeypatch):
    """A swallowed message looks like a seat that read it and ignored it."""
    monkeypatch.setattr(wake_mod, "list_panes", _agent_pane)
    monkeypatch.setattr(
        wake_mod, "_tmux",
        lambda *a: _Captured("Enter to confirm - Esc to cancel"),
    )
    with pytest.raises(WakeError, match="not ready for a turn"):
        wake({"id": 1, "sender": "arch", "topic": "t", "content": "go"})


def test_wake_delivers_when_the_session_is_ready(monkeypatch):
    calls = []

    def fake_tmux(*args):
        calls.append(args)
        if args[0] == "capture-pane":
            return _Captured("> ")
        return _Captured("")

    monkeypatch.setattr(wake_mod, "list_panes", _agent_pane)
    monkeypatch.setattr(wake_mod, "_tmux", fake_tmux)
    outcome = wake({"id": 2, "sender": "arch", "topic": "t", "content": "go"})
    assert outcome.startswith("delivered")
    assert [c[0] for c in calls] == ["capture-pane", "send-keys", "send-keys"]
