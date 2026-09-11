"""A dormant seat queues, and delivers when it wakes.

The operator's ruling for this release (2026-09-09): when a seat has no live
session, hold the message and deliver it the moment the seat wakes — waking is
the operator typing into the session; the daemon notices within a heartbeat.
Reading the inbox at session start/end was rejected as too much overhead.

Measured on the live hub: Zulip sends a heartbeat about every 54 seconds, so the
daemon loop turns over on a silent channel without any timer of its own.
"""

from __future__ import annotations

import pytest

from agent_comms import operations
from agent_comms import wake as wake_mod
from agent_comms.seat import SeatStatus
from tests.conftest import FakeTransport


def _event(msg_id, topic="agent-comms: do a thing"):
    return {"id": msg_id, "type": "message", "flags": [], "message": {
        "id": msg_id, "sender_full_name": "agent-eco-arch",
        "sender_email": "agent-eco-arch@h", "display_recipient": "agent-eco",
        "subject": topic, "content": f"message {msg_id}", "timestamp": 1,
        "stream_id": 7, "type": "stream"}}


def _wake_on(seat_dir):
    (seat_dir / ".comms" / "config.toml").write_text(
        "enabled = true\nwake = true\n", encoding="utf-8"
    )


def _dormant(monkeypatch):
    """The seat says it cannot be spoken to."""
    monkeypatch.setattr(
        operations, "seat_status_now",
        lambda: SeatStatus(verdict="not-addressable", reason="no live session",
                           runtime="claude"),
    )


def _asleep(monkeypatch):
    """Addressable but not attending — held, per the operator's ruling."""
    monkeypatch.setattr(
        operations, "seat_status_now",
        lambda: SeatStatus(verdict="addressable", runtime="claude",
                           target="rc:0.0", awake=False),
    )


def _awake(monkeypatch, delivered):
    """The seat says it can be spoken to, and we record what is typed into it."""
    monkeypatch.setattr(
        operations, "seat_status_now",
        lambda: SeatStatus(verdict="addressable", runtime="claude",
                           target="rc:0.0", awake=True),
    )

    def fake_tmux(*args):
        if args[0] == "send-keys" and "-l" in args:
            delivered.append(args[-1])

        class R:
            returncode = 0
            stderr = ""
            stdout = ""

        return R()

    monkeypatch.setattr(wake_mod, "_tmux", fake_tmux)
    monkeypatch.setattr(wake_mod.shutil, "which", lambda n: f"/usr/bin/{n}")


# -- dormant: hold, do not lose ----------------------------------------------

def test_a_dormant_seat_queues_rather_than_losing(seat, monkeypatch):
    _wake_on(seat)
    _dormant(monkeypatch)
    transport = FakeTransport(event_batches=[{"result": "success",
                                              "events": [_event(1001)]}])
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1)

    from agent_comms.store import Store
    pending = Store(seat / ".comms").undelivered()
    assert [m.id for m in pending] == [1001]


# -- waking: the queue empties itself ----------------------------------------

def test_waking_the_seat_delivers_what_was_queued(seat, monkeypatch):
    """The operator types hello; the daemon notices on the next heartbeat."""
    _wake_on(seat)
    _dormant(monkeypatch)
    transport = FakeTransport(event_batches=[
        {"result": "success", "events": [_event(1001), _event(1002)]},
    ])
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1)

    from agent_comms.store import Store
    assert len(Store(seat / ".comms").undelivered()) == 2

    # ... the seat wakes, and the next tick carries only a heartbeat
    delivered = []
    _awake(monkeypatch, delivered)
    transport2 = FakeTransport(event_batches=[{"result": "success", "events": []}])
    operations.run_daemon(transport_factory=lambda c: transport2, max_iterations=1)

    assert Store(seat / ".comms").undelivered() == []
    assert len(delivered) == 2


def test_queued_messages_go_in_the_order_they_arrived(seat, monkeypatch):
    """A conversation delivered out of order is worse than one delivered late."""
    _wake_on(seat)
    _dormant(monkeypatch)
    transport = FakeTransport(event_batches=[
        {"result": "success", "events": [_event(1001), _event(1002), _event(1003)]},
    ])
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1)

    delivered = []
    _awake(monkeypatch, delivered)
    operations.run_daemon(
        transport_factory=lambda c: FakeTransport(event_batches=[{"result": "success",
                                                                  "events": []}]),
        max_iterations=1,
    )
    assert ["message 1001", "message 1002", "message 1003"] == [
        next(part for part in ("message 1001", "message 1002", "message 1003")
             if part in line) for line in delivered
    ]


def test_a_still_dormant_seat_keeps_everything_queued(seat, monkeypatch):
    """Repeated ticks must not drop or reorder anything."""
    _wake_on(seat)
    _dormant(monkeypatch)
    transport = FakeTransport(event_batches=[
        {"result": "success", "events": [_event(1001)]},
        {"result": "success", "events": []},
        {"result": "success", "events": []},
    ])
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=3)

    from agent_comms.store import Store
    assert [m.id for m in Store(seat / ".comms").undelivered()] == [1001]


def test_a_delivered_message_is_not_delivered_twice(seat, monkeypatch):
    _wake_on(seat)
    delivered = []
    _awake(monkeypatch, delivered)
    transport = FakeTransport(event_batches=[
        {"result": "success", "events": [_event(1001)]},
        {"result": "success", "events": []},
        {"result": "success", "events": []},
    ])
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=3)
    assert len(delivered) == 1


# -- the transition is announced ---------------------------------------------

