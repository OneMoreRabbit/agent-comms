"""The wake mechanism — ADR-0009 §7b–§7f.

The rules under test are not implementation details; they are the difference
between coordination and a message typed at a shell prompt nobody reads.
"""

from __future__ import annotations

import pytest

from agent_comms import operations
from agent_comms import wake as wake_mod
from agent_comms.wake import Pane, WakeError, compose_turn, find_agent_panes, wake


def _panes(*specs):
    return [Pane(target=t, command=c, path="/home/dev/work") for t, c in specs]


class _Ok:
    """A tmux call that worked. `stdout` is the pane capture: a ready prompt."""

    returncode = 0
    stderr = ""
    stdout = "> "


# -- the guard: never talk to a shell ----------------------------------------

def test_a_shell_is_not_an_agent():
    """Sending to a bash pane types the message at a prompt nobody reads."""
    assert find_agent_panes(_panes(("rc:0.0", "bash")), ("claude", "codex")) == []


def test_the_daemons_own_pane_is_not_an_agent():
    """Measured on a live seat: the comms daemon's pane reports `python`."""
    assert find_agent_panes(_panes(("comms:0.0", "python")), ("claude", "codex")) == []


def test_a_claude_pane_is_an_agent():
    found = find_agent_panes(_panes(("work:0.0", "claude")), ("claude", "codex"))
    assert [p.target for p in found] == ["work:0.0"]


def test_codex_counts_too():
    found = find_agent_panes(_panes(("work:0.0", "codex")), ("claude", "codex"))
    assert [p.target for p in found] == ["work:0.0"]


# -- no agent: queue, never start (§7e) --------------------------------------

def test_no_agent_queues_and_never_starts_one(monkeypatch):
    """§7e: a message never starts an agent. That is what closes the overnight case."""
    monkeypatch.setattr(wake_mod, "list_panes", lambda: _panes(("rc:0.0", "bash")))
    touched = []
    monkeypatch.setattr(wake_mod, "_tmux", lambda *a: touched.append(a))
    outcome = wake({"id": 1, "sender": "arch", "topic": "t", "content": "go"})
    assert outcome.startswith("queued")
    assert touched == [], "nothing may be spawned or sent when no agent is running"


def test_no_tmux_at_all_is_a_reported_failure(monkeypatch):
    monkeypatch.setattr(wake_mod, "tmux_available", lambda: False)
    with pytest.raises(WakeError, match="tmux is not installed"):
        wake({"id": 1})


# -- two agents: refuse rather than guess (§7d, constitution §9) -------------

def test_two_agent_panes_refuse_rather_than_guess(monkeypatch):
    monkeypatch.setattr(
        wake_mod, "list_panes",
        lambda: _panes(("a:0.0", "claude"), ("b:0.0", "codex")),
    )
    with pytest.raises(WakeError, match="never two agents"):
        wake({"id": 1, "sender": "arch", "topic": "t", "content": "go"})


# -- delivery mechanics (§7f) -------------------------------------------------

def test_text_and_enter_are_separate_calls(monkeypatch):
    """Verified on a live seat: a trailing Enter in the same call was unreliable."""
    calls = []

    def fake_tmux(*args):
        calls.append(args)
        return _Ok()

    monkeypatch.setattr(wake_mod, "list_panes", lambda: _panes(("work:0.0", "claude")))
    monkeypatch.setattr(wake_mod, "_tmux", fake_tmux)
    wake({"id": 7, "sender": "arch", "topic": "t", "content": "proceed"})

    sends = [c for c in calls if c[0] == "send-keys"]
    assert len(sends) == 2
    assert sends[0][:3] == ("send-keys", "-t", "work:0.0")
    assert sends[0][3] == "-l"
    assert sends[1] == ("send-keys", "-t", "work:0.0", "Enter")


def test_payload_is_sent_literally(monkeypatch):
    """A message is untrusted text arriving at a terminal; -l stops interpretation."""
    calls = []

    def fake_tmux(*args):
        calls.append(args)
        return _Ok()

    monkeypatch.setattr(wake_mod, "list_panes", lambda: _panes(("work:0.0", "claude")))
    monkeypatch.setattr(wake_mod, "_tmux", fake_tmux)
    wake({"id": 8, "sender": "arch", "topic": "t", "content": "press C-c to stop"})
    literal = next(c for c in calls if c[0] == "send-keys" and "-l" in c)
    assert "C-c" in literal[-1], "the text must travel as text, not as key names"


def test_enter_failing_is_a_failure_not_a_success(monkeypatch):
    """Text delivered without Enter sits unsent in the agent's input box."""

    def fake_tmux(*args):
        class R:
            # capture-pane and the literal send succeed; only Enter fails
            returncode = 0 if args[0] == "capture-pane" or "-l" in args else 1
            stderr = "boom"
            stdout = "> "

        return R()

    monkeypatch.setattr(wake_mod, "list_panes", lambda: _panes(("work:0.0", "claude")))
    monkeypatch.setattr(wake_mod, "_tmux", fake_tmux)
    with pytest.raises(WakeError, match="Enter did not"):
        wake({"id": 9, "sender": "arch", "topic": "t", "content": "x"})


# -- the injected turn --------------------------------------------------------

def test_turn_names_the_sender_first():
    """§1a is only actionable if the agent knows who is asking."""
    line = compose_turn({"id": 3, "sender": "blocks-arch", "topic": "seat: fix",
                         "content": "please fix", "permalink": "https://h/#narrow/1"})
    assert line.startswith("[hub message from blocks-arch")
    assert "https://h/#narrow/1" in line
    assert "comms reply 3" in line


def test_turn_is_exactly_one_line():
    """send-keys is a keyboard: a newline is an Enter, and a second turn."""
    line = compose_turn({"id": 4, "sender": "a", "topic": "t",
                         "content": "line one\nline two\n\nline three"})
    assert "\n" not in line
    assert "line two" in line


def test_long_messages_are_pointed_at_not_pasted():
    line = compose_turn({"id": 5, "sender": "a", "topic": "t", "content": "x" * 5000})
    assert len(line) < 1500
    assert "comms show 5" in line


# -- integration with the store ----------------------------------------------

def test_a_sleeping_seat_says_so_once(seat, monkeypatch):
    """Told once, so a sender is not left guessing; not per message, which is noise."""
    monkeypatch.setattr(wake_mod, "list_panes", lambda: _panes(("rc:0.0", "bash")))
    sent = []

    class T:
        def call_endpoint(self, url, method="GET", request=None):
            sent.append(request)
            return {"result": "success", "id": 1}

    for i in (1, 2, 3):
        operations.wake_agent(
            {"id": i, "sender": "arch", "topic": "t", "content": "go",
             "channel": "agent-eco"},
            lambda c: T(),
        )
    assert len(sent) == 1, "a sleeping seat must not repeat itself for every message"
    assert "queued" in sent[0]["content"]
