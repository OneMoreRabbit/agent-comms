"""Consuming `seat status` — devagent-seat-contract 0.5, ADR-0011.

The seam: the seat answers "can this be spoken to?", we answer "was it heard?".
These tests pin our half — that we ask, that we believe the answer, and that we
never quietly substitute a guess for it.
"""

from __future__ import annotations

import json

import pytest

from agent_comms import seat as seat_mod
from agent_comms import wake as wake_mod
from agent_comms.seat import Awake, SeatStatus, SeatUnavailable
from agent_comms.wake import WakeError, wake

MENTION = {"id": 1, "sender": "arch", "topic": "t", "content": "go"}


def _reply(payload: dict, code: int = 0, stderr: str = ""):
    class R:
        returncode = code
        stdout = json.dumps(payload)
        stderr = ""
    R.stderr = stderr
    return R


# -- reading the verdict ------------------------------------------------------

def test_addressable_is_parsed(monkeypatch):
    monkeypatch.setattr(seat_mod, "seat_available", lambda: True)
    monkeypatch.setattr(seat_mod.subprocess, "run", lambda *a, **k: _reply({
        "verdict": "addressable", "reason": "", "runtime": "claude",
        "awake": True, "target": "rc:0.0", "pinned": "abc"}))
    st = seat_mod.status()
    assert st.addressable and st.deliverable
    assert st.runtime == "claude" and st.target == "rc:0.0"


@pytest.mark.parametrize("verdict", ["not-addressable", "broken", "undetermined"])
def test_every_non_addressable_verdict_holds(verdict, monkeypatch):
    """The contract is explicit that 30 is treated exactly as 10."""
    monkeypatch.setattr(seat_mod, "seat_available", lambda: True)
    monkeypatch.setattr(seat_mod.subprocess, "run", lambda *a, **k: _reply({
        "verdict": verdict, "reason": "because", "runtime": "claude",
        "target": "rc:0.0"}))
    st = seat_mod.status()
    assert not st.deliverable
    outcome = wake(MENTION, st)
    assert outcome.startswith("queued")
    assert "because" in outcome


def test_unreadable_output_is_undetermined_not_addressable(monkeypatch):
    """Anything we cannot read cleanly must not become a delivery."""
    class R:
        returncode = 0
        stdout = "not json at all"
        stderr = ""

    monkeypatch.setattr(seat_mod, "seat_available", lambda: True)
    monkeypatch.setattr(seat_mod.subprocess, "run", lambda *a, **k: R())
    st = seat_mod.status()
    assert st.verdict == "undetermined" and not st.deliverable


def test_a_seat_command_that_will_not_run_is_undetermined(monkeypatch):
    monkeypatch.setattr(seat_mod, "seat_available", lambda: True)

    def boom(*a, **k):
        raise OSError("no such file")

    monkeypatch.setattr(seat_mod.subprocess, "run", boom)
    assert seat_mod.status().verdict == "undetermined"


def test_no_seat_command_raises_rather_than_guessing(monkeypatch):
    """No legacy path. An un-updated seat holds visibly."""
    monkeypatch.setattr(seat_mod, "seat_available", lambda: False)
    with pytest.raises(SeatUnavailable, match="needs the agent-skeleton image"):
        seat_mod.status()


# -- awake: the operator's hold ruling ---------------------------------------

def test_asleep_holds_even_though_addressable():
    """Wakefulness comes from `seat awake`, its own command."""
    st = SeatStatus(verdict="addressable", runtime="claude", target="rc:0.0")
    outcome = wake(MENTION, st, Awake(state=False, reason="no live session"))
    assert outcome.startswith("queued")
    assert "asleep" in outcome and "no live session" in outcome


def test_cannot_tell_holds_like_a_no():
    """`seat awake` exit 2. Undetermined is never treated as fine."""
    st = SeatStatus(verdict="addressable", runtime="claude", target="rc:0.0")
    outcome = wake(MENTION, st, Awake(state=None, reason="could not query"))
    assert outcome.startswith("queued")
    assert "unknown wakefulness" in outcome


def test_awake_yes_delivers(monkeypatch):
    calls = []

    class Ok:
        returncode = 0
        stderr = ""
        stdout = ""

    monkeypatch.setattr(wake_mod, "_tmux", lambda *a: (calls.append(a), Ok())[1])
    monkeypatch.setattr(wake_mod.shutil, "which", lambda n: f"/usr/bin/{n}")
    st = SeatStatus(verdict="addressable", runtime="claude", target="rc:0.0")
    assert wake(MENTION, st, Awake(state=True)) == "delivered to rc:0.0 (claude)"


def test_wakefulness_is_not_read_off_status(monkeypatch):
    """`seat status`'s awake field is not consulted — that was the bug.

    On codex it is set by a branch that never reaches the app-server query, so a
    live thread read asleep and every codex seat looked undeliverable.
    """
    calls = []

    class Ok:
        returncode = 0
        stderr = ""
        stdout = ""

    monkeypatch.setattr(wake_mod, "_tmux", lambda *a: (calls.append(a), Ok())[1])
    monkeypatch.setattr(wake_mod.shutil, "which", lambda n: f"/usr/bin/{n}")
    # status says asleep; the dedicated command says awake. The command wins.
    st = SeatStatus(verdict="addressable", runtime="claude", target="rc:0.0", awake=False)
    assert wake(MENTION, st, Awake(state=True)).startswith("delivered")


