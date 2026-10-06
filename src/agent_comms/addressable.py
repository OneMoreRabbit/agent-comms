"""`GET /v0/addressable` — discovery, and nothing else.

**This module answers "which names exist and how would I reach them". It never
answers "may I send".** `POST /v0/resolve` at send time is the authorisation,
and nothing here is consulted by the send path — see the module-level test that
pins that separation.

Two properties of the ruled surface shape what this does (ADR-0013 Amendment 7):

- **`?from` is a CLAIM, not identity.** The directory evaluates the outcome for
  whoever is named, and anyone may name anyone: a nonsense `from` returns 200
  with an empty list (measured 2026-09-26). So an answer is *what the directory
  says about a claimed sender*, and every line this prints says so. Treating
  membership as permission would be the `seat status` trap one layer out —
  asking "can you?" and then acting, with the message lost in the gap where the
  two truths disagree.
- **Tokenless**, so the surface is public. Nothing read here is privileged, and
  nothing about it is logged as though it were.

**Nothing is cached.** A cache of this would be a second copy of a graph only
the directory can evaluate per caller, and the first thing a reader would do is
trust it. `routes.json` caches a different fact — this seat's OWN agents — and
the two stay apart.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .errors import CommsError
from .resolve import directory_address

CONTRACT = "0.1"


class AddressableUnavailable(CommsError):
    """The directory did not answer, so we do not know. Not 'nobody'.

    An empty list and an unanswered question read alike and mean opposite
    things: one says this agent may address nobody, the other says we could not
    find out. A seat that serves nobody is a real configuration, so the
    difference has to survive.
    """

    tag = "addressable-unavailable"
    exit_code = 1


@dataclass(frozen=True)
class Entry:
    """One addressable agent, as the directory describes it."""

    fqn: str
    lifecycle: str = ""
    delivery: str = ""
    channel: str = ""
    bot: str = ""
    components: tuple[str, ...] = ()
    repositories: tuple[str, ...] = ()

    def line(self) -> str:
        """One row a person reads. Names the agent first, because that is the
        address; the bot and channel are how, not who."""
        where = f"{self.channel}/{self.bot}" if self.channel and self.bot else "—"
        served = f"  serves {', '.join(self.repositories)}" if self.repositories else ""
        return (f"  {self.fqn:<44} {self.lifecycle:<12} {self.delivery:<7} "
                f"{where}{served}")


@dataclass
class Answer:
    """What the directory said about a claimed sender."""

    claimed_from: str
    entries: tuple[Entry, ...] = ()
    contract: str = ""
    #: The `from` the DIRECTORY echoed back, which is the claim as it read it.
    echoed_from: str = ""
    warnings: list[str] = field(default_factory=list)


def _entry(row: dict) -> Entry:
    comms = ((row.get("transports") or {}).get("comms") or {})
    components = tuple(c.get("address", "") for c in (row.get("components") or [])
                       if c.get("address"))
    repos = tuple(sorted({r for c in (row.get("components") or [])
                          for r in (c.get("repositories") or [])}))
    return Entry(fqn=row.get("fqn", ""), lifecycle=row.get("lifecycle", ""),
                 delivery=row.get("delivery", ""), channel=comms.get("channel", ""),
                 bot=comms.get("bot", ""), components=components, repositories=repos)


def fetch(from_fqn: str = "", timeout: float = 10.0) -> Answer:
    """Read the addressable set, optionally as claimed by `from_fqn`.

    No credential is sent: the surface is tokenless by ruling, and sending one
    anyway would imply this answer is scoped to us when it is not.
    """
    address = directory_address()
    if not address:
        raise AddressableUnavailable(
            "no estate-directory is configured on this seat "
            "(~/.secrets/estate-directory-address), so there is nothing to ask. "
            "This is not an empty answer — it is no answer.")

    url = f"{address.rstrip('/')}/v0/addressable"
    if from_fqn.strip():
        url += f"?from={urllib.parse.quote(from_fqn.strip(), safe='')}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AddressableUnavailable(
            f"the directory answered {exc.code} for the addressable set. We do not "
            f"know who is addressable, which is not the same as nobody being.") from None
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
        raise AddressableUnavailable(
            f"the directory did not answer ({exc}). We do not know who is "
            f"addressable, which is not the same as nobody being.") from None

    if not isinstance(body, dict) or "addressable" not in body:
        raise AddressableUnavailable(
            "the directory answered something that is not an addressable set. "
            "Refused rather than read as empty.")

    rows = [_entry(r) for r in (body.get("addressable") or []) if r.get("fqn")]
    answer = Answer(claimed_from=from_fqn.strip(), entries=tuple(rows),
                    contract=str(body.get("contract") or ""),
                    echoed_from=str(body.get("from") or ""))
    if answer.contract and answer.contract != CONTRACT:
        # Read it anyway and say so: a contract we have not reviewed is a fact
        # about the estate, not a reason to refuse a read-only lookup.
        answer.warnings.append(
            f"the directory answered addressable contract {answer.contract}; this "
            f"client was written against {CONTRACT}. Read as-is.")
    if answer.claimed_from and answer.echoed_from \
            and answer.echoed_from.strip().casefold() != answer.claimed_from.casefold():
        answer.warnings.append(
            f"asked as {answer.claimed_from!r} and the directory echoed "
            f"{answer.echoed_from!r} — it read the claim differently than it was made.")
    return answer
