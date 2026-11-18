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
MAX_UPLOAD_SECONDS = 10 * 60
_SPACE = re.compile(r"^spaces/[A-Za-z0-9_-]+$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")


def _wire_bytes(text: str) -> int:
    return len(json.dumps({"text": text, "markupSyntax": "MARKUP_SYNTAX_MARKDOWN"},
                          ensure_ascii=False).encode("utf-8"))


def _chat_text(source: str) -> str:
    """Keep headings and tables readable in Chat's limited Markdown dialect."""
    result = []
    fenced = False
    for line in source.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        ending = line[len(content):]
        if content.strip().find("```") == 0:
            fenced = not fenced
            result.append(line)
            continue
        if fenced:
            result.append(line)
            continue
        heading = _HEADING.fullmatch(content)
        if heading:
            result.append("*" + heading.group(1) + "*" + ending)
            continue
        stripped = content.strip()
        if stripped.count("|") >= 2 and stripped[0:1] == "|" and stripped[-1:] == "|":
            cells = [cell.strip() for cell in stripped[1:-1].split("|")]
            if all(cell and set(cell) <= {"-", ":"} for cell in cells):
                continue
            result.append(" — ".join(cells) + ending)
            continue
        result.append(line)
    return "".join(result)


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


def _upload_may_be_running(outbound: GoogleOutbound, moment: datetime) -> bool:
    if not outbound.upload_started_at:
        return False
    try:
        started = datetime.fromisoformat(outbound.upload_started_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (moment - started).total_seconds() < MAX_UPLOAD_SECONDS


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
        if outbound.upload_in_progress:
            if not _upload_may_be_running(outbound, moment):
                store.mark_failed(outbound.id, "Drive upload outcome uncertain; automatic re-upload refused")
            return store.get_outbound(outbound.id)
        started = store.begin_upload(outbound.id)
        if started is None:
            current = store.get_outbound(outbound.id)
            if current.upload_in_progress:
                if not _upload_may_be_running(current, moment):
                    store.mark_failed(outbound.id, "Drive upload outcome uncertain; automatic re-upload refused")
            elif current.state == "pending" and current.upload_attempts >= 3:
                store.mark_failed(outbound.id, "Drive upload attempt limit reached")
            return store.get_outbound(outbound.id)
        try:
            link = upload(config, Path(outbound.payload["file_path"]))
        except Exception as exc:
            return store.upload_failed(outbound.id, google_drive.describe_failure(exc))
        try:
            outbound = store.set_outbound_link(started, link)
        except Exception:
            store.mark_failed(outbound.id, "Drive link could not be recorded; automatic re-upload refused")
            return store.get_outbound(outbound.id)
        if outbound is None:
            store.mark_failed(started.id, "Drive link could not be recorded; automatic re-upload refused")
            return store.get_outbound(started.id)
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
              check_folder: Callable[[google_chat.AgentConfig], None] | None = None,
              ) -> GoogleOutbound:
    """Fix one reply in the outbox before posting, never taking a new route."""
    row = store.get(row.id)
    if row is None:
        raise ValueError("Google Chat message no longer exists")
    if row.agent != config.agent:
        raise ValueError(f"{row.id}: agent does not match the configured Google contact")
    if row.state not in {"delivered", "held", "retrieved", "uncertain"}:
        raise ValueError(f"{row.id}: cannot reply to a {row.state} message")
    if not _SPACE.fullmatch(row.space):
        raise ValueError(f"{row.id}: invalid original direct-message space")
    if (not text or not text.strip()) and file_path is None:
        raise ValueError("Google Chat reply needs text or one file")
    formatted = _chat_text(text)
    long_answer = _wire_bytes(formatted) > MAX_REPLY_BYTES
    if long_answer and file_path is not None:
        raise ValueError("a long answer and a separate file require two replies")
    _still_allowed(row, private_row)
    source = (google_drive.validate_outbound_file(config.agent, file_path)
              if file_path is not None else None)
    outgoing = store.matching_reply(row.id, text, str(source) if source else None,
                                    generated_answer=long_answer)
    if outgoing is None:
        if long_answer or file_path is not None:
            preflight = check_folder
            if preflight is None and upload is google_drive.upload_file:
                preflight = google_drive.check_reply_folder
            if preflight is not None:
                preflight(config)
        if long_answer:
            source = google_drive.create_long_answer(config.agent, text)
        wire_text = ("Long answer (complete text in the linked file): " + formatted[:1000] + "…"
                     if long_answer else formatted)
        outgoing = store.queue_reply(row, wire_text, str(uuid.uuid4()),
                                     str(source) if source is not None else None,
                                     source_text=text, generated_answer=long_answer)
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
