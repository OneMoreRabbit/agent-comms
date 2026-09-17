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
    from agent_comms.seat import SeatTooOld

    seat_app._contract_checked = None
    monkeypatch.setattr(seat_app.subprocess, "run", lambda cmd, **kw:
                        subprocess.CompletedProcess(
                            cmd, 0, json.dumps({"seat": "0.5.1", "contract": "0.5.1"}).encode(), b""))

    with pytest.raises(SeatTooOld) as caught:
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
