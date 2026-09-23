"""Consuming `devagent-seat-contract` 1.0 — the seat delivers, we decide what next.

The seam is one call: `seat msg` takes the body on stdin and answers. These tests
pin OUR half of it — that we call it correctly, that we believe the answer, that
we never pre-check, and that the retry decision the operator gave us is made on
the seat's status rather than guessed.

Replaces test_seat_surface.py and test_dormant_seat.py, which pinned the 0.5x
surface (four verdicts, per-runtime dispatch, a pinned conversation). Deleted
rather than adapted.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from agent_comms import seat as seat_app
from agent_comms.seat import Delivery, SeatUnavailable
from agent_comms.wake import WakeError, compose_turn, wake


def fake_seat(monkeypatch, payload: dict, code: int = 0, stderr: bytes = b"",
              capture=None, contract: str = "1.0"):
    """Stand in for the `seat` command, recording how it was called.

    Answers `seat --version --json` too, because delivery gates on the contract
    major before it sends anything — a pre-1.0 seat has no `seat msg` at all.
    """
    seat_app._contract_checked = None

    def run(cmd, input=None, capture_output=True, timeout=None):
        if "--version" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"seat": "1.0.2", "contract": contract}).encode(), b"")
        if capture is not None:
            capture["cmd"] = cmd
            capture["input"] = input
        out = json.dumps(payload).encode() if payload is not None else b""
        return subprocess.CompletedProcess(cmd, code, out, stderr)
    monkeypatch.setattr(seat_app.subprocess, "run", run)


# -- how we call it ----------------------------------------------------------

def test_the_body_goes_on_stdin_never_as_an_argument(monkeypatch):
    """The amendment this client asked for during the 1.0 review, and the reason:
    sender-authored text survives a pipe and not a command line. The seat refuses
    an argument with exit 2, so passing one would be a self-inflicted failure."""
    seen = {}
    fake_seat(monkeypatch, {"success": True, "status": "delivered"}, capture=seen)
    body = "backticks `x` and $(whoami) and 'quotes' and\nnewlines"
    seat_app.deliver(body)

    assert seen["cmd"] == ["seat", "msg", "--json"]
    assert seen["input"] == body.encode("utf-8")
    assert not any(body in str(part) for part in seen["cmd"]), "body must not reach argv"


def test_delivery_never_pre_checks_with_status(monkeypatch):
    """Contract §3: two truths that can disagree in the gap between them, and the
    gap is where a message is lost. This client asked status-then-acted until 1.0."""
    calls = []

    seat_app._contract_checked = "1.0"  # already established; not the subject here

    def run(cmd, input=None, capture_output=True, timeout=None):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, json.dumps({"success": True, "status": "delivered"}).encode(), b"")

    monkeypatch.setattr(seat_app.subprocess, "run", run)
    wake({"id": 1, "sender": "arch", "topic": "t", "content": "hi"})

    assert calls == [["seat", "msg", "--json"]], f"exactly one call, got {calls}"
    assert not any("status" in c for c in calls), "must not ask status before delivering"


# -- believing the answer ----------------------------------------------------

@pytest.mark.parametrize("status,success,expect_retry", [
    ("delivered", True, False),
    ("queued", True, False),
    ("no-session", False, True),
    ("unknown", False, True),
    ("broken", False, False),
])
def test_each_status_gets_its_own_treatment(monkeypatch, status, success, expect_retry):
    fake_seat(monkeypatch, {"success": success, "status": status, "message": "m"},
              code=0 if success else 10)
    result = seat_app.deliver("body")
    assert result.status == status
    assert result.success is success
    assert result.retryable is expect_retry


def test_queued_is_a_success_not_a_degraded_delivery(monkeypatch):
    """The codex case: a thread that is not loaded takes the message and reads it
    when it next runs. Treating it as failure would retry a message already sent."""
    fake_seat(monkeypatch, {"success": True, "status": "queued",
                            "message": "codex took it", "runtime": "codex"}, code=0)
    result = seat_app.deliver("body")
    assert result.success and not result.retryable


def test_broken_is_never_retried(monkeypatch):
    """Not exactly one session. No retry changes that, and spinning buries the
    reason a person needs to read."""
    fake_seat(monkeypatch, {"success": False, "status": "broken",
                            "message": "2 sessions running"}, code=20)
    result = seat_app.deliver("body")
    assert result.needs_a_person and not result.retryable


def test_a_usage_error_is_ours_and_is_never_retried(monkeypatch):
    """exit 2 means we called the seat wrongly. Repeating a malformed call is how
    a bug becomes a flood."""
    fake_seat(monkeypatch, {"success": False, "status": "failed",
                            "message": "body given as an argument"}, code=2)
    result = seat_app.deliver("body")
    assert not result.retryable


def test_a_failed_attempt_at_exit_10_is_retried(monkeypatch):
    """Same word, different code, different decision — which is why the exit code
    is load-bearing for us and not only a report for a human."""
    fake_seat(monkeypatch, {"success": False, "status": "failed",
                            "message": "send-keys failed"}, code=10)
    assert seat_app.deliver("body").retryable


# -- the limits the contract states ------------------------------------------

def test_an_oversized_body_fails_and_is_never_truncated(monkeypatch):
    """65536 bytes. A shortened message reporting success is worse than one that
    did not arrive."""
    called = {"n": 0}
    seat_app._contract_checked = "1.0"

    def run(cmd, **kw):
        called["n"] += 1
        return subprocess.CompletedProcess(cmd, 0, b"{}", b"")

    monkeypatch.setattr(seat_app.subprocess, "run", run)
    result = seat_app.deliver("x" * (seat_app.MAX_BODY_BYTES + 1))
    assert not result.success and not result.retryable
    assert str(seat_app.MAX_BODY_BYTES) in result.message
    assert called["n"] == 0, "must not send a body the seat will refuse"


def test_unreadable_output_is_reported_as_a_seat_defect(monkeypatch):
    """The contract promises valid JSON on every path. If it is not, say so rather
    than inventing a verdict."""
    seat_app._contract_checked = "1.0"

    def run(cmd, input=None, capture_output=True, timeout=None):
        return subprocess.CompletedProcess(cmd, 10, b"not json at all", b"boom")
    monkeypatch.setattr(seat_app.subprocess, "run", run)
    result = seat_app.deliver("body")
    assert result.status == "unknown"
    assert "seat defect" in result.message


def test_a_missing_seat_command_is_not_a_delivery_answer(monkeypatch):
    """A seat that answers `broken` is working. A seat we cannot invoke is a
    different fault with a different remedy, so it is an exception, not a status."""
    seat_app._contract_checked = None

    def run(cmd, **kw):
        raise FileNotFoundError("seat")
    monkeypatch.setattr(seat_app.subprocess, "run", run)
    with pytest.raises(SeatUnavailable):
        seat_app.deliver("body")
    with pytest.raises(WakeError):
        wake({"id": 1, "content": "hi"})


# -- what we hand over -------------------------------------------------------

def test_the_turn_names_the_sender_first(monkeypatch):
    """ADR-0009 §1a is only actionable if the agent knows who is asking."""
    turn = compose_turn({"id": 9, "sender": "agent-eco-arch", "topic": "t",
                         "content": "do the thing", "permalink": "http://x/9"})
    assert turn.startswith("[hub message from agent-eco-arch")
    assert "comms reply 9" in turn


# -- the queue is ours, and so is working it ---------------------------------

def test_a_held_message_stays_queued_and_is_retried(seat, monkeypatch):
    """The operator's ruling: the seat is stateless about delivery, so an
    undelivered message remains ours. If nothing worked the queue, 'stored and
    will be retried' would be a claim nothing honoured."""
    from agent_comms import operations
    from agent_comms.store import Store
    from tests.conftest import FakeTransport

    store = Store(seat / ".comms")
    transport = FakeTransport(event_batches=[{"result": "success", "events": [
        {"id": 1, "type": "message", "flags": ["mentioned"], "message": {
            "id": 500, "sender_full_name": "agent-eco-arch", "display_recipient": "agent-eco",
            "subject": "agent-comms: q", "content": "?", "timestamp": 1, "stream_id": 7}},
    ]}])

    # First the seat has nothing running, so it holds.
    fake_seat(monkeypatch, {"success": False, "status": "no-session",
                            "message": "no session is running"}, code=10)
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1)
    assert [m.id for m in store.undelivered()] == [500]

    # Then a session comes up and the retry lands it.
    fake_seat(monkeypatch, {"success": True, "status": "delivered",
                            "message": "typed into the claude session"}, code=0)
    landed = operations.retry_undelivered(transport_factory=lambda c: transport)
    assert landed == 1
    assert store.undelivered() == []


