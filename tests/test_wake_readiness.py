"""A running agent is not necessarily an agent ready for a turn.

Found live on 2026-09-05: a freshly started Claude session reported
`pane_current_command=claude` while sitting on a "Try the new fullscreen
renderer?" menu. The command guard says "agent"; the session would have eaten
the message as menu navigation. ADR-0009 §7f names the trust prompt as this
trap — it turns out not to be the only one.
"""

from __future__ import annotations

#: A pane holding a live, idle agent prompt.
IDLE_PANE = "\u23f5\u23f5 auto mode on (shift+tab to cycle)\n> "

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
        lambda *a: _Captured(IDLE_PANE),
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
            return _Captured(IDLE_PANE)
        return _Captured("")

    monkeypatch.setattr(wake_mod, "list_panes", _agent_pane)
    monkeypatch.setattr(wake_mod, "_tmux", fake_tmux)
    outcome = wake({"id": 2, "sender": "arch", "topic": "t", "content": "go"})
    assert outcome.startswith("delivered")
    assert [c[0] for c in calls] == ["capture-pane", "send-keys", "send-keys"]


# -- the remote-control pane: the defect that cost two afternoons -------------

RC_PANE = (
    "[03:05:52] Reconnected after 2m 0s\n"
    "[06:24:00] Reconnected after 3s\n"
    "[13:12:07] Reconnected after 2s\n"
)


def test_a_remote_control_pane_is_refused_not_delivered_into(monkeypatch):
    """Observed on this seat: rc:0.0 shows only reconnect lines and no prompt.

    `pane_current_command` says bash with claude in the tree, so the search
    accepts it; keystrokes then reach no prompt. This client reported
    `delivered` sixteen times into panes like this, and an arch seat told the
    operator its seats were unreachable on the strength of that line.
    """
    monkeypatch.setattr(wake_mod, "_tmux", lambda *a: _Captured(RC_PANE))
    reason = pane_blocked_reason("rc:0.0")
    assert reason is not None
    assert "remote-control client" in reason
    assert "pinned conversation" in reason


def test_a_pane_with_no_prompt_at_all_is_refused(monkeypatch):
    """Positive confirmation, not absence of a known blocker. A pane nobody has
    seen before is not assumed typeable."""
    monkeypatch.setattr(wake_mod, "_tmux", lambda *a: _Captured("some unknown output\n"))
    reason = pane_blocked_reason("work:0.0")
    assert reason is not None
    assert "no agent prompt is visible" in reason


def test_a_mid_turn_agent_is_still_a_live_session(monkeypatch):
    """An agent that is busy is reachable — codex queues a mid-turn line and
    Claude takes it as the next turn. Refusing here would be a false negative."""
    monkeypatch.setattr(
        wake_mod, "_tmux",
        lambda *a: _Captured("Transfiguring... (21s - esc to interrupt)"),
    )
    assert pane_blocked_reason("work:0.0") is None


def test_wake_reports_queued_rather_than_delivered_for_an_rc_pane(monkeypatch):
    """The whole point: `delivered` must mean landed."""
    monkeypatch.setattr(wake_mod, "list_panes", _agent_pane)
    monkeypatch.setattr(wake_mod, "_tmux", lambda *a: _Captured(RC_PANE))
    with pytest.raises(WakeError, match="remote-control client"):
        wake({"id": 1, "sender": "arch", "topic": "t", "content": "go"}, model="claude")
