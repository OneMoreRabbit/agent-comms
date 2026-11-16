"""Durable, original-space text replies through the Google Chat API."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlencode

from . import google_chat
from .google_store import GoogleMessage, GoogleOutbound, GoogleStore

MAX_REPLY_BYTES = 32000
MAX_PENDING_SECONDS = 24 * 60 * 60
_SPACE = re.compile(r"^spaces/[A-Za-z0-9_-]+$")


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
           f"{urlencode({'requestId': outbound.request_id})}")
    response = session.post(url, json={"text": outbound.payload["text"]}, timeout=30)
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
              ) -> GoogleOutbound:
    """Fix one reply in the outbox before posting, never taking a new route."""
    if row.agent != config.agent:
        raise ValueError(f"{row.id}: agent does not match the configured Google contact")
    if not _SPACE.fullmatch(row.space):
        raise ValueError(f"{row.id}: invalid original direct-message space")
    if not text or not text.strip():
        raise ValueError("Google Chat reply needs nonempty text")
    if len(text.encode("utf-8")) > MAX_REPLY_BYTES:
        raise ValueError(f"Google Chat reply exceeds {MAX_REPLY_BYTES} UTF-8 bytes")
    _still_allowed(row, private_row)
    outgoing = store.queue_reply(row, text, str(uuid.uuid4()))
    if outgoing.state == "sent":
        return outgoing
    if outgoing.state == "failed":
        raise PermanentReplyError(f"{row.id}: {outgoing.failure_reason}")
    result = _attempt_pending(store, outgoing, config, private_row, post)
    if result.state == "failed":
        raise PermanentReplyError(f"{row.id}: {result.failure_reason}")
    return result


def retry_pending(store: GoogleStore, outbound: GoogleOutbound,
                  config: google_chat.AgentConfig, private_row: dict,
                  post: Callable[[GoogleOutbound, google_chat.AgentConfig], None] = _post,
                  *, now: datetime | None = None) -> GoogleOutbound:
    """Replay only the original outbox row, with its original requestId."""
    if outbound.state != "pending" or outbound.kind != "reply":
        return outbound
    return _attempt_pending(store, outbound, config, private_row, post, now=now)