def test_retry_stops_at_the_first_message_that_will_not_land(seat, monkeypatch):
    """Oldest first, and stop on the first refusal: a conversation delivered out
    of order is worse than one delivered late, and the seat that refused one will
    refuse the next."""
    from agent_comms import operations
    from agent_comms.store import Store
    from agent_comms.store import Mention

    store = Store(seat / ".comms")
    for mid in (10, 11, 12):
        store.append(Mention(id=mid, sender="agent-eco-arch", channel="agent-eco",
                             topic="t", content="x", timestamp=1, permalink="",
                             reason="mentioned"))
    fake_seat(monkeypatch, {"success": False, "status": "no-session", "message": "none"},
              code=10)
    assert operations.retry_undelivered() == 0
    assert len(store.undelivered()) == 3


def test_a_broken_seat_is_not_hammered(seat, monkeypatch):
    """`broken` needs a person. Retrying it would spin against a state no retry
    can change, and bury the reason in the log."""
    from agent_comms import operations
    from agent_comms.store import Mention, Store

    store = Store(seat / ".comms")
    store.append(Mention(id=20, sender="agent-eco-arch", channel="agent-eco", topic="t",
                         content="x", timestamp=1, permalink="", reason="mentioned"))
    calls = {"n": 0}

    seat_app._contract_checked = "1.0"

    def run(cmd, input=None, capture_output=True, timeout=None):
        calls["n"] += 1
        return subprocess.CompletedProcess(
            cmd, 20, json.dumps({"success": False, "status": "broken",
                                 "message": "2 sessions"}).encode(), b"")

    monkeypatch.setattr(seat_app.subprocess, "run", run)
    operations.retry_undelivered()
    assert calls["n"] == 1, "one attempt, then stop — not a loop"


