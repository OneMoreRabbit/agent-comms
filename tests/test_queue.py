"""The store — comms-design §3. Every rule here was bought by the September flood."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_comms.queue import (ABANDONED, DELIVERED, EXPIRED, HELD, QUEUED, RECEIVED,
                               RETRIEVED, ForwardOnly, Queue)


def old(hours):
    return (datetime.now(tz=timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


@pytest.fixture
def q(tmp_path):
    return Queue(tmp_path / "comms.db")


def test_a_message_never_moves_backwards(q):
    """A store that moves backwards cannot be evidence, and this store is what
    the last incident was reconstructed from."""
    m = q.receive(hub_id="1", sender="arch", body="x")
    q.move(m, QUEUED, "permitted")
    q.record_attempt(m, delivered=True, detail="typed in")

    for backwards in (QUEUED, RECEIVED, HELD):
        with pytest.raises(ForwardOnly):
            q.move(m, backwards, "no")


def test_the_attempt_and_the_mark_land_together(q):
    """THE FIX FOR THE FLOOD. No window between 'sent' and 'recorded'."""
    m = q.receive(hub_id="1", sender="arch", body="x")
    q.move(m, QUEUED, "permitted")
    q.record_attempt(m, delivered=True, detail="typed in")

    row = q.db.execute("SELECT state, attempts FROM messages WHERE id=?", (m,)).fetchone()
    assert (row["state"], row["attempts"]) == (DELIVERED, 1)
    marked = [r for r in q.history(m) if r["became"] == DELIVERED]
    assert len(marked) == 1 and marked[0]["attempt"] == 1


def test_a_failed_wake_never_re_queues_a_delivered_message(q):
    """Four failed wakes seeded 62 permanently-unmarked records. Wake is a
    different fact in a different table, and it cannot touch a message state."""
    m = q.receive(hub_id="1", sender="arch", body="x")
    q.move(m, QUEUED, "permitted")
    q.record_attempt(m, delivered=True, detail="typed in")
    q.record_wake(m, "failed", "notify_command exited 5: agent-not-awake")

    assert q.state_of(m) == DELIVERED
    assert q.db.execute("SELECT COUNT(*) c FROM wakes").fetchone()["c"] == 1


def test_three_attempts_and_it_is_abandoned_not_retried_forever(q):
    m = q.receive(hub_id="1", sender="arch", body="x")
    q.move(m, QUEUED, "permitted")
    for _ in range(3):
        q.record_attempt(m, delivered=False, detail="no-session")
    assert q.state_of(m) == ABANDONED


def test_age_is_checked_before_EVERY_attempt_not_only_the_first(q):
    """The design says before the first attempt. That leaves any message which
    got one attempt unbounded, because no retry interval is fixed — so a stale
    message reaches a session days later, which is the exact thing the rule
    exists to prevent. Built to the rule's stated intent instead."""
    m = q.receive(hub_id="1", sender="arch", body="x", received_at=old(30))
    q.move(m, QUEUED, "permitted")
    q.record_attempt(m, delivered=False, detail="no-session")   # already attempted once

    assert q.retire().expired == 1, "an already-attempted message escaped the age bound"
    assert q.state_of(m) == EXPIRED
    assert m not in [r["id"] for r in q.due()]


def test_nothing_stale_is_ever_offered_for_delivery(q):
    """A stale message must not reach a session EVEN ONCE: it arrives looking
    current, which is what makes it dangerous."""
    fresh = q.receive(hub_id="new", sender="arch", body="x")
    stale = q.receive(hub_id="old", sender="arch", body="x", received_at=old(48))
    for m in (fresh, stale):
        q.move(m, QUEUED, "permitted")

    assert [r["id"] for r in q.due()] == [fresh]


def test_a_backlog_is_capped_newest_first(q):
    """The operator's cap: a backlog must never arrive as N live turns. Newest
    first, because the newest are the likeliest to still be current."""
    ids = [q.receive(hub_id=str(i), sender="arch", body="x") for i in range(10)]
    for m in ids:
        q.move(m, QUEUED, "permitted")

    due = [r["id"] for r in q.due()]
    assert len(due) == 3
    assert due == ids[-3:][::-1], "not the three newest, newest first"
    assert q.backlog() == 7


def test_retirement_produces_one_line_with_the_supersession_caveat(q):
    for i in range(4):
        m = q.receive(hub_id=str(i), sender="arch", body="x", received_at=old(40))
        q.move(m, QUEUED, "permitted")

    line = q.retire().line()
    assert line.count("\n") == 0, "a retirement must be ONE line, never N turns"
    assert "4 message(s) retired" in line
    assert "read newest-first" in line and "not instructions" in line


