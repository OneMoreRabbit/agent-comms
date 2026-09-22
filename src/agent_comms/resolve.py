"""Turning a name into a route — the per-send half of comms-design §4a.

**Two layers, ruled apart 2026-09-22.** *Resolution* is per send; *configuration*
is the periodic GET that rewrites the local file. This module is resolution only.
It never writes config and never asks whether anything is alive.

Three properties this module exists to hold:

1. **The directory is never *required* on the hot path.** Unreachable means we
   answer from the local agent set — and we **say the answer came from cache**.
   Degraded resolution is labelled, never refused.
2. **An auth failure is terminal.** Never retried, at any level. A retry loop on
   one bad credential can get the estate's shared IP banned, which would take
   out every seat at once. The directory's own 401 already says
   `retryable: false`; this client would refuse to retry it even if it did not.
3. **No liveness, ever.** A resolution answers *where a name lives*. Whether
   anything is running there is `seat msg`'s answer and nobody else's.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


#: Where the orchestrator drops the directory's address. Absent = no directory
#: configured, which is a supported deployment and not a fault (§4a: "resolve
#: from the local config's agent set").
ADDRESS_FILE = Path.home() / ".secrets" / "estate-directory-address"
CREDENTIAL_FILE = Path.home() / ".secrets" / "estate-directory-token"

#: How long a directory answer may be reused. Short on purpose: §4a allows a
#: cache, and the DNS pattern it names relies on failure to invalidate, so the
#: TTL is a backstop rather than the mechanism.
CACHE_TTL = timedelta(seconds=60)

CONTRACT = "0.1"

#: Where an answer came from. This is not decoration — a consumer that cannot
#: tell a live answer from a cached one will eventually report a stale route as
#: current, which is the class of failure this whole design exists to remove.
FROM_DIRECTORY = "directory"
FROM_CACHE = "cache"
FROM_LOCAL = "local"


@dataclass
class Resolution:
    """Where a name lives. Never whether anything is running there."""

    success: bool
    status: str
    requested: str
    canonical_id: str = ""
    seat: str = ""
    host: str = ""
    local_route: str = ""
    delivery: str = ""
    transports: dict = field(default_factory=dict)
    permissions: dict = field(default_factory=dict)
    route_revision: int | None = None
    near_misses: tuple[str, ...] = ()
    message: str = ""
    #: `directory`, `cache` or `local` — §4a requires a degraded answer to say so.
    source: str = FROM_DIRECTORY
    #: Why we fell back, in the seat's own words. Empty when we did not.
    #: "Did not answer" and "refused our credential" are different faults with
    #: different remedies, and one label for both would hide which.
    reason: str = ""

    @property
    def degraded(self) -> bool:
        """Did this answer come from something other than the directory itself?"""
        return self.source != FROM_DIRECTORY

    @property
    def retryable(self) -> bool:
        """Is it worth asking again later?

        `not-registered` is the only refusal that can become a success without
        anyone re-asking us: a route can appear. It is retryable **inside our own
        bounds** (3 attempts, 24h) and not forever — an unbounded retryable
        refusal is a flood source, and we have had one of those.

        `unknown` and `not-permitted` are answers, not outages.
        """
        return self.status == "not-registered"

    def label(self) -> str:
        """One line naming the answer, where it came from, and why."""
        if self.source == FROM_DIRECTORY:
            where = ""
        elif self.source == FROM_LOCAL:
            where = " (from local config — no directory is configured)"
        else:
            why = self.reason or "the directory did not answer"
            where = f" (from cache — {why})"
        who = self.canonical_id or self.requested
        return f"{self.status}: {who}{where}"


def directory_address() -> str:
    """The directory's address, or empty when none is configured.

    Absence is **not** a §11 defect: comms-design §4a names running without a
    directory as a supported shape, and the fallback is declared, not invented.
    What would be a defect is answering from the fallback without saying so —
    which is why `Resolution.source` exists and is never optional.
    """
    try:
        return ADDRESS_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _credential() -> str:
    try:
        return CREDENTIAL_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _post(address: str, payload: dict, timeout: float) -> tuple[int, dict]:
    """One call. Returns (http status, decoded body) or raises for transport faults."""
    request = urllib.request.Request(
        f"{address.rstrip('/')}/v0/resolve",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    token = _credential()
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"error": "unreadable", "message": body[:300]}


class Resolver:
    """Per-send resolution, with the degraded path labelled rather than refused.

    Holds a short-lived cache keyed on the requested name. `invalidate()` is the
    DNS pattern §4a names: a delivery failure from the seat (`unknown-agent`) is
    what proves a cached route wrong, and proving it wrong is what evicts it.
    """

    def __init__(self, local_agents: dict[str, dict] | None = None, timeout: float = 5.0):
        #: The agent set from the local config — the periodic layer's output, and
        #: the fallback when the directory cannot be reached.
        self.local_agents = local_agents or {}
        self.timeout = timeout
        self._cache: dict[str, tuple[datetime, Resolution]] = {}
        #: Set once if the directory refuses our credential. Non-empty means every
        #: answer from here is from local config, and `comms doctor` must FAIL.
        self.auth_failure: str = ""

    def invalidate(self, name: str) -> None:
        """Forget a cached answer. Called when the seat says `unknown-agent`."""
        self._cache.pop(name, None)

    def resolve(self, target: str, caller: str, purpose: str = "comms") -> Resolution:
        address = directory_address()
        if not address:
            return self._from_local(target, FROM_LOCAL)
        if self.auth_failure:
            # Asked once, refused, never asked again. This is the whole of what
            # "terminal" buys: one failed call per process, not one per message.
            return self._from_local(target, FROM_CACHE, "the credential was refused")

        cached = self._cache.get(target)
        if cached and datetime.now(tz=timezone.utc) - cached[0] < CACHE_TTL:
            return cached[1]

        payload = {"kind": "resolution-request", "contract": CONTRACT,
                   "caller": caller, "target": target, "purpose": purpose}
        try:
            code, body = _post(address, payload, self.timeout)
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
            # The directory is not answering. This is exactly the case §4a says
            # must degrade rather than refuse — so answer from what we have and
            # SAY it is cached. Never raise: an unreachable directory must not
            # stop a seat being able to talk.
            return self._from_local(target, FROM_CACHE, "the directory did not answer")

        if code in (401, 403):
            # TERMINAL means STOP ASKING THE DIRECTORY. It does not mean stop
            # delivering. Two rules meet here and both hold:
            #   - never retry an auth failure (a credential retry loop can get the
            #     estate's shared address banned, taking out every seat at once);
            #   - §4a: degraded resolution is LABELLED, never refused.
            # So the directory is switched off for this process and we answer from
            # local config marked as cache. `auth_failure` is what makes that
            # honest: constitution §11's carve-out permits running without a
            # capability only when its absence is reported unmistakably at every
            # surface that would otherwise imply it works — `comms doctor` FAILs
            # on this, and every answer carries `source=cache`.
            self.auth_failure = (
                f"the estate-directory refused this seat's credential ({code}: "
                f"{body.get('message') or body.get('error') or 'no reason given'}). "
                "Not retried — a retry loop on one bad credential can get the "
                "estate's shared address banned, which takes out every seat rather "
                "than this one. A person must issue the credential. Until then "
                "resolution answers from local config, labelled as cache."
            )
            return self._from_local(target, FROM_CACHE, "the credential was refused")

        if code >= 500 or code == 426:
            # The directory is there and cannot serve us. Same treatment as
            # unreachable: degrade, label, never refuse.
            return self._from_local(target, FROM_CACHE, f"the directory answered {code}")

        resolution = _read(target, body)
        if resolution.success:
            self._cache[target] = (datetime.now(tz=timezone.utc), resolution)
        return resolution

    def _from_local(self, target: str, source: str, reason: str = "") -> Resolution:
        """Answer from the agent set the periodic layer last wrote.

        A miss here is `unknown` and says so — it does not become `not-registered`
        (a lifecycle fact we cannot know offline) and it never becomes
        `no-session` (the seat's word, and only the seat's).
        """
        record = self.local_agents.get(target)
        if record is None:
            near = sorted(n for n in self.local_agents if target.lower() in n.lower())
            return Resolution(
                success=False, status="unknown", requested=target, source=source,
                reason=reason,
                near_misses=tuple(near[:5]),
                message=(f"no agent or alias named '{target}' is in this seat's local "
                         "agent set, and the directory did not answer"
                         if source == FROM_CACHE else
                         f"no agent or alias named '{target}' is configured here"),
            )
        return Resolution(
            success=True, status="resolved", requested=target, source=source,
            reason=reason,
            canonical_id=record.get("id", target),
            seat=record.get("seat", ""), host=record.get("host", ""),
            local_route=record.get("local_route", ""),
            delivery=record.get("delivery", ""),
            transports=record.get("transports", {}) or {},
            permissions=record.get("permissions", {}) or {},
        )


def _read(target: str, body: dict) -> Resolution:
    """Read the directory's answer. Unknown shapes fail closed, never open."""
    if not isinstance(body, dict):
        return Resolution(success=False, status="unknown", requested=target,
                          message="the directory returned something that is not an answer")

    if body.get("success"):
        route = body.get("route") or {}
        return Resolution(
            success=True, status=str(body.get("status") or "resolved"), requested=target,
            canonical_id=str(body.get("canonical_id") or ""),
            seat=str(route.get("seat") or ""), host=str(route.get("host") or ""),
            local_route=str(route.get("local_route") or ""),
            delivery=str(body.get("delivery") or ""),
            transports=body.get("transports") or {},
            permissions=body.get("permissions") or {},
            route_revision=body.get("route_revision"),
        )

    return Resolution(
        success=False,
        status=str(body.get("status") or body.get("error") or "unknown"),
        requested=str(body.get("requested") or target),
        near_misses=tuple(body.get("near_misses") or ()),
        message=str(body.get("message") or ""),
    )