def test_a_permanently_failing_message_stops_being_retried(seat, monkeypatch):
    """Found by testing against a real seat 1.0.2: an oversized body answers
    `failed` at exit 10 — retryable by the status table, identical every time.
    Unbounded retry would spin on it forever and hold the queue behind it."""
    from agent_comms import operations
    from agent_comms.store import Mention, Store

    store = Store(seat / ".comms")
    store.append(Mention(id=30, sender="agent-eco-arch", channel="agent-eco", topic="t",
                         content="x", timestamp=1, permalink="", reason="mentioned"))
    fake_seat(monkeypatch, {"success": False, "status": "failed",
                            "message": "body is 70000 bytes; the limit is 65536"}, code=10)

    for _ in range(operations.MAX_DELIVERY_ATTEMPTS + 2):
        operations.retry_undelivered()

    assert store.undelivered() == [], "it must leave the queue rather than block it"
    events = (seat / ".comms" / "events.log").read_text()
    assert "no longer being retried" in events
    assert "still" in events and "comms inbox" in events, "and it must say where it went"


def test_a_pre_1_0_seat_is_refused_loudly(monkeypatch):
    """Ruled 2026-09-17: comms 1.x hard-requires contract 1.0 and fails loudly
    rather than carrying both paths.

    Measured the same day on a real 0.5.1 seat, and the reason it must be loud:
    `seat msg` there prints the seat's own help and EXITS 0. Nothing in the shell
    says anything went wrong. Without this check the only symptom is unparseable
    output, which this client would report as a seat defect — blaming the wrong
    component for an upgrade-ordering mistake."""
    from agent_comms.seat import SeatContractUnsupported

    seat_app._contract_checked = None
    monkeypatch.setattr(seat_app.subprocess, "run", lambda cmd, **kw:
                        subprocess.CompletedProcess(
                            cmd, 0, json.dumps({"seat": "0.5.1", "contract": "0.5.1"}).encode(), b""))

    with pytest.raises(SeatContractUnsupported) as caught:
        seat_app.deliver("body")
    assert "0.5.1" in str(caught.value) and "1.x" in str(caught.value)
    assert "exiting 0" in str(caught.value), "say why silence is not evidence of success"


def test_the_contract_is_checked_once_not_per_message(monkeypatch):
    """A seat's build does not change under a running daemon. Re-asking per
    message would add a subprocess to the hot path to re-learn a constant."""
    calls = []
    seat_app._contract_checked = None

    def run(cmd, input=None, capture_output=True, timeout=None):
        calls.append(cmd[1] if len(cmd) > 1 else cmd[0])
        if "--version" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"seat": "1.0.2", "contract": "1.0"}).encode(), b"")
        return subprocess.CompletedProcess(
            cmd, 0, json.dumps({"success": True, "status": "delivered"}).encode(), b"")

    monkeypatch.setattr(seat_app.subprocess, "run", run)
    for _ in range(3):
        seat_app.deliver("body")
    assert calls.count("--version") == 1, f"asked {calls.count('--version')} times"


