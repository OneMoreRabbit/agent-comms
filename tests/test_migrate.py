"""The one-shot import. Both ambiguities refuse to guess."""

from __future__ import annotations

import json

from agent_comms.migrate import import_jsonl
from agent_comms.queue import DELIVERED, EXPIRED, REFUSED, Queue


def write(tmp_path, *records):
    p = tmp_path / "messages.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records))
    return p


def base(**kw):
    r = dict(id=1, sender="agent-eco-arch", content="x", topic="t", permalink="",
             timestamp=1_760_000_000, delivered=True, read=False, attempts=0,
             authorised=True, channel="agent-eco", reason="mentioned")
    r.update(kw)
    return r


def test_a_refused_sender_never_imports_as_delivered(tmp_path):
    """MEASURED ON THE REAL STORE: all 15 refused messages also carry
    delivered:true, because 1.0.0 marked a message delivered once it had been
    HANDLED — stored and shown so the agent could report the sender
    (ADR-0009 §1a). Under the new states those are two different words.

    The naive rule `delivered:true -> delivered` would record 15 refusals as
    successful deliveries and lose the evidence that the boundary worked.
    """
    src = write(tmp_path, base(id=1, authorised=False, delivered=True))
    q = Queue(tmp_path / "comms.db")
    out = import_jsonl(src, q)

    assert (out.refused, out.delivered) == (1, 0)
    assert q.counts() == {REFUSED: 1}


def test_the_refusal_keeps_what_the_old_record_also_said(tmp_path):
    """Nothing is lost and nothing is invented: the transition row says the
    1.0.0 record ALSO said delivered, so the ambiguity survives the import."""
    src = write(tmp_path, base(authorised=False, delivered=True))
    q = Queue(tmp_path / "comms.db")
    import_jsonl(src, q)

    causes = [r["cause"] for r in q.history(1)]
    assert any("also said delivered" in c for c in causes)


def test_an_undelivered_message_expires_rather_than_being_assumed(tmp_path):
    """`delivered:false` mixes never-attempted with delivered-but-unrecorded —
    the flood's own signature. The JSONL cannot tell them apart.

    An honest overcount of expiry is safe; an optimistic `delivered` writes
    September into the new store on its first day.
    """
    src = write(tmp_path, base(delivered=False))
    q = Queue(tmp_path / "comms.db")
    out = import_jsonl(src, q)

    assert out.expired == 1
    assert q.state_of(1) == EXPIRED


def test_a_delivered_message_carries_its_attempt(tmp_path):
    src = write(tmp_path, base(delivered=True))
    q = Queue(tmp_path / "comms.db")
    import_jsonl(src, q)

    assert q.state_of(1) == DELIVERED
    assert q.db.execute("SELECT attempts FROM messages WHERE id=1").fetchone()["attempts"] == 1


def test_running_it_twice_changes_nothing(tmp_path):
    """A migration a person is afraid to re-run is one they run once, wrongly."""
    src = write(tmp_path, base(id=1), base(id=2, authorised=False))
    q = Queue(tmp_path / "comms.db")
    import_jsonl(src, q)
    before = q.counts()
    second = import_jsonl(src, q)

    assert q.counts() == before
    assert second.written == 0 or q.counts() == before


def test_the_reconciliation_accounts_for_every_line(tmp_path):
    """Printed counts are the check a person actually performs."""
    src = write(tmp_path, base(id=1), base(id=2, authorised=False),
                base(id=3, delivered=False))
    q = Queue(tmp_path / "comms.db")
    out = import_jsonl(src, q)

    assert out.read == 3 and out.written == 3
    assert "0 unaccounted for" in out.report(src, tmp_path / "comms.db")


def test_a_malformed_line_is_skipped_and_named(tmp_path):
    """One bad line must cost one message, not the history."""
    p = tmp_path / "messages.jsonl"
    p.write_text(json.dumps(base(id=1)) + "\n{not json\n" + json.dumps(base(id=2)))
    q = Queue(tmp_path / "comms.db")
    out = import_jsonl(p, q)

    assert out.read == 2 and len(out.skipped) == 1


def test_the_import_carries_the_timestamp_and_channel(tmp_path):
    """Without the epoch an imported message reads as 1970 — and the age bound
    treats the entire imported history as ancient. The epoch-1970 trap
    (catalogue 0.55) arriving in production data rather than in test data."""
    src = write(tmp_path, base(id=1, timestamp=1_760_000_000, channel="agent-eco"))
    q = Queue(tmp_path / "comms.db")
    import_jsonl(src, q)

    row = q.db.execute("SELECT received_at_epoch, channel FROM messages WHERE id=1").fetchone()
    assert row["received_at_epoch"] == 1_760_000_000
    assert row["channel"] == "agent-eco"


def test_the_import_catches_up_rather_than_running_once(tmp_path):
    """MEASURED ON A LIVE SEAT: a daemon still running pre-2.0 code keeps
    appending to the JSONL while the CLI reads SQLite. A once-only marker stops
    the two ever meeting, and messages sit in the JSONL invisible to `show`
    with nothing reporting a problem.

    The import is idempotent on the hub id, so running it every time costs a
    file read and closes the window entirely.
    """
    from agent_comms.operations import message_store

    src = write(tmp_path, base(id=1))
    store = message_store(tmp_path)
    assert len(store.all()) == 1

    # something else appends to the JSONL afterwards — the old daemon
    with src.open("a") as fh:
        fh.write("\n" + json.dumps(base(id=2)))

    assert len(message_store(tmp_path).all()) == 2, "the late arrival was stranded"
