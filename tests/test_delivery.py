"""Comms delivers to an FQN. The seat turns an FQN into a session."""

from __future__ import annotations

import pytest

from agent_comms.delivery import HOLD, INJECT, NONE, NotDeliverable, permitted_to_send, plan
from agent_comms.resolve import FROM_CACHE, Resolution


def _resolved(**kw):
    base = dict(success=True, status="resolved", requested="arch",
                canonical_id="bakehouse.agent-eco.arch", seat="agent-eco/arch",
                delivery="inject")
    base.update(kw)
    return Resolution(**base)


def test_comms_passes_the_fqn_and_nothing_else():
    """`local_route` is dead (seat-router v1.2 §A1.2): the FQN binds to the slot
    directly and no third name exists."""
    p = plan(_resolved())
    assert p.fqn == "bakehouse.agent-eco.arch"


def test_nothing_reads_a_route_field_any_more():
    """`local_route` is deleted, not defaulted.

    It asked the orchestrator to author what only the seat can know, which is
    why it was null on all 32 live records. A field that governs nothing is
    deleted (§11) — carrying it would leave a control an operator could set and
    nothing would read.
    """
    from agent_comms.resolve import Resolution as _R

    assert "local_route" not in _R.__dataclass_fields__


def test_an_unnamed_resolution_is_never_given_a_constructed_name():
    """The estate names its agents. Comms carries the name it is given.

    A message delivered into the wrong session cannot be noticed by anyone,
    which is why this fails instead of guessing.
    """
    with pytest.raises(NotDeliverable, match="does not construct"):
        plan(_resolved(canonical_id=""))


def test_a_failed_resolution_never_becomes_a_delivery():
    with pytest.raises(NotDeliverable):
        plan(Resolution(success=False, status="unknown", requested="nobody"))


def test_the_degraded_label_survives_into_the_plan():
    p = plan(_resolved(source=FROM_CACHE, reason="the credential was refused"))
    assert p.degraded is True and p.reason == "the credential was refused"


@pytest.mark.parametrize("mode,ok", [(INJECT, True), (HOLD, True), (NONE, False)])
def test_the_estates_three_delivery_modes(mode, ok):
    assert permitted_to_send(mode)[0] is ok


@pytest.mark.parametrize("typo", ["Inject", "queue", "", "injectt"])
def test_an_unknown_delivery_mode_is_refused_never_defaulted(typo):
    """The seat carries this value verbatim and never reads it, so comms is the
    only thing in the estate that can catch a typo in it."""
    ok, why = permitted_to_send(typo)
    assert ok is False
    assert "not one of" in why