def test_retired_is_not_deleted(q):
    m = q.receive(hub_id="1", sender="arch", body="x", received_at=old(40))
    q.move(m, QUEUED, "permitted")
    q.retire()

    assert q.state_of(m) == EXPIRED
    assert q.db.execute("SELECT body FROM messages WHERE id=?", (m,)).fetchone()["body"] == "x"
    assert len(q.history(m)) >= 3


def test_a_held_message_is_retrieved_not_delivered(q):
    """`delivered` means the seat took it. A held message the agent came and got
    is a different fact, and one word for both makes 'what reached a session'
    unanswerable. Flagged in our review; built as two words."""
    m = q.receive(hub_id="1", sender="arch", body="x")
    q.move(m, HELD, "delivery: hold")
    q.move(m, RETRIEVED, "the agent read it from the inbox")

    assert q.state_of(m) == RETRIEVED
    assert q.counts().get(DELIVERED, 0) == 0


def test_a_refused_message_is_stored_and_never_delivered(q):
    """ADR-0009 §1a: an agent must be able to report an unexpected sender, so
    the refusal is kept and visible, not dropped."""
    from agent_comms.queue import REFUSED

    m = q.receive(hub_id="1", sender="stranger", body="x")
    q.move(m, REFUSED, "sender not permitted", rule="partners")

    assert q.state_of(m) == REFUSED
    assert q.counts() == {REFUSED: 1}
    with pytest.raises(ForwardOnly):
        q.move(m, QUEUED, "no")


def test_the_same_hub_message_is_stored_once(q):
    a = q.receive(hub_id="dup", sender="arch", body="x")
    b = q.receive(hub_id="dup", sender="arch", body="x")
    assert a == b
    assert q.counts() == {RECEIVED: 1}


def test_seq_is_monotonic_and_local(q):
    ids = [q.receive(hub_id=str(i), sender="arch", body="x") for i in range(3)]
    seqs = [q.db.execute("SELECT seq FROM messages WHERE id=?", (m,)).fetchone()["seq"]
            for m in ids]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3


def test_a_broken_seat_does_not_burn_the_attempt_budget(q):
    """Measured on agent-skeleton's seat: a seat upgraded before its config file
    existed answered `broken` to every delivery, and that mail was NEVER
    re-delivered after a person fixed it. Stranded.

    `broken` and `conflicted` are not the message's fault and no retry could
    have worked, so consuming the budget on them is what strands the mail —
    three broken windows and a message is abandoned having never had one real
    attempt.
    """
    m = q.receive(hub_id="1", sender="arch", body="x")
    q.move(m, QUEUED, "permitted")

    for _ in range(5):
        q.record_attempt(m, delivered=False, detail="broken — no agent configuration",
                         consumes_attempt=False)

    assert q.state_of(m) == QUEUED, "a broken seat abandoned the message"
    assert q.db.execute("SELECT attempts FROM messages WHERE id=?", (m,)).fetchone()["attempts"] == 0
    assert m in [r["id"] for r in q.due()], "the message is not waiting for the fix"


def test_the_message_delivers_on_the_pass_after_the_seat_is_fixed(q):
    """The stranding, closed: no fresh message is needed to point an agent at
    its own backlog."""
    m = q.receive(hub_id="1", sender="arch", body="x")
    q.move(m, QUEUED, "permitted")
    q.record_attempt(m, delivered=False, detail="broken", consumes_attempt=False)

    q.record_attempt(m, delivered=True, detail="typed in")      # a person fixed the seat
    assert q.state_of(m) == DELIVERED


def test_a_non_attempt_is_still_written_down(q):
    """Every transition is a row. 'Nothing happened' is a fact worth keeping —
    it is how you reconstruct a broken window afterwards."""
    m = q.receive(hub_id="1", sender="arch", body="x")
    q.move(m, QUEUED, "permitted")
    q.record_attempt(m, delivered=False, detail="broken", consumes_attempt=False)

    causes = [r["cause"] for r in q.history(m)]
    assert any("not attemptable" in c and "no attempt consumed" in c for c in causes)


def test_a_broken_window_cannot_hold_a_message_forever(q):
    """Not consuming attempts must not mean waiting indefinitely. The age bound
    is what stops that, and it still applies."""
    m = q.receive(hub_id="1", sender="arch", body="x", received_at=old(40))
    q.move(m, QUEUED, "permitted")
    q.record_attempt(m, delivered=False, detail="broken", consumes_attempt=False)

    assert q.retire().expired == 1
    assert q.state_of(m) == EXPIRED


