"""An explicitly selected, owner-only run store for test replies only."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from urllib.parse import quote

from .google_chat import AgentConfig
from .google_store import GoogleOutbound, GoogleStore

ROOT = Path("/home/dev/.local/share/agent-comms/stand-in")


def open_run(path: Path, agent: str) -> tuple[GoogleStore, AgentConfig, dict, Path]:
    """Only a marked child of the fixed stand-in root can redirect a reply."""
    candidate = path.absolute()
    if candidate.parent != ROOT or candidate == ROOT:
        raise ValueError("stand-in run must be a direct child of the fixed stand-in root")
    for item in (ROOT, candidate):
        info = item.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError("stand-in run directory must be owner-only")
    marker = candidate / "marker.json"
    info = marker.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError("stand-in run marker must be an owner-only regular file")
    data = json.loads(marker.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or set(data) != {"stand_in_run", "agent", "private_row"}
            or data.get("stand_in_run") != 1 or data.get("agent") != agent
            or not isinstance(data.get("private_row"), dict)):
        raise ValueError("stand-in run marker does not match this agent")
    database = candidate / "comms.db"
    if database.is_symlink():
        raise ValueError("stand-in database may not be a symlink")
    config = AgentConfig(agent, "stand-in", candidate / "unused-credential.json", "stand-in")
    return GoogleStore(database), config, data["private_row"], candidate


def recorder(run_dir: Path):
    """Capture the exact would-be post locally, with no Google API object."""
    def post(outbound: GoogleOutbound, config: AgentConfig) -> None:
        destination = run_dir / "posts.jsonl"
        if destination.is_symlink():
            raise ValueError("stand-in posts file may not be a symlink")
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as output:
            info = os.fstat(output.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise ValueError("stand-in posts file must be an owner-only regular file")
            output.write(json.dumps({"message_id": outbound.message_id,
                                     "request_id": outbound.request_id,
                                     "agent": config.agent,
                                     "space": outbound.payload["space"],
                                     "text": outbound.payload["text"]}, sort_keys=True) + "\n")
    return post


def fake_upload(run_dir: Path):
    def upload(config: AgentConfig, path: Path) -> str:
        from .google_drive import validate_outbound_file
        validate_outbound_file(config.agent, path)
        return "https://drive.google.com/file/d/stand-in-" + quote(path.name, safe="") + "/view"
    return upload
