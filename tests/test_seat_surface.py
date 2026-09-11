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
from agent_comms.seat import SeatStatus, SeatUnavailable
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
#
# Read from `seat status --json`'s `awake` field. It was a separate `seat awake`
# call until 2026-09-11: the status field had been set by a codex branch that
# never reached the app-server, so a live thread read asleep and every codex seat
# looked undeliverable. agent-skeleton merged the two onto one shared check, and
# a second call asking the same check the same question is work for nothing.


def test_asleep_holds_even_though_addressable():
    st = SeatStatus(verdict="addressable", runtime="claude", target="rc:0.0",
                    awake=False, reason="no live session")
    outcome = wake(MENTION, st)
    assert outcome.startswith("queued")
    assert "asleep" in outcome and "no live session" in outcome


def test_cannot_tell_holds_like_a_no():
    """`awake: null` — the runtime could not be asked. Never treated as fine."""
    st = SeatStatus(verdict="addressable", runtime="claude", target="rc:0.0",
                    awake=None, reason="could not query")
    outcome = wake(MENTION, st)
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
    st = SeatStatus(verdict="addressable", runtime="claude", target="rc:0.0", awake=True)
    assert wake(MENTION, st) == "delivered to rc:0.0 (claude)"


def test_only_a_clear_yes_is_attending():
    addressable = dict(verdict="addressable", runtime="claude", target="rc:0.0")
    assert SeatStatus(**addressable, awake=True).deliverable
    assert not SeatStatus(**addressable, awake=False).deliverable
    assert not SeatStatus(**addressable, awake=None).deliverable, (
        "the seat could not ask the runtime; that is not a yes"
    )


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

def test_awake_is_parsed_off_status(monkeypatch):
    """One call, both answers."""
    monkeypatch.setattr(seat_mod, "seat_available", lambda: True)
    monkeypatch.setattr(seat_mod.subprocess, "run", lambda *a, **k: _reply({
        "verdict": "addressable", "awake": True, "runtime": "codex",
        "target": "01a0", "reason": "the app-server reports thread 01a0 loaded"}))
    st = seat_mod.status()
    assert st.attending and st.deliverable


def test_a_missing_awake_field_is_not_a_yes(monkeypatch):
    """An older seat that reports no `awake` holds rather than delivering."""
    monkeypatch.setattr(seat_mod, "seat_available", lambda: True)
    monkeypatch.setattr(seat_mod.subprocess, "run", lambda *a, **k: _reply({
        "verdict": "addressable", "runtime": "claude", "target": "rc:0.0"}))
    st = seat_mod.status()
    assert st.awake is None and not st.deliverable


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
    outcome = wake(MENTION, st,
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
    _fake_seat(monkeypatch, '{"seat":"0.5.1","contract":"0.5.1"}\n')
    v = seat_mod.version()
    assert (v.seat, v.contract) == ("0.5.1", "0.5.1")
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
    assert "0.5.1" in v.summary()


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


# -- the exit code is the verdict, not the string -----------------------------
#
# `devagent-seat-contract` 0.5.1: "branch on the exit code, not this string."
# Reasons are prose and may be reworded; verdict strings are a convenience.


def _status_reply(monkeypatch, payload, code=0):
    class R:
        returncode = code
        stderr = ""
    R.stdout = json.dumps(payload)
    monkeypatch.setattr(seat_mod, "seat_available", lambda: True)
    monkeypatch.setattr(seat_mod.subprocess, "run", lambda *a, **k: R)


def test_the_exit_code_decides_the_verdict(monkeypatch):
    _status_reply(monkeypatch, {"verdict": "addressable", "runtime": "codex"}, code=10)
    st = seat_mod.status()
    assert st.verdict == "not-addressable" and not st.addressable


def test_a_disagreement_is_reported_not_silently_resolved(monkeypatch):
    """Two sources for one fact is how this client got into trouble before."""
    _status_reply(monkeypatch, {"verdict": "addressable", "reason": "fine"}, code=10)
    assert "but its JSON says 'addressable'" in seat_mod.status().reason


def test_an_unknown_exit_code_is_undetermined(monkeypatch):
    _status_reply(monkeypatch, {"verdict": "addressable"}, code=7)
    assert seat_mod.status().verdict == "undetermined"


# -- waiting: delivered, with nothing running to read it ----------------------


def test_a_pinned_codex_thread_with_no_session_is_waiting(monkeypatch):
    """Addressable either way — the session count is what tells them apart.

    Codex has its own queue, so a pinned thread takes a message whether or not
    anything is loaded (agent-skeleton measured it with the app-server stopped).
    The exit code cannot distinguish; `sessions` can, and it is a declared field
    rather than prose.
    """
    _status_reply(monkeypatch, {
        "verdict": "addressable", "runtime": "codex", "awake": True,
        "target": "01a0", "sessions": {"claude": 0, "codex": 0}})
    st = seat_mod.status()
    assert st.deliverable and st.waiting


def test_a_running_session_is_not_waiting(monkeypatch):
    _status_reply(monkeypatch, {
        "verdict": "addressable", "runtime": "codex", "awake": True,
        "target": "01a0", "sessions": {"claude": 0, "codex": 1}})
    assert not seat_mod.status().waiting


def test_an_unknown_session_count_is_not_waiting(monkeypatch):
    """`null` is "could not be asked", which is not 0. We do not claim either way."""
    _status_reply(monkeypatch, {
        "verdict": "addressable", "runtime": "codex", "awake": True,
        "target": "01a0", "sessions": {"codex": None}})
    st = seat_mod.status()
    assert st.session_running is None and not st.waiting


def test_the_landing_check_is_gone():
    """Dropped 2026-09-11. It read absence from the thread record as loss, when
    it meant waiting — and it was asymmetric: claude has no equivalent, so
    `delivered` meant two different things depending on the runtime."""
    for gone in ("codex_landed", "_thread_record"):
        assert not hasattr(wake_mod, gone), f"{gone} should be deleted, not demoted"