def test_the_suite_can_never_reach_a_real_binary(seat):
    """Containment, asserted rather than assumed — this has now drifted twice.

    2026-09-13: the suite's notify_command was the real `comms wake`, and six
    fixture mentions were typed into the running agent session. Shimmed `comms`.

    2026-09-21: 1.0.0 moved delivery from notify_command to `seat msg`, and the
    shim did not follow. Any test reaching seat.deliver() typed its fixture into
    the live session again — found by tracing subprocess.run and seeing
    ['seat','msg','--json'] leave the suite. The trigger was my own wiring:
    retry_undelivered runs on the daemon's backstop timer, so a test that fires
    the backstop delivers for real.

    Both times the containment was correct for the delivery path that existed
    when it was written, and silently wrong after the path moved. So assert the
    property — no real binary is reachable — rather than the mechanism.
    """
    import shutil
    import subprocess as sp

    for binary in ("comms", "seat"):
        resolved = shutil.which(binary)
        assert resolved is not None, f"{binary} should resolve to the shim, not be absent"
        assert ".test-bin" in resolved, (
            f"{binary} resolves to {resolved}, which is a REAL binary. A test that "
            "shells out to it reaches the live seat and types fixtures into a "
            "running agent session."
        )
        out = sp.run([binary, "msg"], capture_output=True)
        assert out.returncode == 127, f"{binary} shim must refuse, got {out.returncode}"
        assert b"test shim refused" in out.stderr


# -- devagent-seat-contract 1.1 (draft) ---------------------------------------
#
# 1.1 is additive: exit codes are unchanged, so only a consumer branching on the
# status STRING has to care. These are the places this client branches on it.
# Each fails against the pre-1.1 client.

def _answer(**fields):
    import json as _json
    payload = {"success": False, "status": "delivered", "message": "", "runtime": "claude",
               "seat": "test-claude"}
    payload.update(fields)
    return _json.dumps(payload).encode()


def test_conflicted_needs_a_person_not_a_retry():
    """Exit 20 means a person must intervene. `conflicted` is exit 20.

    Falling through as an ordinary refusal would leave nobody told, and nobody
    can fix two sessions claiming one agent except a person.
    """
    d = seat_app._parse(_answer(status="conflicted",
                            message="two live sessions could be test-claude.review: 41ab, 7c02"),
                    b"", 20)
    assert d.needs_a_person is True
    assert d.retryable is False


def test_unknown_agent_is_never_retried():
    """This seat does not serve that name. No number of retries changes that."""
    d = seat_app._parse(_answer(status="unknown-agent",
                            message="this seat serves test-claude.main, test-claude.review"),
                    b"", 10)
    assert d.retryable is False
    assert d.needs_a_person is False


def test_the_resolved_agent_and_label_are_kept():
    """1.1 answers say WHICH agent took it. A refusal that cannot name the
    intended recipient is a refusal nobody can act on."""
    d = seat_app._parse(_answer(success=True, status="delivered",
                            agent="test-claude.review", label="review"), b"", 0)
    assert (d.agent, d.label) == ("test-claude.review", "review")
    assert "test-claude.review" in d.summary()


def test_a_1_0_seat_answer_still_parses_with_the_new_fields_absent():
    """Absence is not an error — every 1.0 guarantee still holds."""
    d = seat_app._parse(_answer(success=True, status="delivered"), b"", 0)
    assert (d.agent, d.label) == ("", "")
    assert d.success is True


def test_agent_is_passed_as_a_flag_and_never_as_part_of_the_body(monkeypatch):
    """`--agent` is the only way to address one agent; an id is never an address."""
    seen = {}

    class _R:
        stdout = _answer(success=True, status="delivered", agent="test-claude.review",
                         label="review")
        stderr = b""
        returncode = 0

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["stdin"] = kw.get("input")
        return _R()

    monkeypatch.setattr(seat_app, "_contract_checked", "1.1")
    monkeypatch.setattr(seat_app.subprocess, "run", fake_run)
    seat_app.deliver("hello", agent="test-claude.review")

    assert seen["cmd"] == ["seat", "msg", "--json", "--agent", "test-claude.review"]
    assert seen["stdin"] == b"hello"


def test_no_agent_means_the_seats_default_and_adds_no_flag(monkeypatch):
    """A caller that passes no new flag behaves exactly as it does today."""
    seen = {}

    class _R:
        stdout = _answer(success=True, status="delivered")
        stderr = b""
        returncode = 0

    monkeypatch.setattr(seat_app, "_contract_checked", "1.1")
    monkeypatch.setattr(seat_app.subprocess, "run",
                        lambda cmd, **kw: (seen.__setitem__("cmd", cmd), _R())[1])
    seat_app.deliver("hello")
    assert seen["cmd"] == ["seat", "msg", "--json"]


