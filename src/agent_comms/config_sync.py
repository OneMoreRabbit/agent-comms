"""The periodic half of comms-design §4a — configuration, not resolution.

**Two layers, ruled apart 2026-09-22.** *Resolution* is per send and lives in
`resolve.py`. *Configuration* is this: a periodic GET of the slow-changing
facts — the agent set, delivery modes, transports, permissions — written to a
local file that comms always boots from.

Three properties, each one ruled:

- **The file is always the boot source.** The directory is its upstream, never
  its replacement. A seat with no directory runs from the file and is not
  degraded; that is a supported shape, not a fault.
- **The rewrite is atomic and stamped.** `generation` and `fetched_at` say
  which version is held and when it arrived, so staleness is visible rather
  than inferred.
- **A refresh REPLACES the set; it is not merged.** The directory's answer is
  authoritative (estate-directory-registration 0.2, and the operator's
  master ruling 2026-09-23: if a local record and the directory disagree, the
  directory wins and the local record is never the tiebreaker). Merging would
  make this seat the second authority and keep a withdrawn agent alive locally
  forever.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .resolve import CREDENTIAL_FILE, FALLBACK_CREDENTIAL_FILE, directory_address

#: Where the fetched set lives. `doctor` already reads this for its
#: grant-versus-subscription drift check, which is inert until something writes it.
ROUTES_FILE = "routes.json"


@dataclass
class Fetched:
    """What one refresh did, for `comms config show` and for a person."""

    generation: int
    fetched_at: str
    agents: int
    source: str
    reason: str = ""

    def line(self) -> str:
        if self.source == "directory":
            return (f"generation {self.generation}, {self.agents} agent(s), "
                    f"fetched {self.fetched_at}")
        return f"from file — {self.reason or 'no directory configured'}"


def _credential() -> str:
    for path in (CREDENTIAL_FILE, FALLBACK_CREDENTIAL_FILE):
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if token:
            return token
    return ""


def seat_path(project: str, seat: str) -> str:
    """The seat-facing door. **Project-qualified, measured 2026-09-23.**

    `/v0/seats/agent-eco/test-claude/assignments` answers 200; the bare seat
    name answers 403 `credential cannot read 'test-claude'`. `/v0/routes` is
    the OPERATOR view and refuses a seat credential — a seat reading its own
    configuration uses this door, not that one.
    """
    return f"/v0/seats/{project}/{seat}/assignments"


def fetch(project: str, seat: str, state_dir: Path, timeout: float = 10.0) -> Fetched:
    """GET this seat's assignments and rewrite the local file atomically.

    Never raises on an unreachable directory: the file is the boot source and
    an outage must not stop a seat working from what it already has.
    """
    target = state_dir / ROUTES_FILE
    address = directory_address()
    if not address:
        return _from_file(target, "no directory configured")

    request = urllib.request.Request(
        f"{address.rstrip('/')}{seat_path(project, seat)}",
        headers={"Accept": "application/json"})
    token = _credential()
    if token:
        request.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return _from_file(target, f"the directory answered {exc.code}")
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return _from_file(target, "the directory did not answer")

    if not isinstance(body, dict) or "assignments" not in body:
        # Fail closed. An answer we cannot read must never replace a set we can.
        return _from_file(target, "the directory answered something that is not an assignment set")

    fetched_at = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    record = {"contract": body.get("contract", ""),
              "generation": body.get("generation", 0),
              "fetched_at": fetched_at,
              "source": "directory",
              "routes": _complete(body.get("assignments") or [],
                                  caller=f"bakehouse.{project}.{seat}")}
    _write_atomic(target, record)
    return Fetched(generation=record["generation"], fetched_at=fetched_at,
                   agents=len(record["routes"]), source="directory")


def _complete(assignments: list, caller: str) -> list:
    """The assignment list, with anything the directory left out filled in.

    **The assignments answer now carries the whole record** -- agent, label,
    runtime, delivery, transports and permissions -- since the directory was
    extended on 2026-09-25. One call for one seat's agents, which is what the
    cache is for.

    Before that it carried no transports and no permissions, so this resolved
    each agent separately to complete the record. That fallback stays for a
    directory that has not been extended yet: a seat cached half its own
    configuration and enforced none of the rest, and a blocked sender was
    delivered (UC-03, measured). It costs nothing when the fields are present.

    **An agent that will not resolve keeps whatever the assignment gave.** A
    failed lookup is not a statement that a value is absent, and treating it as
    one would quietly widen a permission the moment the directory hiccuped.
    """
    missing = [a for a in assignments
               if a.get("agent") or a.get("id")
               if "transports" not in a or "permissions" not in a]
    if not missing:
        return [dict(a) for a in assignments]

    from .resolve import Resolver
    resolver = Resolver()
    out = []
    for record in assignments:
        record = dict(record)
        fqn = record.get("agent") or record.get("id")
        if fqn and ("transports" not in record or "permissions" not in record):
            try:
                answer = resolver.resolve(fqn, caller=caller)
            except Exception:  # noqa: BLE001 - a sync must not die on one agent
                answer = None
            if answer is not None and answer.success:
                if answer.delivery:
                    record["delivery"] = answer.delivery
                record.setdefault("transports", answer.transports or {})
                record.setdefault("permissions", answer.permissions or {})
        out.append(record)
    return out


def _write_atomic(target: Path, record: dict) -> None:
    """Write-then-rename, so a torn write cannot leave a half-set on disk.

    A partially written routing file is worse than a stale one: stale is
    visible in `fetched_at`, torn is silent and arbitrary.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def _from_file(target: Path, reason: str) -> Fetched:
    held = load(target.parent)
    return Fetched(generation=held.get("generation", 0),
                   fetched_at=held.get("fetched_at", ""),
                   agents=len(held.get("routes") or []),
                   source="file", reason=reason)


def load(state_dir: Path) -> dict:
    """The held set. Absent or unreadable is an empty set, never an error."""
    try:
        held = json.loads((state_dir / ROUTES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return held if isinstance(held, dict) else {}


def agent_set(state_dir: Path) -> dict[str, dict]:
    """The held set keyed by FQN, in the shape `Resolver` takes as its fallback.

    This is what makes the fallback real. Until something wrote this file,
    `local_agents` was empty on every seat and the degraded path answered
    `unknown` for everything — compliant, and untested against a populated set.
    """
    out: dict[str, dict] = {}
    for record in load(state_dir).get("routes") or []:
        fqn = record.get("agent") or record.get("id")
        if fqn:
            out[fqn] = {"id": fqn, "delivery": record.get("delivery", ""),
                        "label": record.get("label", ""),
                        "transports": record.get("transports") or {},
                        "permissions": record.get("permissions") or {}}
    return out
