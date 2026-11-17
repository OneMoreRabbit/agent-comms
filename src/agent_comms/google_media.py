"""Download admitted Chat uploads into generated, owner-only local paths."""

from __future__ import annotations

import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote

from .google_chat import AgentConfig, GoogleChatConfigError
from .google_intake import MAX_AGE_SECONDS, _age_seconds
from .google_store import GoogleMessage, GoogleStore

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
_MEDIA_NAME = re.compile(r"[A-Za-z0-9_/-]+\Z")
_CONTENT_TYPE = re.compile(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+\Z")


class MediaError(ValueError):
    """An attachment cannot be safely delivered as a local file."""


def _download(resource_name: str, config: AgentConfig) -> Iterable[bytes]:
    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
    except ImportError as exc:
        raise GoogleChatConfigError("Google Chat media needs google-auth") from exc
    credential = service_account.Credentials.from_service_account_file(
        str(config.credential_file), scopes=["https://www.googleapis.com/auth/chat.bot"])
    with AuthorizedSession(credential) as session:
        url = f"https://chat.googleapis.com/v1/media/{quote(resource_name, safe='/')}?alt=media"
        with session.get(url, stream=True, timeout=30) as response:
            response.raise_for_status()
            yield from response.iter_content(chunk_size=64 * 1024)


def _owner_only_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise MediaError("inbound file directory is not owner-only")


def _target_directory(message_id: str) -> Path:
    root = Path.home() / "comms-files"
    for path in (root, root / "in", root / "in" / message_id):
        _owner_only_directory(path)
    return root / "in" / message_id


def prepare_queued(store: GoogleStore, row: GoogleMessage, config: AgentConfig,
                   download: Callable[[str, AgentConfig], Iterable[bytes]] = _download,
                   ) -> GoogleMessage:
    """Save all referenced bytes before the handoff; never use a Google filename."""
    if row.state != "queued" or not row.payload.get("has_attachments"):
        return row
    if row.payload.get("files"):
        return row
    if _age_seconds(row.event_time, datetime.now(timezone.utc)) > MAX_AGE_SECONDS:
        return store.transition(row.id, from_state="queued", to_state="expired",
                                reason="Google event is older than 24 hours") or store.get(row.id)
    attachments = row.payload.get("attachments")
    if not isinstance(attachments, list) or not 1 <= len(attachments) <= 10:
        return store.transition(row.id, from_state="queued", to_state="refused",
                                reason="attachment reference is unavailable") or store.get(row.id)
    target_dir = _target_directory(row.id)
    files = []
    written = []
    total = 0
    try:
        for index, attachment in enumerate(attachments, 1):
            reference = attachment.get("attachmentDataRef", {})
            resource_name = reference.get("resourceName")
            if not isinstance(resource_name, str) or not _MEDIA_NAME.fullmatch(resource_name):
                raise MediaError("malformed attachment reference")
            content_type = attachment.get("contentType")
            if not isinstance(content_type, str) or not _CONTENT_TYPE.fullmatch(content_type):
                content_type = "application/octet-stream"
            destination = target_dir / f"file-{index:02d}"
            size = 0
            descriptor, name = tempfile.mkstemp(prefix=".partial-", dir=target_dir)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    for chunk in download(resource_name, config):
                        if not isinstance(chunk, bytes):
                            raise MediaError("media download returned non-bytes data")
                        size += len(chunk)
                        if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
                            raise MediaError("file too large")
                        output.write(chunk)
                if destination.exists() or destination.is_symlink():
                    previous = destination.lstat()
                    if not stat.S_ISREG(previous.st_mode):
                        raise MediaError("inbound file path is not a regular file")
                os.replace(temporary, destination)
                written.append(destination)
            finally:
                temporary.unlink(missing_ok=True)
            total += size
            files.append({"path": str(destination), "content_type": content_type,
                          "bytes": size})
    except MediaError as exc:
        # The message remains evidence, but no partial file is named in a turn.
        for path in written:
            path.unlink(missing_ok=True)
        return store.transition(row.id, from_state="queued", to_state="refused",
                                reason=str(exc)) or store.get(row.id)
    return store.set_files(row, files) or store.get(row.id)
