"""Durable, original-space text replies through the Google Chat API."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

from . import google_chat, google_drive
from .google_store import GoogleMessage, GoogleOutbound, GoogleStore

MAX_REPLY_BYTES = 32000
MAX_PENDING_SECONDS = 24 * 60 * 60
_SPACE = re.compile(r"^spaces/[A-Za-z0-9_-]+$")


def _wire_bytes(text: str) -> int:
    return len(json.dumps({"text": text, "markupSyntax": "MARKUP_SYNTAX_MARKDOWN"},
                          ensure_ascii=False).encode("utf-8"))


class PermanentReplyError(ValueError):
    """A reply cannot be posted and must not be retried."""


def _post(outbound: GoogleOutbound, config: google_chat.AgentConfig) -> None:
    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
    except ImportError as exc:
        raise google_chat.GoogleChatConfigError(
            "Google Chat is configured but google-auth is not installed"
        ) from exc
    credential = service_account.Credentials.from_service_account_file(
        str(config.credential_file), scopes=["https://www.googleapis.com/auth/chat.bot"])
    session = AuthorizedSession(credential)
    url = (f"https://chat.googleapis.com/v1/{outbound.payload['space']}/messages?"
           f"{urlencode({'requestId': outbound.request_id,
                         'messageId': 'client-' + outbound.request_id.replace('-', '')})}")
    response = session.post(url, json={"text": outbound.payload["text"],
                                       "markupSyntax": "MARKUP_SYNTAX_MARKDOWN"}, timeout=30)
    response.raise_for_status()


def _still_allowed(row: GoogleMessage, private_row: dict) -> None:
    from .google_intake import IntakeEvent, _admission

    if private_row.get("google_chat") is None:
        raise PermanentReplyError(f"{row.id}: Google Chat contact is no longer enabled")
    people = google_chat.people_for_agent({row.agent: private_row}, row.agent)
    event = IntakeEvent(
        agent=row.agent, message_name=row.source_name, space_name=row.space,
        event_time=row.event_time, text=row.payload.get("text", ""),
        event_type=row.payload.get("event_type", ""),
        space_type=row.payload.get("space_type", ""),
        sender_type=row.payload.get("sender_type", ""),
        sender_account_id=row.payload.get("sender_account_id", ""),
        sender_address=row.payload.get("sender_address", ""),
        app_agent=row.payload.get("app_agent", ""),
        has_attachments=bool(row.payload.get("has_attachments")),
    )
    reason = _admission(event, people)
    if reason:
        raise PermanentReplyError(f"{row.id}: no longer permitted: {reason}")


def _permanent_http_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) and 400 <= status < 500 and status not in (408, 429) else None


def _attempt_pending(store: GoogleStore, outbound: GoogleOutbound,
                     config: google_chat.AgentConfig, private_row: dict,
                     post: Callable[[GoogleOutbound, google_chat.AgentConfig], None],
                     upload: Callable[[google_chat.AgentConfig, Path], str] = google_drive.upload_file,
                     *, now: datetime | None = None) -> GoogleOutbound:
    moment = now or datetime.now(timezone.utc)
    created = datetime.fromisoformat(outbound.created_at.replace("Z", "+00:00"))
    if (moment - created).total_seconds() >= MAX_PENDING_SECONDS:
        store.mark_failed(outbound.id, "reply pending for 24 hours")
        return store.get_outbound(outbound.id)
    row = store.get(outbound.message_id)
    if row is None or row.agent != config.agent or not _SPACE.fullmatch(row.space):
        store.mark_failed(outbound.id, "pending reply has no valid original route")
        return store.get_outbound(outbound.id)
    try:
        _still_allowed(row, private_row)
    except PermanentReplyError as exc:
        store.mark_failed(outbound.id, str(exc))
        return store.get_outbound(outbound.id)
    if outbound.payload.get("file_path") and not outbound.payload.get("drive_link"):
        link = upload(config, Path(outbound.payload["file_path"]))
        outbound = store.set_outbound_link(outbound, link) or store.get_outbound(outbound.id)
        if _wire_bytes(outbound.payload["text"]) > MAX_REPLY_BYTES:
            store.mark_failed(outbound.id, "Google Chat reply exceeds 32000 UTF-8 bytes after file link")
            return store.get_outbound(outbound.id)
    try:
        post(outbound, config)
    except Exception as exc:
        status = _permanent_http_status(exc)
        if status is None:
            raise
        store.mark_failed(outbound.id, f"Google Chat rejected reply with HTTP {status}")
        return store.get_outbound(outbound.id)
    store.mark_sent(outbound.id)
    return store.get_outbound(outbound.id)


def send_text(store: GoogleStore, row: GoogleMessage, text: str,
              config: google_chat.AgentConfig, private_row: dict,
              post: Callable[[GoogleOutbound, google_chat.AgentConfig], None] = _post,
              *, file_path: Path | None = None,
              upload: Callable[[google_chat.AgentConfig, Path], str] = google_drive.upload_file,
              ) -> GoogleOutbound:
    """Fix one reply in the outbox before posting, never taking a new route."""
    if row.agent != config.agent:
        raise ValueError(f"{row.id}: agent does not match the configured Google contact")
    if not _SPACE.fullmatch(row.space):
        raise ValueError(f"{row.id}: invalid original direct-message space")
    if (not text or not text.strip()) and file_path is None:
        raise ValueError("Google Chat reply needs text or one file")
    long_answer = _wire_bytes(text) > MAX_REPLY_BYTES
    if long_answer and file_path is not None:
        raise ValueError("a long answer and a separate file require two replies")
    _still_allowed(row, private_row)
    outgoing = store.reply_for_message(row.id)
    if outgoing is not None:
        if ((outgoing.payload.get("source_text", outgoing.payload["text"]) != text)
                or (file_path is not None and
                    outgoing.payload.get("file_path") !=
                    str(google_drive.validate_outbound_file(config.agent, file_path)))):
            raise ValueError(f"{row.id}: a different reply is already recorded")
    else:
        source = (google_drive.create_long_answer(config.agent, text) if long_answer
                  else google_drive.validate_outbound_file(config.agent, file_path) if file_path
                  else None)
        wire_text = ("Long answer (complete text in the linked file): " + text[:1000] + "…"
                     if long_answer else text)
        outgoing = store.queue_reply(row, wire_text, str(uuid.uuid4()),
                                     str(source) if source is not None else None,
                                     source_text=text)
    if outgoing.state == "sent":
        return outgoing
    if outgoing.state == "failed":
        raise PermanentReplyError(f"{row.id}: {outgoing.failure_reason}")
    result = _attempt_pending(store, outgoing, config, private_row, post, upload)
    if result.state == "failed":
        raise PermanentReplyError(f"{row.id}: {result.failure_reason}")
    return result


def retry_pending(store: GoogleStore, outbound: GoogleOutbound,
                  config: google_chat.AgentConfig, private_row: dict,
                  post: Callable[[GoogleOutbound, google_chat.AgentConfig], None] = _post,
                  upload: Callable[[google_chat.AgentConfig, Path], str] = google_drive.upload_file,
                  *, now: datetime | None = None) -> GoogleOutbound:
    """Replay only the original outbox row, with its original requestId."""
    if outbound.state != "pending" or outbound.kind != "reply":
        return outbound
    return _attempt_pending(store, outbound, config, private_row, post, upload, now=now)


def retry_notice(store: GoogleStore, outbound: GoogleOutbound,
                 config: google_chat.AgentConfig,
                 post: Callable[[GoogleOutbound, google_chat.AgentConfig], None] = _post,
                 *, now: datetime | None = None) -> GoogleOutbound:
    """Retry a fixed line without a private-person lookup or a new route."""
    if outbound.state != "pending" or outbound.kind not in {
            "notice-refused", "notice-no-session", "notice-unavailable",
            "notice-held", "notice-expired", "notice-abandoned"}:
        return outbound
    moment = now or datetime.now(timezone.utc)
    created = datetime.fromisoformat(outbound.created_at.replace("Z", "+00:00"))
    if (moment - created).total_seconds() >= MAX_PENDING_SECONDS:
        store.mark_failed(outbound.id, "notice pending for 24 hours")
        return store.get_outbound(outbound.id)
    if not _SPACE.fullmatch(outbound.payload.get("space", "")):
        store.mark_failed(outbound.id, "notice has no valid original route")
        return store.get_outbound(outbound.id)
    try:
        post(outbound, config)
    except Exception as exc:
        status = _permanent_http_status(exc)
        if status is None:
            raise
        store.mark_failed(outbound.id, "Google Chat rejected notice")
        return store.get_outbound(outbound.id)
    store.mark_sent(outbound.id)
    return store.get_outbound(outbound.id)