def test_the_permalink_is_stored_not_discarded(q):
    """`receive` accepted a permalink and threw it away — a phantom parameter,
    the exact class this client spends its time flagging elsewhere.

    It matters because the permalink is how chat cites the record: a stored
    message nobody can cite is a message that cannot be pointed at in a
    post-mortem, which is what the store is FOR.
    """
    m = q.receive(hub_id="1", sender="arch", body="x",
                  permalink="https://hub/#narrow/channel/5-agent-eco/topic/t/near/1")
    row = q.db.execute("SELECT permalink FROM messages WHERE id=?", (m,)).fetchone()
    assert row["permalink"].endswith("/near/1")


# -- the envelope address: one bot, several agents ---------------------------

def test_the_addressed_fqn_is_read_from_the_topic():
    """One bot is one SEAT's mailbox, so the agent cannot ride on the mention.
    The topic prefix is what --to named, and that is the envelope address."""
    from agent_comms.operations import addressed_agent
    mine = {"bakehouse.agent-eco.test-claude-another1",
            "bakehouse.agent-eco.test-claude-new001"}
    assert addressed_agent("bakehouse.agent-eco.test-claude-another1: uc01", mine) == \
        "bakehouse.agent-eco.test-claude-another1"
    # A bare seat name is NOT an agent address — this is what keeps seat-name
    # addressing working exactly as it did at 1.0.
    assert addressed_agent("test-claude: uc01", mine) == ""
    assert addressed_agent("", mine) == ""
    assert addressed_agent("no colon here", mine) == ""
    # Shape is three segments. Two or four is not an FQN, and a guess here
    # would be the prefix-matching trap the estate has already paid for.
    assert addressed_agent("agent-eco.test-claude: x", mine) == ""
    assert addressed_agent("a.b.c.d: x", mine) == ""


def test_a_reply_in_someone_elses_topic_is_not_an_envelope_address():
    """THE REPLY TRAP. A reply stays in the topic it answers, so the prefix
    names whoever the THREAD was opened to — not whoever this message is for.

    Measured 2026-09-25: test-claude's agent replied to test-codex in topic
    `bakehouse.agent-eco.test-claude-another1: uc01-another1`. test-codex read
    that as its envelope address and its seat answered `unknown-agent` —
    'assigned to another seat'. The reply sat queued and undelivered."""
    from agent_comms.operations import addressed_agent
    theirs = "bakehouse.agent-eco.test-claude-another1"
    # On test-codex, which serves none of test-claude's agents:
    assert addressed_agent(f"{theirs}: uc01-another1",
                           {"bakehouse.agent-eco.test-codex-dave"}) == ""
    # Fails safe: nothing assigned yet means no --agent, so the seat's default
    # answers. Never worse than 1.0.
    assert addressed_agent(f"{theirs}: uc01-another1", set()) == ""


def test_the_envelope_address_survives_the_store(tmp_path):
    """It is no use reading the FQN if it is dropped on the way to the seat."""
    from agent_comms.store import Mention
    from agent_comms.queue import MessageStore
    q = MessageStore(tmp_path / "comms.db")
    m = Mention(id=9001, sender="test-codex", channel="seat-testing",
                topic="bakehouse.agent-eco.test-claude-another1: uc01",
                agent="bakehouse.agent-eco.test-claude-another1",
                content="body", timestamp=1758800000, permalink="")
    q.append(m)
    row = q.db.execute("SELECT agent FROM messages WHERE hub_id='9001'").fetchone()
    assert row["agent"] == "bakehouse.agent-eco.test-claude-another1"
    assert q.all()[-1].agent == "bakehouse.agent-eco.test-claude-another1"


def test_the_seat_is_told_which_agent(monkeypatch):
    """THE WIRING. Without this the seat delivers to its DEFAULT agent and says
    `delivered` — a silent delivery to the wrong recipient. Measured on
    test-claude 2026-09-25 before this was connected."""
    from agent_comms import wake as W
    seen = {}

    def fake_deliver(body, timeout=30, agent=None):
        seen["agent"] = agent
        from agent_comms.seat import Delivery
        return Delivery(success=True, status="delivered", message="typed in")

    monkeypatch.setattr(W.seat_app, "deliver", fake_deliver)
    W.wake({"id": 1, "sender": "s", "content": "x", "topic": "t",
            "agent": "bakehouse.agent-eco.test-claude-another1",
            "timestamp": 1758800000, "permalink": "", "channel": "seat-testing"})
    assert seen["agent"] == "bakehouse.agent-eco.test-claude-another1"

    # Addressed to the seat: no --agent, so the seat's default answers.
    W.wake({"id": 2, "sender": "s", "content": "x", "topic": "t", "agent": "",
            "timestamp": 1758800000, "permalink": "", "channel": "seat-testing"})
    assert seen["agent"] is None


