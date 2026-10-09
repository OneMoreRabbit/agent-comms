"""The directory's registration 0.2 per-sender delivery obligation."""

from __future__ import annotations

import json
import time

from click.testing import CliRunner
import pytest

from agent_comms import cli, config_sync, operations, wake
from agent_comms.queue import HELD, QUEUED, REFUSED
from agent_comms.seat import Delivery
from agent_comms.store import Mention
from tests.conftest import FakeTransport


TARGET = "bakehouse.agent-eco.agent-comms"
OVERRIDES = {
    "bakehouse.atlas.arch": "inject",
    "bakehouse.orchestrator.estate-directory": "inject",
    "bakehouse.orchestrator.estate-monitor": "inject",
    "bakehouse.orchestrator.estate-review": "inject",
}
ARCH = "bakehouse.agent-eco.arch"


def _row(*, overrides=OVERRIDES, blocked=()):
    return {
        "agent": TARGET,
        "delivery": "hold",
        "delivery_overrides": overrides,
        "permissions": {"comms": {
            "partners": [*OVERRIDES, ARCH], "blocked": list(blocked),
        }},
        "transports": {"comms": {"channel": "agent-eco", "bot": "agent-eco-agent-comms"}},
    }


def _save(seat, row):
    (seat / ".comms" / "routes.json").write_text(json.dumps({
        "contract": "0.2", "generation": 19, "source": "directory",
        "fetched_at": "2026-10-09T08:00:00+00:00", "routes": [row],
    }), encoding="utf-8")


def _event(mid, sender):
    return {"id": mid, "type": "message", "flags": ["mentioned"], "message": {
        "id": mid, "sender_full_name": "test-sender", "sender_email": "sender@h",
        "display_recipient": "agent-eco", "subject": f"{TARGET}: delivery gate",
        "content": operations.addressed("agent-eco-agent-comms", f"marker {mid}",
                                      to_fqn=TARGET, from_fqn=sender),
        "timestamp": 1, "stream_id": 7, "type": "stream",
    }}


def test_registration_02_override_survives_refresh_and_is_visible(seat, monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"contract": "0.2", "generation": 19,
                               "assignments": [_row()]}).encode()

    monkeypatch.setattr(config_sync, "directory_address", lambda: "http://dev.invalid")
    monkeypatch.setattr(config_sync, "_credential", lambda: "test")
    monkeypatch.setattr(config_sync.urllib.request, "urlopen", lambda *_a, **_k: Response())
    state = seat / ".comms"
    fetched = config_sync.fetch("agent-eco", "agent-comms", state)
    assert fetched.source == "directory" and fetched.generation == 19
    assert config_sync.agent_set(state)[TARGET]["delivery_overrides"] == OVERRIDES

    plain = CliRunner().invoke(cli.main, ["config", "show"])
    assert plain.exit_code == 0, plain.output
    for sender in OVERRIDES:
        assert f"from {sender}: inject" in plain.output
    machine = CliRunner().invoke(cli.main, ["config", "show", "--json"])
    assert machine.exit_code == 0
    assert json.loads(machine.output)["routes"][0]["delivery_overrides"] == OVERRIDES


def test_exact_sender_override_then_scalar_then_10_fallback(seat, monkeypatch):
    state = seat / ".comms"
    _save(seat, _row())
    for sender in OVERRIDES:
        assert wake.effective_delivery(TARGET, sender, state) == (
            "inject", f"override for {sender}")
        assert wake.holds(TARGET, state, sender_fqn=sender) is False
    assert wake.effective_delivery(TARGET, ARCH, state) == ("hold", "default")
    assert wake.holds(TARGET, state, sender_fqn=ARCH) is True
    # An exact FQN is required: neither a bot name nor a prefix borrows a grant.
    assert wake.effective_delivery(TARGET, "estate-monitor", state)[0] == "hold"
    assert wake.effective_delivery(TARGET, "bakehouse.orchestrator.estate-monitor.extra", state)[0] == "hold"

    _save(seat, _row(overrides={"bakehouse.atlas.arch": "unknown"}))
    assert wake.effective_delivery(TARGET, "bakehouse.atlas.arch", state) == (
        "hold", "default")
    _save(seat, _row(overrides={}))
    assert wake.effective_delivery(TARGET, "bakehouse.atlas.arch", state) == (
        "hold", "default")
    _save(seat, {"agent": TARGET, "delivery": "unknown"})
    assert wake.effective_delivery(TARGET, ARCH, state)[0] == "inject"
    monkeypatch.setattr(config_sync, "agent_set", lambda _d: (_ for _ in ()).throw(OSError()))
    assert wake.effective_delivery(TARGET, ARCH, state)[0] == "inject"