# -- sending: one mechanism per runtime, chosen by the seat -------------------

def test_claude_is_typed_into_its_pane(monkeypatch):
    calls = []

    class Ok:
        returncode = 0
        stderr = ""
        stdout = ""

    monkeypatch.setattr(wake_mod, "_tmux", lambda *a: (calls.append(a), Ok())[1])
    monkeypatch.setattr(wake_mod.shutil, "which", lambda n: f"/usr/bin/{n}")
    st = SeatStatus(verdict="addressable", runtime="claude", target="rc:0.0", awake=True)
    assert wake(MENTION, st) == "delivered to rc:0.0 (claude)"
    assert [c[0] for c in calls] == ["send-keys", "send-keys"]
    assert calls[0][3] == "-l", "literal, so a message is not read as key names"
    assert calls[1][-1] == "Enter", "Enter is a separate call"


def test_codex_is_queued_to_its_thread_with_no_tmux(monkeypatch):
    ran = []

    class Ok:
        returncode = 0
        stderr = ""
        stdout = ""

    def no_tmux(*a):
        raise AssertionError("the codex path must not touch tmux")

    monkeypatch.setattr(wake_mod, "_tmux", no_tmux)
    monkeypatch.setattr(wake_mod, "_run", lambda cmd: (ran.append(cmd), Ok())[1])
    monkeypatch.setattr(wake_mod.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(wake_mod, "codex_landed", lambda t, m, wait=8.0: True)
    st = SeatStatus(verdict="addressable", runtime="codex",
                    target="01a0-thread", awake=True)
    assert wake(MENTION, st) == "delivered to 01a0-thread (codex)"
    assert ran[0][:4] == ["codex", "queue", "--thread", "01a0-thread"]


def test_an_unknown_runtime_is_refused_not_guessed():
    st = SeatStatus(verdict="addressable", runtime="gemini", target="x", awake=True)
    with pytest.raises(WakeError, match="no way to send to it"):
        wake(MENTION, st)


def test_addressable_with_no_target_is_refused():
    st = SeatStatus(verdict="addressable", runtime="claude", target="", awake=True)
    with pytest.raises(WakeError, match="gave no target"):
        wake(MENTION, st)


def test_enter_failing_is_a_failed_delivery(monkeypatch):
    def tmux(*args):
        class R:
            returncode = 0 if "-l" in args else 1
            stderr = "boom"
            stdout = ""

        return R()

    monkeypatch.setattr(wake_mod, "_tmux", tmux)
    monkeypatch.setattr(wake_mod.shutil, "which", lambda n: f"/usr/bin/{n}")
    st = SeatStatus(verdict="addressable", runtime="claude", target="rc:0.0", awake=True)
    with pytest.raises(WakeError, match="Enter did not"):
        wake(MENTION, st)


# -- the seam holds -----------------------------------------------------------

def test_we_never_look_at_panes_ourselves():
    """The deletion is the point: no pane scan, no process tree, no heuristic."""
    for gone in ("list_panes", "find_runtime_panes", "find_agent_panes",
                 "pane_blocked_reason", "deliver_by_scan", "codex_lock_threads",
                 "codex_live_threads", "codex_daemon_running", "_descendants"):
        assert not hasattr(wake_mod, gone), f"{gone} should have been deleted, not demoted"


def test_there_is_no_session_module():
    """`seat start`/`seat status` own the session; we no longer have a copy."""
    with pytest.raises(ImportError):
        __import__("agent_comms.session")


# -- delivered means landed ---------------------------------------------------

def test_codex_success_requires_the_message_to_reach_the_record(monkeypatch):
    """`codex queue` exits 0 for a message that strands. Twice observed live."""
    class Ok:
        returncode = 0
        stderr = ""
        stdout = ""

    monkeypatch.setattr(wake_mod, "_run", lambda cmd: Ok())
    monkeypatch.setattr(wake_mod.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(wake_mod, "codex_landed", lambda t, m, wait=8.0: False)
    st = SeatStatus(verdict="addressable", runtime="codex", target="cold", awake=True)
    with pytest.raises(WakeError, match="stranded"):
        wake(MENTION, st)


def test_codex_success_when_it_does_reach_the_record(monkeypatch):
    class Ok:
        returncode = 0
        stderr = ""
        stdout = ""

    monkeypatch.setattr(wake_mod, "_run", lambda cmd: Ok())
    monkeypatch.setattr(wake_mod.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(wake_mod, "codex_landed", lambda t, m, wait=8.0: True)
    st = SeatStatus(verdict="addressable", runtime="codex", target="warm", awake=True)
    assert wake(MENTION, st) == "delivered to warm (codex)"


def test_landing_check_reads_the_thread_record(tmp_path, monkeypatch):
    """Against a real file layout, so the glob is not assumed."""
    home = tmp_path / "codex"
    day = home / "sessions" / "2026" / "09" / "09"
    day.mkdir(parents=True)
    (day / "rollout-2026-09-09T00-00-00-thread-xyz.jsonl").write_text(
        '{"payload":{"content":"hello LANDED-MARKER there"}}\n', encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_HOME", str(home))
    assert wake_mod.codex_landed("thread-xyz", "LANDED-MARKER", wait=0) is True
    assert wake_mod.codex_landed("thread-xyz", "ABSENT-MARKER", wait=0) is False
    assert wake_mod.codex_landed("no-such-thread", "x", wait=0) is False


def test_seat_awake_is_parsed(monkeypatch):
    monkeypatch.setattr(seat_mod, "seat_available", lambda: True)
    monkeypatch.setattr(seat_mod.subprocess, "run", lambda *a, **k: _reply({
        "awake": True, "runtime": "codex",
        "reason": "the app-server reports thread 01a0 loaded"}))
    aw = seat_mod.awake()
    assert aw.state is True and not aw.holds


def test_seat_awake_unreadable_is_cannot_tell(monkeypatch):
    class R:
        returncode = 2
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(seat_mod, "seat_available", lambda: True)
    monkeypatch.setattr(seat_mod.subprocess, "run", lambda *a, **k: R())
    aw = seat_mod.awake()
    assert aw.state is None and aw.holds, "cannot tell must hold, like undetermined"


# -- persistence: what "queued" means to whoever is waiting -------------------

def test_persistence_is_read_from_the_seats_declaration(tmp_path, monkeypatch):
    seat_dir = tmp_path / ".seat"
    seat_dir.mkdir()
    (seat_dir / "session.yml").write_text(
        'runtime: claude\n'
        'session:\n  kind: tmux\n  target: "rc:0.0"\n'
        'persistence:\n'
        '  survives: [ssh-disconnect, container-restart]\n'
        '  lost_on: [host-reboot]\n',
        encoding="utf-8",
    )
    p = seat_mod.persistence(str(seat_dir / "session.yml"))
    assert p.survives == ("ssh-disconnect", "container-restart")
    assert p.lost_on == ("host-reboot",)
    assert "survives ssh-disconnect" in p.summary()


def test_a_seat_that_does_not_declare_persistence_says_less_not_wrongly(tmp_path):
    assert seat_mod.persistence(str(tmp_path / "absent.yml")).summary() == ""


def test_a_hold_tells_the_sender_how_long_it_might_wait():
    """We asked for this in ADR-0011 step 0 and then did not consume it."""
    from agent_comms.seat import Persistence

    st = SeatStatus(verdict="not-addressable", reason="no live session", runtime="claude")
    outcome = wake(MENTION, st, None,
                   Persistence(survives=("container-restart",), lost_on=("host-reboot",)))
    assert "survives container-restart" in outcome
    assert "lost on host-reboot" in outcome


# -- which build answered: seat --version, contractual from 0.3.3 -------------

def _fake_seat(monkeypatch, stdout: str, code: int = 0):
    import subprocess as sp

    class R:
        returncode, stderr = code, ""
    R.stdout = stdout
    monkeypatch.setattr(seat_mod, "seat_available", lambda: True)
    monkeypatch.setattr(sp, "run", lambda *a, **k: R)
    monkeypatch.setattr(seat_mod.subprocess, "run", lambda *a, **k: R)


def test_version_reads_the_contracted_json(monkeypatch):
    _fake_seat(monkeypatch, '{"seat":"0.3.3","contract":"0.3.3"}\n')
    v = seat_mod.version()
    assert (v.seat, v.contract) == ("0.3.3", "0.3.3")
    assert not v.below()


def test_version_reads_an_0_3_0_seat_that_prints_prose_first(monkeypatch):
    """0.3.0 printed the two prose lines, then the object.

    Both shapes are on real seats during a rollout, so the reader scans for the
    object rather than parsing the whole stream. Measured on this seat_mod.
    """
    _fake_seat(monkeypatch,
               "seat 0.3.0\ncontract devagent-seat-contract 0.5 (ADR-0011)\n"
               '{"seat":"0.3.0","contract":"0.5"}\n')
    v = seat_mod.version()
    assert v.seat == "0.3.0"
    assert v.below(), "older than the build this client is written against"
    assert "0.3.3" in v.summary()


def test_version_falls_back_to_the_plain_form(monkeypatch):
    _fake_seat(monkeypatch, "seat 0.3.4\ncontract devagent-seat-contract 0.3.4 (ADR-0011)\n")
    assert seat_mod.version().seat == "0.3.4"


def test_version_sorts_numerically_not_lexically(monkeypatch):
    """0.3.10 is newer than 0.3.9 — the trap that nearly numbered our own release."""
    _fake_seat(monkeypatch, '{"seat":"0.3.10","contract":"0.3.10"}\n')
    assert not seat_mod.version().below("0.3.9")


def test_an_unreadable_version_is_unknown_not_old(monkeypatch):
    _fake_seat(monkeypatch, "who knows\n")
    v = seat_mod.version()
    assert not v.known and not v.below()
    assert "unknown" in v.summary()
