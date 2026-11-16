"""Optional Google Chat configuration and private assignment checks.

This is an opt-in surface. A seat without settings never contacts Google or
requests the directory's private 0.3 assignment record.
"""

from __future__ import annotations

import json
import os
import re
import stat
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import config_sync
from .resolve import directory_address

_FQN = re.compile(r"^[a-z0-9][a-z0-9-]*\.[a-z0-9][a-z0-9-]*\.[a-z0-9][a-z0-9-]*$")
_SUBSCRIPTION = re.compile(r"^projects/[^/]+/subscriptions/[^/]+$")
_FIELDS = {"subscription", "credential_file", "reply_drive_folder_id"}


class GoogleChatConfigError(ValueError):
    """The optional Google Chat configuration cannot safely be used."""


@dataclass(frozen=True)
class AgentConfig:
    agent: str
    subscription: str
    credential_file: Path
    reply_drive_folder_id: str


def settings_dir() -> Path:
    return Path.home() / ".secrets" / "google-chat"


def settings_file() -> Path:
    return settings_dir() / "settings.json"


def _private_file(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise GoogleChatConfigError(f"{path} must be a regular 0600 file")
    if info.st_uid != os.getuid():
        raise GoogleChatConfigError(f"{path} must be owned by this seat user")


def load_settings(state_dir: Path, *, path: Path | None = None) -> dict[str, AgentConfig]:
    """Read strict operational settings; never read people from this file."""
    path = path or settings_file()
    if not path.exists() and not path.is_symlink():
        return {}
    root = path.parent
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise GoogleChatConfigError(f"{root} must be a 0700 directory")
    if info.st_uid != os.getuid():
        raise GoogleChatConfigError(f"{root} must be owned by this seat user")
    _private_file(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GoogleChatConfigError("Google Chat settings are not valid JSON") from exc
    if not isinstance(raw, dict):
        raise GoogleChatConfigError("Google Chat settings must be an object keyed by agent FQN")
    assigned = config_sync.agent_set(state_dir)
    result = {}
    for agent, entry in raw.items():
        if not isinstance(agent, str) or not _FQN.fullmatch(agent) or agent not in assigned:
            raise GoogleChatConfigError(f"unknown or unassigned Google Chat agent: {agent}")
        if not isinstance(entry, dict) or set(entry) != _FIELDS:
            raise GoogleChatConfigError(f"{agent}: expected only {', '.join(sorted(_FIELDS))}")
        if not all(isinstance(value, str) and value.strip() for value in entry.values()):
            raise GoogleChatConfigError(f"{agent}: all Google Chat settings must be nonempty text")
        if not _SUBSCRIPTION.fullmatch(entry["subscription"]):
            raise GoogleChatConfigError(f"{agent}: invalid subscription name")
        expected = root / f"{agent}.json"
        if Path(entry["credential_file"]) != expected:
            raise GoogleChatConfigError(f"{agent}: credential_file must be {expected}")
        _private_file(expected)
        result[agent] = AgentConfig(agent, entry["subscription"], expected,
                                    entry["reply_drive_folder_id"])
    return result


def private_assignments(project: str, seat: str, *, timeout: float = 10.0) -> dict[str, dict]:
    """Fetch private 0.3 assignments separately from the hub's 0.2 cache.

    Failure is an error: an old people list must never admit a removed person.
    """
    address = directory_address()
    if not address:
        raise GoogleChatConfigError("directory address unavailable for private 0.3 read")
    url = f"{address.rstrip('/')}{config_sync.seat_path(project, seat)}?contract=0.3"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    token = config_sync._credential()
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        raise GoogleChatConfigError("private 0.3 assignment read failed") from exc
    if (not isinstance(body, dict) or body.get("contract") != "0.3"
            or not isinstance(body.get("assignments"), list)):
        raise GoogleChatConfigError("private 0.3 assignment response is malformed")
    rows = {}
    for row in body["assignments"]:
        if not isinstance(row, dict) or not isinstance(row.get("agent"), str):
            raise GoogleChatConfigError("private 0.3 assignment row is malformed")
        if row["agent"] in rows:
            raise GoogleChatConfigError("private 0.3 assignment has a duplicate agent")
        rows[row["agent"]] = row
    return rows


def diagnostic(state_dir: Path, project: str, seat: str) -> tuple[bool, str]:
    """A local-only off verdict, or a checked private assignment verdict."""
    try:
        configured = load_settings(state_dir)
        if not configured:
            return True, "not configured; no Google connection or private directory read"
        rows = private_assignments(project, seat)
        for agent in configured:
            record = rows.get(agent, {}).get("google_chat")
            if not isinstance(record, dict) or not isinstance(record.get("people"), list):
                raise GoogleChatConfigError(f"{agent}: no private google_chat.people record")
            if any(not isinstance(person, dict) or not isinstance(person.get("address"), str)
                   or not person["address"] for person in record["people"]):
                raise GoogleChatConfigError(f"{agent}: malformed private people list")
        return True, f"configured for {len(configured)} agent(s); private people records available"
    except (GoogleChatConfigError, OSError) as exc:
        return False, str(exc)