def test_none_override_refuses_and_blocked_wins_before_delivery(seat):
    none_sender = "bakehouse.orchestrator.estate-review"
    blocked_sender = "bakehouse.atlas.arch"
    nonpartner_sender = "bakehouse.agent-eco.test-codex"
    row = _row(overrides={**OVERRIDES, none_sender: "none"},
               blocked=[blocked_sender])
    row["delivery_overrides"][nonpartner_sender] = "inject"
    _save(seat, row)
    events = [_event(9101, none_sender), _event(9102, blocked_sender),
              _event(9103, ARCH), _event(9104, nonpartner_sender)]
    transport = FakeTransport(event_batches=[{"result": "success", "events": events}])
    operations.run_daemon(transport_factory=lambda _c: transport, max_iterations=1)
    states = {m.id: m.state for m in operations.inbox()}
    assert states == {9101: REFUSED, 9102: REFUSED, 9103: HELD,
                      9104: REFUSED}
    store = operations.message_store(seat / ".comms")
    def causes(hub_id):
        return [r["cause"] for r in store.history(store._find(hub_id)["id"])]
    assert "delivery: none (override for " + none_sender + ")" in causes(9101)
    assert "sender not permitted" in causes(9102)
    assert "sender not permitted" in causes(9104)
    assert "delivery: hold (default) — the agent asks for it" in causes(9103)


def test_four_overrides_queue_while_an_unrelated_sender_is_held(seat):
    _save(seat, _row())
    events = [_event(9200 + i, sender) for i, sender in enumerate(OVERRIDES)]
    events.append(_event(9204, ARCH))
    transport = FakeTransport(event_batches=[{"result": "success", "events": events}])
    operations.run_daemon(transport_factory=lambda _c: transport, max_iterations=1)
    states = {m.id: m.state for m in operations.inbox()}
    assert [states[9200 + i] for i in range(4)] == [QUEUED] * 4
    assert states[9204] == HELD


def test_wake_hands_only_the_matching_sender_to_the_seat(seat, monkeypatch):
    _save(seat, _row(overrides={**OVERRIDES,
                                "bakehouse.orchestrator.estate-review": "none"}))
    handed = []

    def deliver(body, *, agent):
        handed.append((body, agent))
        return Delivery(success=True, status="delivered", agent=agent)

    monkeypatch.setattr(wake.seat_app, "deliver", deliver)
    def mention(sender):
        return {"id": 9301, "agent": TARGET, "sender_fqn": sender,
                "sender": "seat-bot", "topic": "gate", "content": "marker",
                "permalink": "", "timestamp": 1}

    got = wake.wake(mention("bakehouse.orchestrator.estate-monitor"))
    assert got.success and [agent for _, agent in handed] == [TARGET]
    with pytest.raises(wake.Held, match="default"):
        wake.wake(mention(ARCH))
    with pytest.raises(wake.NoDelivery, match="override"):
        wake.wake(mention("bakehouse.orchestrator.estate-review"))
    assert len(handed) == 1, "hold and none must never call seat msg"


def test_a_queued_message_becomes_refused_if_policy_changes_to_none(seat):
    sender = "bakehouse.orchestrator.estate-review"
    _save(seat, {"agent": TARGET, "delivery": "inject"})
    store = operations.message_store(seat / ".comms")
    store.append(Mention(id=9401, sender="seat-bot", sender_fqn=sender,
                         agent=TARGET, channel="agent-eco", topic="gate",
                         content="marker", timestamp=int(time.time()), permalink=""))
    assert store._find(9401)["state"] == QUEUED

    _save(seat, _row(overrides={sender: "none"}))
    assert operations.retry_undelivered(
        transport_factory=lambda _c: FakeTransport()) == 0
    assert store._find(9401)["state"] == REFUSED
    assert store._find(9401)["attempts"] == 0
