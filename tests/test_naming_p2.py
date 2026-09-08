"""P2 — `comms doctor` must pass clean on the estate's actual names.

The collation's evidence: doctor warned persistently on `blocks-service` and on
`zuliprc-blocks-service`, both of which are **correct** under ADR-0009 §7a. A
warning that fires on correct configuration is §9's second failure, and the
collation is right that it would have repeated on every remaining seat.

These pin the real deployed shapes so the wider rollout does not reintroduce it.
"""

from __future__ import annotations

import pytest

from agent_comms.config import Identity

# (project, seat, role, the bot the estate actually minted, the credential it delivered)
DEPLOYED = [
    ("blocks", "blocks-service", "component", "blocks-service", "zuliprc-blocks-service"),
    ("blocks", "blocks-android", "component", "blocks-android", "zuliprc-blocks-android"),
    ("blocks", "arch", "arch", "blocks-arch", "zuliprc-blocks-arch"),
    ("agent-eco", "agent-comms", "component", "agent-comms", "zuliprc-agent-comms"),
    ("orient", "app", "component", "app", "zuliprc-app"),
    ("orient", "arch", "arch", "orient-arch", "zuliprc-orient-arch"),
]


@pytest.mark.parametrize("project,seat,role,bot,cred", DEPLOYED)
def test_deployed_names_are_canonical(project, seat, role, bot, cred):
    identity = Identity(project=project, seat=seat)
    assert bot in identity.canonical_names(role), (
        f"{project}/{seat}: doctor would warn on a name the estate correctly minted"
    )
    assert cred in [p.name for p in identity.credential_candidates], (
        f"{project}/{seat}: the delivered credential path would not be found"
    )


def test_an_arch_bot_without_its_project_is_still_warned_on():
    """§7a: an arch bot appears in several channels, so a bare seat name there
    genuinely is ambiguous. The rule is unambiguity, not permissiveness."""
    identity = Identity(project="blocks", seat="arch")
    assert "arch" not in identity.canonical_names("arch")
    assert identity.canonical_names("arch") == ("blocks-arch",)


def test_a_component_bot_may_carry_its_project_too():
    """Verbose but unambiguous, so accepted rather than warned on — otherwise
    the estate would be forced into a rename it does not need."""
    identity = Identity(project="blocks", seat="blocks-service")
    assert "blocks-blocks-service" in identity.canonical_names("component")
