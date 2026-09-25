"""P2 — the estate's actual names, READ from the directory rather than derived.

The collation's evidence: doctor warned persistently on `blocks-service` and on
`zuliprc-blocks-service`, both of which are **correct**. A warning that fires on
correct configuration is §9's second failure.

**Rewritten 2026-09-26.** The old fix was `canonical_names(role)` — accept two
spellings, because the bot name was a template (`<project>-<seat>`) that is
wrong for most of the estate. The real fix is to stop deriving it: the
addressing model is channel↔seat, bot↔seat, FQN↔agent, and the directory holds
all three. These cases now pin that reading it yields the estate's real names —
including the four of six the template gets wrong.
"""

from __future__ import annotations

import pytest

from agent_comms.config import Declared, Identity

# (project, seat, the bot the estate actually minted, the credential delivered)
DEPLOYED = [
    ("blocks", "blocks-service", "blocks-service", "zuliprc-blocks-service"),
    ("blocks", "blocks-android", "blocks-android", "zuliprc-blocks-android"),
    ("blocks", "arch", "blocks-arch", "zuliprc-blocks-arch"),
    ("agent-eco", "agent-comms", "agent-comms", "zuliprc-agent-comms"),
    ("orient", "app", "app", "zuliprc-app"),
    ("orient", "arch", "orient-arch", "zuliprc-orient-arch"),
    # Measured 2026-09-25, and the case the template cannot express: a seat in
    # project `agent-eco` whose channel is `seat-testing`.
    ("agent-eco", "test-claude", "test-claude", "zuliprc-test-claude"),
]


@pytest.mark.parametrize("project,seat,bot,cred", DEPLOYED)
def test_the_declared_bot_is_the_estates_real_name(project, seat, bot, cred):
    """One value, read. No set, no role, no spelling rule."""
    identity = Identity(project=project, seat=seat,
                        declared=Declared(bot=bot, channel="irrelevant-here",
                                          fqn=f"bakehouse.{project}.{seat}",
                                          source="directory"))
    assert identity.bot_name == bot
    assert identity.credential_candidates[0].name == cred, \
        "the credential is named after the BOT, so reading the bot finds it first"


@pytest.mark.parametrize("project,seat,bot,cred", DEPLOYED)
def test_the_template_would_have_got_these_wrong(project, seat, bot, cred):
    """The near-miss that justifies the change: for most of the estate the old
    `<project>-<seat>` template does NOT produce the minted bot name, which is
    why a second accepted spelling had to be invented."""
    templated = f"{project}-{seat}"
    if templated == bot:
        pytest.skip(f"{project}/{seat} is the one shape the template happens to fit")
    identity = Identity(project=project, seat=seat)      # nothing declared
    assert identity.bot_name == templated
    assert identity.bot_name != bot, \
        "this is the case the template gets wrong and the directory gets right"


def test_an_unsynced_seat_still_finds_itself_and_says_so():
    """A seat with no cache must still name itself and locate its credential,
    or a fresh container cannot start. The template survives ONLY here, and
    `Declared.source` records that it was not read."""
    identity = Identity(project="blocks", seat="arch")
    assert identity.declared.source == "unread"
    assert identity.bot_name == "blocks-arch"            # template, pre-sync
    names = [p.name for p in identity.credential_candidates]
    assert "zuliprc-arch" in names and "zuliprc-blocks-arch" in names, \
        "both delivered spellings are tried before the seat has ever synced"


def test_known_names_is_for_recognising_ourselves_not_for_posting():
    """Two jobs, and conflating them produced canonical_names(role). Posting
    takes the ONE declared bot; recognising ourselves accepts every name other
    parties may already have written in a topic or a mention."""
    identity = Identity(project="agent-eco", seat="test-claude",
                        declared=Declared(bot="test-claude", channel="seat-testing",
                                          fqn="bakehouse.agent-eco.test-claude",
                                          source="directory"))
    assert identity.bot_name == "test-claude"            # posting: one value
    known = {n.casefold() for n in identity.known_names()}
    assert "test-claude" in known
    # ...and nothing invented: the project-prefixed form is NOT offered once the
    # directory has spoken, because the estate did not mint it.
    assert "agent-eco-test-claude" not in known