# -- same-seat addressing: one bot, sibling agents ---------------------------

def test_the_sender_writes_an_explicit_envelope():
    from agent_comms.operations import addressed, envelope_from_body
    out = addressed("test-claude", "hello", to_fqn="bakehouse.agent-eco.test-claude-another1")
    assert out.startswith("@**test-claude** →`bakehouse.agent-eco.test-claude-another1` ")
    assert envelope_from_body(out) == "bakehouse.agent-eco.test-claude-another1"
    # Unaddressed sends are unchanged — no marker, no behaviour change.
    assert addressed("test-claude", "hello") == "@**test-claude** hello"
    assert envelope_from_body("@**test-claude** hello") == ""


def test_our_own_post_is_kept_only_when_it_addresses_one_of_our_agents():
    """SAME-SEAT ADDRESSING. The FQN is the agent and the bot is the seat, so an
    agent addressing a sibling posts through this very bot and the message
    comes straight back. Dropping every self-post made that impossible.

    But an agent REPLYING in its own thread has a topic naming itself, and
    accepting that would hand the agent its own words back forever. A send
    carries the marker; a reply does not — which is the distinction the topic
    cannot make."""
    from agent_comms.operations import addressed_to_seat, addressed
    from agent_comms.config import Settings, Identity

    settings = Settings(identity=Identity(project="agent-eco", seat="test-claude"),
                        channel="seat-testing", role="component")
    me = "test-claude-bot@example.com"
    sibling = "bakehouse.agent-eco.test-claude-another1"

    addressed_to_sibling = {"sender_email": me, "subject": f"{sibling}: x",
                            "content": addressed("test-claude", "hi", to_fqn=sibling)}
    assert addressed_to_seat(settings, addressed_to_sibling, [], me) == \
        "addressed to an agent on this seat"

    # A reply from that same agent, in its own topic, carries NO marker.
    our_own_reply = {"sender_email": me, "subject": f"{sibling}: x",
                     "content": "@**test-codex** thanks, noted"}
    assert addressed_to_seat(settings, our_own_reply, [], me) is None


def test_the_marker_beats_the_topic_and_a_reply_cannot_forge_one():
    from agent_comms.operations import addressed_agent, addressed
    mine = {"bakehouse.agent-eco.test-claude-another1",
            "bakehouse.agent-eco.test-claude-new001"}
    body = addressed("test-claude", "hi", to_fqn="bakehouse.agent-eco.test-claude-new001")
    # Topic says another1, the marker says new001 — the marker is the address.
    assert addressed_agent("bakehouse.agent-eco.test-claude-another1: x", mine, body) == \
        "bakehouse.agent-eco.test-claude-new001"
    # A marker naming an agent we do not serve is not ours to claim.
    foreign = addressed("test-claude", "hi", to_fqn="bakehouse.agent-eco.test-codex-dave")
    assert addressed_agent("whatever: x", mine, foreign) == ""


def test_the_attempt_bound_is_enforced_on_the_path_the_daemon_USES(tmp_path, monkeypatch):
    """UC-05, which failed live. The retry loop called a shim that added one to
    the counter and enforced nothing, so `max_attempts` governed no live path:
    a message on test-codex reached its EIGHTH attempt against a cap of three.

    The bound now lives in one place and is applied in the same transaction as
    the attempt. This drives the REAL path -- `attempt_by_hub_id`, by hub id,
    as the daemon calls it -- because a test against the store's private API
    is what let the gap live."""
    from agent_comms.queue import MessageStore, QUEUED, ABANDONED
    from agent_comms.store import Mention
    q = MessageStore(tmp_path / "comms.db", max_attempts=3)
    q.append(Mention(id=4242, sender="test-codex", channel="seat-testing",
                     topic="t", content="body", timestamp=1758800000, permalink=""))
    assert q.state_of(q._find(4242)["id"]) == QUEUED

    counts = [q.attempt_by_hub_id(4242, "no-session") for _ in range(3)]
    assert counts == [1, 2, 3], counts
    assert q.state_of(q._find(4242)["id"]) == ABANDONED, \
        "three attempts against a cap of three must abandon, not keep counting"

    # And there is exactly ONE bound, not a second one somewhere else.
    import agent_comms.operations as O
    assert not hasattr(O, "MAX_DELIVERY_ATTEMPTS"), \
        "a second definition of the bound is how the first came to govern nothing"
