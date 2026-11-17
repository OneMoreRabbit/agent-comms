"""Shared-drive reply files; no delegation and no public permission changes."""

from __future__ import annotations

import mimetypes
import os
import stat
import tempfile
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlparse

from .google_chat import AgentConfig, GoogleChatConfigError

MAX_REPLY_FILE_BYTES = 50 * 1024 * 1024
_FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveError(ValueError):
    """The declared shared-drive reply folder or file cannot be used."""


def _session(config: AgentConfig):
    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
    except ImportError as exc:
        raise GoogleChatConfigError("Google Drive needs google-auth") from exc
    credential = service_account.Credentials.from_service_account_file(
        str(config.credential_file), scopes=["https://www.googleapis.com/auth/drive"])
    return AuthorizedSession(credential)


def check_reply_folder(config: AgentConfig, session_factory: Callable = _session) -> None:
    """The service account must be able to add a child to a shared-drive folder."""
    try:
        with session_factory(config) as session:
            response = session.get(
                f"https://www.googleapis.com/drive/v3/files/{quote(config.reply_drive_folder_id, safe='')}",
                params={"supportsAllDrives": "true",
                        "fields": "id,mimeType,driveId,capabilities/canAddChildren"}, timeout=20)
            response.raise_for_status()
            folder = response.json()
    except Exception as exc:
        raise DriveError("reply folder inaccessible") from exc
    if not isinstance(folder, dict) or folder.get("mimeType") != _FOLDER_MIME or not folder.get("driveId"):
        raise DriveError("reply folder is not a shared-drive folder")
    if folder.get("capabilities", {}).get("canAddChildren") is not True:
        raise DriveError("reply folder is not writable by this service account")


def validate_outbound_file(agent: str, path: Path) -> Path:
    """Keep an agent's file beneath its own declared out root, with no symlink."""
    root = Path.home() / "comms-files" / "out" / agent
    candidate = path.absolute()
    if ".." in path.parts or not candidate.is_relative_to(root):
        raise DriveError("reply file must be under the agent's comms-files/out folder")
    for item in (Path.home() / "comms-files", Path.home() / "comms-files" / "out", root,
                 *[root.joinpath(*candidate.relative_to(root).parts[:index])
                   for index in range(1, len(candidate.relative_to(root).parts) + 1)]):
        try:
            info = item.lstat()
        except FileNotFoundError as exc:
            raise DriveError("reply file does not exist") from exc
        if stat.S_ISLNK(info.st_mode):
            raise DriveError("reply file path contains a symlink")
    info = candidate.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise DriveError("reply file must be a regular file owned by this seat user")
    if info.st_size > MAX_REPLY_FILE_BYTES:
        raise DriveError("reply file is too large (limit 50 MiB)")
    return candidate


def create_long_answer(agent: str, text: str) -> Path:
    """Keep an overlong answer whole in an owner-only file for Drive upload."""
    root = Path.home() / "comms-files" / "out" / agent
    for directory in (Path.home() / "comms-files", Path.home() / "comms-files" / "out", root):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise DriveError("long-answer output directory is not owner-only")
    if len(text.encode("utf-8")) > MAX_REPLY_FILE_BYTES:
        raise DriveError("long answer is too large (limit 50 MiB)")
    descriptor, name = tempfile.mkstemp(prefix="answer-", suffix=".txt", dir=root)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(text)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def upload_file(config: AgentConfig, path: Path, session_factory: Callable = _session) -> str:
    """Create one non-public file in the assigned shared-drive folder."""
    source = validate_outbound_file(config.agent, path)
    check_reply_folder(config, session_factory)
    content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    with session_factory(config) as session:
        start = session.post(
            "https://www.googleapis.com/upload/drive/v3/files",
            params={"uploadType": "resumable", "supportsAllDrives": "true",
                    "fields": "id,webViewLink"},
            json={"name": source.name, "parents": [config.reply_drive_folder_id]},
            headers={"X-Upload-Content-Type": content_type,
                     "X-Upload-Content-Length": str(source.stat().st_size)}, timeout=30)
        start.raise_for_status()
        location = start.headers.get("Location", "")
        parsed = urlparse(location)
        if parsed.scheme != "https" or parsed.hostname not in {"www.googleapis.com", "upload.googleapis.com"}:
            raise DriveError("Drive did not return a trusted upload location")
        with source.open("rb") as content:
            result = session.put(location, data=content,
                                 headers={"Content-Type": content_type,
                                          "Content-Length": str(source.stat().st_size)},
                                 timeout=120)
        result.raise_for_status()
        uploaded = result.json()
    if not isinstance(uploaded, dict) or not isinstance(uploaded.get("webViewLink"), str):
        raise DriveError("Drive did not return a file link")
    return uploaded["webViewLink"]
