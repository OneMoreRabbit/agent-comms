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

import pathlib

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
                                          source="directory"))
    assert identity.bot_name == bot
    assert identity.credential_candidates[0].name == cred, \
        "the credential is named after the BOT, so reading the bot finds it first"


@pytest.mark.parametrize("project,seat,bot,cred", DEPLOYED)
def test_there_is_no_template_left_to_get_them_wrong(project, seat, bot, cred):
    """**The template is gone (2026-09-28), not merely second in line.**

    This used to assert that `<project>-<seat>` produced the WRONG name for
    most of the estate — which it does — and then keep it as the pre-sync
    fallback anyway. The operator's ruling ended that: FQNs and declared names
    only, nothing constructed, including on a seat that has never synced.

    An unsynced seat now has NO bot name. That is an answer, not a gap: the
    directory has not said, and inventing one names an account that may not
    exist.
    """
    identity = Identity(project=project, seat=seat)      # nothing declared
    assert identity.bot_name == "", (
        f"a bot name was constructed for an unsynced {project}/{seat}: "
        f"{identity.bot_name!r}")


def test_an_unsynced_seat_DISCOVERS_its_credential_rather_than_spelling_it(tmp_path,
                                                                           monkeypatch):
    """A seat on its first install must still reach the hub — and it does so by
    looking, not by guessing what the file is called.

    **The old pair of spellings could both be wrong.** `zuliprc-<seat>` and
    `zuliprc-<project>-<seat>` were tried in order; a deployer that names the
    file anything else — `zuliprc-blocks-arch-bot`, say — defeated both, and
    the seat could not start. The deployer places exactly one credential, so
    there is nothing to guess.
    """
    secrets = tmp_path / ".secrets"
    secrets.mkdir()
    (secrets / "zuliprc-blocks-arch-bot").write_text("[api]\nemail=a@b\nkey=k\nsite=s\n")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))

    identity = Identity(project="blocks", seat="arch")
    assert identity.declared.source == "unread"
    assert identity.bot_name == "", "nothing is constructed before the directory speaks"
    found = [p.name for p in identity.credential_candidates]
    assert found == ["zuliprc-blocks-arch-bot"], found

    # The near-miss: neither old template would have found that file.
    assert "zuliprc-arch" not in found and "zuliprc-blocks-arch" not in found

    # Several credentials is a real ambiguity, so all are offered in a stable
    # order for a person to settle — never one picked as a favourite.
    (secrets / "zuliprc-other").write_text("[api]\nemail=a@b\nkey=k\nsite=s\n")
    both = [p.name for p in Identity(project="blocks", seat="arch").credential_candidates]
    assert both == ["zuliprc-blocks-arch-bot", "zuliprc-other"], both


def test_known_names_is_for_recognising_ourselves_not_for_posting():
    """Two jobs, and conflating them produced canonical_names(role). Posting
    takes the ONE declared bot; recognising ourselves accepts every name other
    parties may already have written in a topic or a mention."""
    identity = Identity(project="agent-eco", seat="test-claude",
                        declared=Declared(bot="test-claude", channel="seat-testing",
                                          source="directory"))
    assert identity.bot_name == "test-claude"            # posting: one value
    known = {n.casefold() for n in identity.known_names()}
    assert "test-claude" in known
    # ...and nothing invented: the project-prefixed form is NOT offered once the
    # directory has spoken, because the estate did not mint it.
    assert "agent-eco-test-claude" not in known


def test_a_seat_carries_no_FQN_at_all():
    """**A seat has no FQN**, so nothing holds one against a seat.

    `Declared` briefly did — first preferring "the agent whose last segment is
    the seat name" (deriving an FQN from a seat name, the exact defect the class
    exists to remove), then "the one agent this seat serves". Both were rejected:
    an FQN names an agent session, so a seat-level FQN is a category error
    however it is filled, and keeping one invites the next reader to treat a
    seat as addressable.

    The sending agent states itself with `--from`."""
    from agent_comms.config import Declared, Identity
    assert not hasattr(Declared(), "fqn"), "a seat-level FQN must not exist"
    assert not hasattr(Identity(project="agent-eco", seat="test-claude"), "fqn")
