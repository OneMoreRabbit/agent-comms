"""Resolution — comms-design §4a, and the rules that were bought by incidents.

Every test here fails against a client with no resolver at all, which is the
state before this module. They are written against the BEHAVIOUR the design
names, not against the implementation.
"""

from __future__ import annotations

import urllib.error

import pytest

from agent_comms import resolve as R

AGENTS = {"arch": {"id": "bakehouse.agent-eco.arch", "seat": "agent-eco/arch",
                   "delivery": "inject"}}


@pytest.fixture
def directory(monkeypatch, tmp_path):
    """A configured directory address, so the fallback paths are reachable."""
    address = tmp_path / "address"
    address.write_text("http://directory.invalid:8040")
    monkeypatch.setattr(R, "ADDRESS_FILE", address)
    monkeypatch.setattr(R, "CREDENTIAL_FILE", tmp_path / "absent-token")
    return address


def _refuses(code, body):
    def fake(address, payload, timeout):
        return code, body
    return fake


def test_an_unreachable_directory_degrades_and_says_so(directory, monkeypatch):
    """§4a: degraded resolution is LABELLED, never refused.

    An unreachable directory must not stop a seat being able to talk, and must
    not quietly look like a live answer either.
    """
    def boom(*a, **k):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(R, "_post", boom)
    answer = R.Resolver(local_agents=AGENTS).resolve("arch", caller="c")

    assert answer.success is True
    assert answer.degraded is True
    assert answer.source == R.FROM_CACHE
    assert "did not answer" in answer.label()


def test_a_refused_credential_is_terminal_but_still_delivers(directory, monkeypatch):
    """Terminal means STOP ASKING THE DIRECTORY, not stop delivering.

    Both rules hold at once: never retry an auth failure (a credential retry
    loop can get the estate's shared address banned, taking out every seat), and
    never refuse where the design says degrade.
    """
    monkeypatch.setattr(R, "_post", _refuses(401, {"error": "unauthenticated",
                                                   "message": "a bearer credential is required",
                                                   "retryable": False}))
    resolver = R.Resolver(local_agents=AGENTS)
    answer = resolver.resolve("arch", caller="c")

    assert answer.success is True, "a refused credential must not stop delivery"
    assert answer.source == R.FROM_CACHE
    assert "credential was refused" in answer.label()
    assert resolver.auth_failure, "the failure must be recorded for doctor to FAIL on"


def test_the_directory_is_asked_once_after_a_refusal_never_again(directory, monkeypatch):
    """One failed call per process, not one per message. That is what terminal buys."""
    calls = {"n": 0}

    def counting(address, payload, timeout):
        calls["n"] += 1
        return 401, {"error": "unauthenticated"}

    monkeypatch.setattr(R, "_post", counting)
    resolver = R.Resolver(local_agents=AGENTS)
    for _ in range(5):
        resolver.resolve("arch", caller="c")

    assert calls["n"] == 1, f"asked the directory {calls['n']} times after a refusal"


