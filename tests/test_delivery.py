"""Comms delivers to an FQN. The seat turns a route into a session."""

from __future__ import annotations

import pytest

from agent_comms.delivery import NoRouteToPass, plan
from agent_comms.resolve import Resolution


def _resolved(**kw):
    base = dict(success=True, status="resolved", requested="arch",
                canonical_id="bakehouse.agent-eco.arch", seat="agent-eco/arch",
                local_route="arch", delivery="inject")
    base.update(kw)
    return Resolution(**base)


def test_comms_passes_the_local_route_the_estate_authored():
    p = plan(_resolved())
    assert p.route == "arch"
    assert p.fqn == "bakehouse.agent-eco.arch"


def test_a_missing_local_route_fails_loudly_and_is_never_derived():
    """Measured: `seat msg --agent` refuses an estate FQN (unknown-agent,
    exit 10), and `local_route` is null on all 32 live records.

    Deriving one from the FQN would be §11 — a plausible value nobody declared
    — and wrong in the way that matters: a local route is a NAMING CHOICE, not
    a fact recoverable from the name. A message in the wrong session is worse
    than one that did not arrive, because nobody can tell it happened.
    """
    with pytest.raises(NoRouteToPass) as caught:
        plan(_resolved(local_route=""))

    said = str(caught.value)
    assert "local_route" in said
    assert "not derived" in said or "not a fact recoverable" in said
    assert "arch" not in said.split("resolved to seat")[0].replace(
        "bakehouse.agent-eco.arch", ""), "must not have invented a route"


def test_a_failed_resolution_never_becomes_a_delivery():
    with pytest.raises(NoRouteToPass):
        plan(Resolution(success=False, status="unknown", requested="nobody"))


def test_the_plan_carries_the_degraded_label_through():
    """A person reading a delivery record must be able to see the route came
    from cache, not from the directory."""
    from agent_comms.resolve import FROM_CACHE

    p = plan(_resolved(source=FROM_CACHE, reason="the credential was refused"))
    assert p.degraded is True
    assert p.reason == "the credential was refused"
