"""The comms directory — who this seat may exchange messages with.

**The estate decides who talks to whom.** A seat reads this file; it does not
write it, and it cannot widen its own permissions from inside. That is the
property ADR-0009 §9 actually requires, and the reason the file is deployed
rather than configured locally.

```yaml
# ~/.comms/comms.yml — managed by the orchestrator, installed with comms
project: true                                    # every seat in my project channel
partners: [blocks-service, blocks-android, orchestrator]   # and these
blocked: []                                      # never these, whatever the above says
```

One list answers both directions: *may they message me* and *may I message them*.
A separate outbound rule would be a second place for the answer to live, and the
two would disagree the first time one was edited.

**Humans are never governed by it.** The directory is machine-to-machine policy;
an account that is not a bot is the operator, and refusing the operator is never
the right answer. `blocked` still overrides, so the escape hatch exists.

Absence is not an error — it means the default, `project: true` with no partners,
and `comms doctor` says so. A *malformed* file is refused loudly: a permission
list read halfway is worse than one not read at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import CommsError


class DirectoryUnreadable(CommsError):
    """The comms directory exists but could not be read.

    Loud on purpose. Every other unreadable thing in this client degrades to
    "undetermined, so hold"; a permission list cannot, because the safe
    interpretation is unknowable — fail open and the boundary is gone, fail
    closed and the seat goes silent with no explanation.
    """

    tag = "directory-unreadable"


@dataclass
class Directory:
    """Who this seat may exchange messages with."""

    #: Every seat in this seat's own project channel. The default.
    project: bool = True
    #: Named seats beyond the project — the cross-project exceptions.
    partners: tuple[str, ...] = ()
    #: Never permitted, whatever `project` or `partners` say.
    blocked: tuple[str, ...] = ()
    #: Where it came from, for `comms doctor`. Empty when running on the default.
    source: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def installed(self) -> bool:
        return bool(self.source)

    def permits(self, name: str, *, in_project: bool, is_human: bool = False) -> bool:
        """May this name exchange messages with this seat?

        `in_project` is answered by the hub — the subscriber list of this seat's
        channel — rather than by a second roster kept here. One source for the
        fact, which is the whole reason `project: true` needs no names.
        """
        folded = name.strip().casefold()
        if folded in {b.casefold() for b in self.blocked}:
            return False
        if is_human:
            return True
        if folded in {p.casefold() for p in self.partners}:
            return True
        return self.project and in_project

    def refusal(self, name: str, *, in_project: bool) -> str:
        """Why `name` was refused, in terms the estate can act on."""
        if name.strip().casefold() in {b.casefold() for b in self.blocked}:
            return f"'{name}' is blocked for this seat in {self.describe()}"
        where = "in this seat's project channel" if in_project else "outside this seat's project"
        return (
            f"'{name}' is not a permitted partner for this seat ({where}). "
            f"Permitted: {self.describe()}. The estate declares the link — a seat "
            "cannot widen its own permissions, which is the point of the rule."
        )

    def describe(self) -> str:
        parts = ["every seat in this project"] if self.project else []
        if self.partners:
            parts.append(", ".join(self.partners))
        if not parts:
            parts.append("nobody")
        line = "; ".join(parts)
        if self.blocked:
            line += f" (blocked: {', '.join(self.blocked)})"
        return line

    def summary(self) -> str:
        """One line for `comms doctor`."""
        if not self.installed:
            return (
                "no directory installed — running on the default (every seat in this "
                "project, no cross-project partners). The orchestrator installs "
                "~/.comms/comms.yml with comms; until it does, permissions are a "
                "default rather than a declaration."
            )
        return f"{self.describe()} — from {self.source}"


#: `key: value` at the top level. The file is three flat keys by design; anything
#: that needs a parser needs a discussion first.
_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")


def load(state_dir: Path | None = None) -> Directory:
    """Read the directory, or return the default if there is none.

    Parsed by hand, like the seat's own manifests: three flat keys, a bool and
    two lists. Adding a YAML dependency to read them would be the heavier
    choice, and the shape is small enough to validate strictly — which matters
    more here than anywhere else in this client.
    """
    root = state_dir or Path.home() / ".comms"
    path = Path(root) / "comms.yml"
    if not path.exists():
        return Directory()

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DirectoryUnreadable(
            f"{path} exists but could not be read: {exc}. This file says who may "
            "message this seat; refusing to run on a guess."
        ) from exc

    found: dict[str, object] = {}
    pending: str | None = None  # a key whose list is written as `- item` lines
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if pending and line.lstrip().startswith("-"):
            found.setdefault(pending, [])
            value = line.lstrip()[1:].strip().strip("'\"")
            if value:
                found[pending].append(value)  # type: ignore[union-attr]
            continue
        match = _KEY.match(line.strip())
        if not match:
            raise DirectoryUnreadable(
                f"{path}: cannot parse {raw.strip()!r}. Expected `project: true`, "
                "`partners: [a, b]` or `blocked: []`."
            )
        key, value = match.group(1), match.group(2).strip()
        pending = None
        if key == "project":
            found["project"] = value.strip().lower() in ("true", "yes", "1")
            if value.strip().lower() not in ("true", "false", "yes", "no", "1", "0"):
                raise DirectoryUnreadable(
                    f"{path}: project must be true or false, got {value!r}."
                )
        elif key in ("partners", "blocked"):
            if value.startswith("["):
                inner = value.strip().lstrip("[").rstrip("]")
                found[key] = [v.strip().strip("'\"") for v in inner.split(",") if v.strip()]
            elif value:
                raise DirectoryUnreadable(
                    f"{path}: {key} must be a list — `{key}: [a, b]` or `-` items."
                )
            else:
                pending = key
                found.setdefault(key, [])
        else:
            raise DirectoryUnreadable(
                f"{path}: unknown key {key!r}. This file takes project, partners "
                "and blocked, and nothing else — an ignored key here would be a "
                "permission somebody believes is in force."
            )

    directory = Directory(
        project=bool(found.get("project", True)),
        partners=tuple(found.get("partners", ()) or ()),
        blocked=tuple(found.get("blocked", ()) or ()),
        source=str(path),
    )
    overlap = {p.casefold() for p in directory.partners} & {
        b.casefold() for b in directory.blocked
    }
    if overlap:
        # Not fatal — blocked wins, and it is stated — but it means the generator
        # emitted a contradiction, which is worth someone's attention.
        directory.warnings.append(
            f"{path}: {', '.join(sorted(overlap))} is in both partners and blocked. "
            "Blocked wins; the generator should not have emitted both."
        )
    return directory
