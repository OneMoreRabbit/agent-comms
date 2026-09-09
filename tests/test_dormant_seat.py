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
    """No agent session anywhere."""
    monkeypatch.setattr(wake_mod, "list_panes", lambda: [])
    monkeypatch.setattr(wake_mod, "find_runtime_panes", lambda p, r: [])


def _awake(monkeypatch, delivered):
    """A live, typeable session that records what it receives."""
    monkeypatch.setattr(
        wake_mod, "list_panes",
        lambda: [wake_mod.Pane(target="rc:0.0", command="claude", path="/w", pid=1)],
    )
    monkeypatch.setattr(
        wake_mod, "find_runtime_panes",
        lambda p, r: [wake_mod.Pane(target="rc:0.0", command="claude", path="/w", pid=1)],
    )
    monkeypatch.setattr(wake_mod, "pane_blocked_reason", lambda t: None)

    def fake_tmux(*args):
        if args[0] == "send-keys" and "-l" in args:
            delivered.append(args[-1])

        class R:
            returncode = 0
            stderr = ""
            stdout = ""

        return R()

    monkeypatch.setattr(wake_mod, "_tmux", fake_tmux)


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
    assert any("queued" in p["content"] for p in posted), "queued notice on the way down"

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
