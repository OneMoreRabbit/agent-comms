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