@pytest.mark.parametrize("reported", ["1.0", "1.1", "1.1-draft", "1.2.3", "1"])
def test_the_gate_accepts_any_major_1(monkeypatch, reported):
    """Accept-any-major-1, never a string match on "1.0".

    1.1 is additive, so a seat reporting it is strictly MORE capable, not less.
    A gate that matched the string would refuse the better seat — the same defect
    class we found in their draft, pointing the other way. `1.1-draft` is the
    real spelling both test seats report today, measured, not assumed.
    """
    import json as _json

    class _R:
        stdout = _json.dumps({"contract": reported}).encode()
        stderr = b""
        returncode = 0

    monkeypatch.setattr(seat_app, "_contract_checked", None)
    monkeypatch.setattr(seat_app.subprocess, "run", lambda *a, **k: _R())
    assert seat_app.require_contract() == reported


@pytest.mark.parametrize("reported", ["0.5.1", "3.0", "10.0", ""])
def test_the_gate_refuses_anything_it_does_not_speak(monkeypatch, reported):
    """Pre-1.0 has no `seat msg`; major 3 is a contract nobody has written.

    `10.0` is here for the trap the obvious fix walks into: a gate that
    STARTSWITH "1" accepts major 10, which is not major 1.

    **`2.0` was in this list until 2026-09-22**, when arch ruled the gate
    widened to speak it. The list changed, the trap did not.
    """
    import json as _json

    from agent_comms.seat import SeatContractUnsupported

    class _R:
        stdout = _json.dumps({"contract": reported}).encode()
        stderr = b""
        returncode = 0

    monkeypatch.setattr(seat_app, "_contract_checked", None)
    monkeypatch.setattr(seat_app.subprocess, "run", lambda *a, **k: _R())
    with pytest.raises(SeatContractUnsupported):
        seat_app.require_contract()


# -- the gate as a SET of majors (arch ruling, 2026-09-22) ---------------------

@pytest.mark.parametrize("reported", ["1.0", "1.1", "1.1-draft", "1.2.3",
                                      "2.0", "2.0-draft", "2.3"])
def test_the_gate_speaks_majors_one_and_two(monkeypatch, reported):
    """Contract 1.0 §9 set seat-then-comms because comms fails against OLDER.
    Nothing set what happens when the seat goes NEWER by a major — and an
    equality gate refuses it totally, so seat-first breaks every seat while
    comms-first is impossible. Widened deliberately, one major at a time."""
    import json as _json

    class _R:
        stdout = _json.dumps({"contract": reported}).encode()
        stderr = b""
        returncode = 0

    monkeypatch.setattr(seat_app, "_contract_checked", None)
    monkeypatch.setattr(seat_app.subprocess, "run", lambda *a, **k: _R())
    assert seat_app.require_contract() == reported


@pytest.mark.parametrize("reported", ["0.5.1", "3.0", "10.0", "", "draft", "x.y"])
def test_the_gate_refuses_everything_else(monkeypatch, reported):
    """`10.0` is the trap a startswith check walks into; `3.0` is a contract
    nobody has written. Neither is guessed at."""
    import json as _json

    from agent_comms.seat import SeatContractUnsupported

    class _R:
        stdout = _json.dumps({"contract": reported}).encode()
        stderr = b""
        returncode = 0

    monkeypatch.setattr(seat_app, "_contract_checked", None)
    monkeypatch.setattr(seat_app.subprocess, "run", lambda *a, **k: _R())
    with pytest.raises(SeatContractUnsupported):
        seat_app.require_contract()


def test_the_gate_is_a_set_not_a_comparison():
    """A floor would admit a major 3 nobody has written. The estate widens this
    one major at a time, with the contract read first."""
    from agent_comms.seat import SPEAKABLE_CONTRACT_MAJORS

    assert SPEAKABLE_CONTRACT_MAJORS == frozenset({1, 2})
    assert 3 not in SPEAKABLE_CONTRACT_MAJORS and 0 not in SPEAKABLE_CONTRACT_MAJORS


@pytest.mark.parametrize("spelling,major", [
    ("2.0-draft", 2), ("1.1-draft", 1), ("10.0", 10), ("", None), ("draft", None)])
def test_the_major_is_parsed_as_a_number(spelling, major):
    from agent_comms.seat import _major

    assert _major(spelling) == major