def test_waking_is_announced_once(seat, monkeypatch):
    """The sender was told it was queued; nothing else would tell them it landed."""
    _wake_on(seat)
    _dormant(monkeypatch)
    posted = []

    class T(FakeTransport):
        def call_endpoint(self, url, method="GET", request=None):
            if url == "messages":
                posted.append(request)
            return super().call_endpoint(url, method, request)

    operations.run_daemon(
        transport_factory=lambda c: T(event_batches=[{"result": "success",
                                                      "events": [_event(1001)]}]),
        max_iterations=1,
    )
    # The operator asked for this wording, 2026-09-11: a sender should be told
    # plainly that the message is **not deliverable**, in the seat's own words,
    # rather than left to infer it from "queued".
    assert any("not deliverable right now" in p["content"] for p in posted), (
        "the sender is told, on the way down"
    )

    posted.clear()
    delivered = []
    _awake(monkeypatch, delivered)
    operations.run_daemon(
        transport_factory=lambda c: T(event_batches=[{"result": "success", "events": []}]),
        max_iterations=1,
    )
    awake = [p for p in posted if p["topic"].endswith(": awake")]
    assert len(awake) == 1
    assert "1 queued message" in awake[0]["content"]


def test_nothing_is_flushed_when_wake_is_off(seat, monkeypatch):
    """`wake` off means this seat does not deliver at all; the store still holds."""
    delivered = []
    _awake(monkeypatch, delivered)
    transport = FakeTransport(event_batches=[{"result": "success",
                                              "events": [_event(1001)]}])
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1)
    assert delivered == []


def test_an_asleep_seat_holds_until_it_wakes(seat, monkeypatch):
    """Operator ruling 2026-09-09: addressable + awake:false means hold.

    We told skeleton in review that we would deliver here. That was wrong, and
    this is the test that keeps it wrong-proof.
    """
    _wake_on(seat)
    _asleep(monkeypatch)
    transport = FakeTransport(event_batches=[{"result": "success",
                                              "events": [_event(2001)]}])
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1)

    from agent_comms.store import Store
    pending = Store(seat / ".comms").undelivered()
    assert [m.id for m in pending] == [2001]

    delivered = []
    _awake(monkeypatch, delivered)
    operations.run_daemon(
        transport_factory=lambda c: FakeTransport(
            event_batches=[{"result": "success", "events": []}]),
        max_iterations=1,
    )
    assert len(delivered) == 1


def test_a_seat_with_no_seat_command_holds_and_says_so(seat, monkeypatch):
    """No legacy path: an un-updated seat holds visibly rather than guessing."""
    from agent_comms.seat import SeatUnavailable

    _wake_on(seat)

    def unavailable():
        raise SeatUnavailable("this seat has no `seat` command")

    monkeypatch.setattr(operations, "seat_status_now", unavailable)
    transport = FakeTransport(event_batches=[{"result": "success",
                                              "events": [_event(2002)]}])
    operations.run_daemon(transport_factory=lambda c: transport, max_iterations=1)

    from agent_comms.store import Store
    assert [m.id for m in Store(seat / ".comms").undelivered()] == [2002]
    assert "no `seat` command" in (seat / ".comms" / "events.log").read_text()


# -- delivered, but nothing is running to read it ----------------------------

def test_a_waiting_seat_tells_the_sender_how_to_have_it_read_now(seat, monkeypatch):
    """Operator ask, 2026-09-11: "a message queued - session not active" is
    useful when the codex seat needs waking.

    This is a *successful* delivery with a delay the sender cannot otherwise
    see — the message is in codex's queue. The remedy is theirs, so say it.
    """
    from agent_comms.seat import Persistence, SeatStatus

    _wake_on(seat)
    posted = []

    class T(FakeTransport):
        def call_endpoint(self, url, method="GET", request=None):
            if url == "messages":
                posted.append(request)
            return super().call_endpoint(url, method, request)

    waiting = SeatStatus(verdict="addressable", runtime="codex", awake=True,
                         target="01a0", sessions={"codex": 0})
    monkeypatch.setattr(operations, "seat_status_now", lambda: waiting)
    monkeypatch.setattr(operations, "seat_persistence", lambda: Persistence())
    monkeypatch.setattr(operations, "wake", lambda *a, **k: "delivered to 01a0 (codex)")

    operations.run_daemon(
        transport_factory=lambda c: T(event_batches=[{"result": "success",
                                                      "events": [_event(1101)]}]),
        max_iterations=1,
    )
    notices = [p for p in posted if "no codex session is running" in p["content"]]
    assert len(notices) == 1
    assert "seat start codex" in notices[0]["content"]
    assert "nothing is lost" in notices[0]["content"]


def test_the_waiting_notice_is_said_once(seat, monkeypatch):
    from agent_comms.seat import Persistence, SeatStatus

    _wake_on(seat)
    posted = []

    class T(FakeTransport):
        def call_endpoint(self, url, method="GET", request=None):
            if url == "messages":
                posted.append(request)
            return super().call_endpoint(url, method, request)

    waiting = SeatStatus(verdict="addressable", runtime="codex", awake=True,
                         target="01a0", sessions={"codex": 0})
    monkeypatch.setattr(operations, "seat_status_now", lambda: waiting)
    monkeypatch.setattr(operations, "seat_persistence", lambda: Persistence())
    monkeypatch.setattr(operations, "wake", lambda *a, **k: "delivered to 01a0 (codex)")

    operations.run_daemon(
        transport_factory=lambda c: T(event_batches=[
            {"result": "success", "events": [_event(1102)]},
            {"result": "success", "events": [_event(1103)]},
        ]),
        max_iterations=2,
    )
    assert len([p for p in posted if "no codex session is running" in p["content"]]) == 1
