"""Shared-drive reply files; no delegation and no public permission changes."""

from __future__ import annotations

import mimetypes
import os
import re
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlparse

from .google_chat import AgentConfig, GoogleChatConfigError
from .google_paths import FileDirectoryError, ensure_owner_only_directory, work_files_root

MAX_REPLY_FILE_BYTES = 50 * 1024 * 1024
_FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveError(ValueError):
    """The declared shared-drive reply folder or file cannot be used."""


def describe_failure(exc: Exception) -> str:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 403:
        return "Drive API not enabled or access refused (HTTP 403)"
    if status == 404:
        return "reply folder not found or not shared with this account (HTTP 404)"
    if isinstance(exc, DriveError):
        return str(exc)
    return "Google Drive is not reachable"


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
        raise DriveError(describe_failure(exc)) from exc
    if not isinstance(folder, dict) or folder.get("mimeType") != _FOLDER_MIME or not folder.get("driveId"):
        raise DriveError("reply folder is not a shared-drive folder")
    if folder.get("capabilities", {}).get("canAddChildren") is not True:
        raise DriveError("reply folder is not writable by this service account")


def ensure_outbound_root(agent: str) -> Path:
    """Prepare the directory named to an agent before handing it a turn."""
    try:
        root = work_files_root() / "out" / agent
        for directory in (root.parent.parent, root.parent, root):
            ensure_owner_only_directory(directory)
    except FileDirectoryError as exc:
        raise DriveError(str(exc)) from exc
    return root


def validate_outbound_file(agent: str, path: Path) -> Path:
    """Keep an agent's file beneath its own declared out root, with no symlink."""
    try:
        root = work_files_root() / "out" / agent
    except FileDirectoryError as exc:
        raise DriveError(str(exc)) from exc
    candidate = path.absolute()
    if ".." in path.parts or not candidate.is_relative_to(root):
        raise DriveError("reply file must be under the agent's comms-files/out folder")
    ensure_outbound_root(agent)
    for item in (root.parent.parent, root.parent, root,
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
    root = ensure_outbound_root(agent)
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


def cleanup_long_answers(store, *, now: datetime | None = None) -> int:
    """Remove only generated long-answer files after 30 days; retain the outbox."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=30)
    removed = 0
    for outbound in store.replies():
        if not outbound.payload.get("generated_answer"):
            continue
        try:
            created = datetime.fromisoformat(outbound.created_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if created >= cutoff:
            continue
        row = store.get(outbound.message_id)
        if row is None:
            continue
        try:
            root = work_files_root() / "out" / row.agent
        except FileDirectoryError:
            continue
        candidate = Path(outbound.payload.get("file_path", ""))
        if candidate.parent != root or not re.fullmatch(r"answer-[A-Za-z0-9_-]+\.txt", candidate.name):
            continue
        try:
            info = candidate.lstat()
            directories = [path.lstat() for path in (root.parent.parent, root.parent, root)]
        except FileNotFoundError:
            continue
        if (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and all(stat.S_ISDIR(directory.st_mode) and directory.st_uid == os.getuid()
                        for directory in directories)):
            candidate.unlink()
            removed += 1
    return removed