def test_the_two_degradations_are_told_apart(directory, monkeypatch):
    """'Did not answer' and 'refused our credential' need different remedies.

    One label for both would hide which, and a person would go looking in the
    wrong place. Constitution §9: a signal must carry information.
    """
    monkeypatch.setattr(R, "_post", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    unreachable = R.Resolver(local_agents=AGENTS).resolve("arch", caller="c").label()

    monkeypatch.setattr(R, "_post", _refuses(403, {"error": "caller-mismatch"}))
    refused = R.Resolver(local_agents=AGENTS).resolve("arch", caller="c").label()

    assert unreachable != refused
    assert "did not answer" in unreachable
    assert "credential was refused" in refused


def test_no_directory_configured_is_not_a_fault(monkeypatch, tmp_path):
    """A seat with no directory is a supported shape, not a degraded one."""
    monkeypatch.setattr(R, "ADDRESS_FILE", tmp_path / "absent")
    answer = R.Resolver(local_agents=AGENTS).resolve("arch", caller="c")

    assert answer.success is True
    assert answer.source == R.FROM_LOCAL
    assert "no directory is configured" in answer.label()


def test_a_local_miss_is_unknown_and_never_borrows_another_layers_word(directory, monkeypatch):
    """`not-registered` is lifecycle we cannot know offline; `no-session` is the
    seat's word alone. A miss here is `unknown` and stays `unknown`."""
    monkeypatch.setattr(R, "_post", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    answer = R.Resolver(local_agents=AGENTS).resolve("nobody", caller="c")

    assert answer.status == "unknown"
    assert answer.status not in ("not-registered", "no-session")


def test_only_not_registered_is_retryable():
    """An unbounded retryable refusal is a flood source; we have had one."""
    assert R.Resolution(False, "not-registered", "x").retryable is True
    assert R.Resolution(False, "unknown", "x").retryable is False
    assert R.Resolution(False, "not-permitted", "x").retryable is False


def test_a_delivery_failure_evicts_the_cached_route(directory, monkeypatch):
    """The DNS pattern §4a names: failure IS the cache invalidation."""
    calls = {"n": 0}

    def answering(address, payload, timeout):
        calls["n"] += 1
        return 200, {"kind": "resolution-result", "contract": "0.1",
                     "success": True, "status": "resolved",
                     "canonical_id": "bakehouse.agent-eco.arch",
                     "route": {"seat": "agent-eco/arch"}, "route_revision": 12}

    monkeypatch.setattr(R, "_post", answering)
    resolver = R.Resolver(local_agents=AGENTS)
    resolver.resolve("arch", caller="c")
    resolver.resolve("arch", caller="c")
    assert calls["n"] == 1, "the second call should have been served from cache"

    resolver.invalidate("arch")          # the seat answered unknown-agent
    resolver.resolve("arch", caller="c")
    assert calls["n"] == 2, "an invalidated route must be resolved again"


def test_a_directory_answer_is_never_marked_degraded(directory, monkeypatch):
    """The label has to be true in both directions, or it means nothing."""
    monkeypatch.setattr(R, "_post", lambda *a, **k: (200, {
        "kind": "resolution-result", "contract": "0.1",
        "success": True, "status": "resolved", "canonical_id": "bakehouse.agent-eco.arch",
        "route": {"seat": "agent-eco/arch", "host": "otter", "local_route": "arch"},
        "delivery": "inject", "route_revision": 12}))
    answer = R.Resolver(local_agents=AGENTS).resolve("arch", caller="c")

    assert answer.degraded is False
    assert answer.source == R.FROM_DIRECTORY
    assert answer.label().endswith("bakehouse.agent-eco.arch")
    assert answer.local_route == "arch" and answer.route_revision == 12


def test_the_credential_file_is_the_name_the_estate_declares():
    """`estate-directory-read`, beside `estate-directory-address`.

    It was `estate-directory-token` for one commit — invented because it sounded
    right. A wrong filename here fails SILENTLY and in the worst direction: the
    seat reads "no credential issued" forever while the real one sits on disk
    next to it, and every answer is degraded with nobody able to see why.
    Constitution §11 — the estate declares this, we do not guess it.
    """
    assert R.CREDENTIAL_FILE.name == "estate-directory-read"
    assert R.ADDRESS_FILE.name == "estate-directory-address"
    assert R.CREDENTIAL_FILE.parent == R.ADDRESS_FILE.parent


@pytest.mark.parametrize("code,expect", [
    (404, "resolution is not being served"),
    (426, "does not support our resolution contract"),
    (503, "answered 503"),
])
def test_a_directory_that_cannot_serve_us_degrades_with_its_own_reason(
        directory, monkeypatch, code, expect):
    """Endpoint absent, wrong contract version and a fault are three faults.

    None is retryable and none may refuse — §4a degrades and labels. They get
    different words because they have different remedies.
    """
    monkeypatch.setattr(R, "_post", _refuses(code, {"error": "x"}))
    answer = R.Resolver(local_agents=AGENTS).resolve("arch", caller="c")

    assert answer.success is True
    assert answer.source == R.FROM_CACHE
    assert expect in answer.label()


def test_an_unrecognised_answer_never_becomes_a_route(directory, monkeypatch):
    """Fail closed. Reading an error body as a route is how a message goes
    somewhere nobody chose."""
    monkeypatch.setattr(R, "_post", _refuses(200, {"totally": "unexpected"}))
    answer = R.Resolver(local_agents=AGENTS).resolve("arch", caller="c")

    assert answer.source == R.FROM_CACHE
    assert "not a resolution" in answer.label()


# -- the short-name/FQN vocabulary wrinkle (arch, 2026-09-22) ------------------
#
# The live register spells permissions.comms.partners in SHORT seat names while
# addressing is by FQN. Until one spelling wins, understanding only one of them
# silently refuses half the estate.

from agent_comms.directory import Directory  # noqa: E402


def test_a_partner_matches_in_both_spellings():
    d = Directory(project=False, partners=("agent-eco-arch",), source="test")
    assert d.permits("agent-eco-arch", in_project=False) is True
    assert d.permits("bakehouse.agent-eco.agent-eco-arch", in_project=False) is True


def test_a_short_name_never_matches_across_projects():
    """An alias never spans a project, so neither may a permission."""
    d = Directory(project=False, partners=("bakehouse.arc-web.arch",), source="test")
    assert d.permits("bakehouse.arc-web.arch", in_project=False) is True
    assert d.permits("bakehouse.labs.arch", in_project=False) is False


def test_matching_is_the_last_segment_and_never_a_prefix():
    """`arch` must not admit `arch-shadow`. A permission that matches loosely is
    a permission nobody declared."""
    d = Directory(project=False, partners=("arch",), source="test")
    assert d.permits("arch", in_project=False) is True
    assert d.permits("arch-shadow", in_project=False) is False
    assert d.permits("shadow-arch", in_project=False) is False


def test_blocked_wins_in_either_spelling():
    d = Directory(project=True, partners=("agent-eco-arch",),
                  blocked=("agent-eco-arch",), source="test")
    assert d.permits("agent-eco-arch", in_project=True) is False
    assert d.permits("bakehouse.agent-eco.agent-eco-arch", in_project=True) is False


def test_the_reason_is_not_told_twice_and_differently(directory, monkeypatch):
    """`message` and `label()` must agree on WHY we degraded.

    `message` hardcoded "the directory did not answer" while `label()` carried
    the real cause, so a refused credential reported itself as an unreachable
    directory — and a person would have gone looking at the network instead of
    at their token. Found by running it against the live service; a fixture
    would never have caught it, because a fixture agrees with itself.
    """
    monkeypatch.setattr(R, "_post", _refuses(401, {"error": "unauthenticated"}))
    answer = R.Resolver(local_agents={}).resolve("nobody", caller="c")

    assert "credential was refused" in answer.label()
    assert "credential was refused" in answer.message
    assert "did not answer" not in answer.message


def test_no_directory_configured_is_not_degraded(monkeypatch, tmp_path):
    """A standalone seat is a supported shape, not a fault.

    Marking it degraded fires a warning on every resolution it ever makes —
    constitution §9's "speech when it should be silent", which teaches a reader
    to ignore the ones that matter. Only `cache` means we meant to ask the
    directory and could not.
    """
    monkeypatch.setattr(R, "ADDRESS_FILE", tmp_path / "absent")
    answer = R.Resolver(local_agents=AGENTS).resolve("arch", caller="c")

    assert answer.source == R.FROM_LOCAL
    assert answer.degraded is False


def test_a_fallback_after_a_real_failure_IS_degraded(directory, monkeypatch):
    """The other direction has to hold too, or the flag means nothing."""
    monkeypatch.setattr(R, "_post", _refuses(401, {"error": "unauthenticated"}))
    assert R.Resolver(local_agents=AGENTS).resolve("arch", caller="c").degraded is True
