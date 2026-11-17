"""One bounded, opt-in Google collection pass; stand-ins use the same path."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Callable

from . import google_chat, google_intake, google_media, google_notices, google_reply
from .google_store import GoogleStore


def _subscriber(config: google_chat.AgentConfig):
    try:
        from google.cloud import pubsub_v1
    except ImportError as exc:
        raise google_chat.GoogleChatConfigError(
            "Google Chat is configured but google-cloud-pubsub is not installed"
        ) from exc
    return pubsub_v1.SubscriberClient.from_service_account_file(str(config.credential_file))


def _empty_pull(exc: Exception) -> bool:
    """A bounded unary pull can time out with no available messages."""
    try:
        from google.api_core.exceptions import DeadlineExceeded
    except ImportError:
        return False
    return isinstance(exc, DeadlineExceeded)


def _event_metadata(raw: object, outcome: str) -> str:
    """Log event shape without copying untrusted text or a person's address."""
    data = raw if isinstance(raw, dict) else {}
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    space = data.get("space") if isinstance(data.get("space"), dict) else {}

    def label(value: object, allowed: set[str]) -> str:
        return value if isinstance(value, str) and value in allowed else "unknown"

    form = "add_on" if "chat" in data else "interaction"
    return (f"Google Chat event: form={form} "
            f"type={label(data.get('type'), {'MESSAGE', 'ADDED_TO_SPACE', 'REMOVED_FROM_SPACE'})} "
            f"space_type={label(space.get('spaceType'), {'DIRECT_MESSAGE', 'GROUP_CHAT', 'SPACE'})} "
            f"user.name={bool(user.get('name'))} user.email={bool(user.get('email'))} "
            f"message={isinstance(data.get('message'), dict)} outcome={outcome}")


def run_once(state_dir: Path, project: str, seat_name: str,
             subscriber_factory: Callable = _subscriber,
             private_rows: dict[str, dict] | None = None,
             record: Callable[[str, str], None] | None = None,
             notice_post: Callable | None = None,
             reply_post: Callable | None = None,
             reply_upload: Callable | None = None,
             configured_override: dict[str, google_chat.AgentConfig] | None = None,
             media_download: Callable | None = None,
             seat_handoff: Callable | None = None,
             stand_in_run: Path | None = None,
             cleanup_files: bool = True,
             *, raise_on_error: bool = True) -> int:
    """Pull one batch per configured agent; ack after commit, then hand off.

    No settings means no private directory, Google, or store call. A failed
    private read prevents all pulls, so no old people list admits anyone.
    """
    if stand_in_run is not None and (
            configured_override is None or private_rows is None
            or subscriber_factory is _subscriber or notice_post is None
            or reply_post is None or reply_upload is None or media_download is None):
        raise ValueError("stand-in run requires isolated settings, people, media, post and upload callbacks")
    if stand_in_run is not None:
        from . import google_standin
        if Path(state_dir) != Path(stand_in_run) or len(configured_override) != 1:
            raise ValueError("stand-in pass must use one marked run directory and one agent")
        agent = next(iter(configured_override))
        _, verified_config, verified_row, _ = google_standin.open_run(Path(stand_in_run), agent)
        if configured_override[agent] != verified_config or private_rows != {agent: verified_row}:
            raise ValueError("stand-in pass must use its marked settings and private fixture")
    configured = configured_override if configured_override is not None else google_chat.load_settings(state_dir)
    if not configured:
        return 0
    store = GoogleStore(state_dir / "comms.db")
    if cleanup_files:
        google_media.cleanup_inbound(store)
        google_reply.google_drive.cleanup_long_answers(store)
    for message_id in store.expire_pending_replies():
        if record is not None:
            record("warn", f"Google Chat {message_id}: reply failed after 24 hours")
    rows = (private_rows if private_rows is not None else
            google_chat.private_assignments(project, seat_name))
    accepted = 0
    failures = []

    def warn(message: str) -> None:
        failures.append(message)
        if record is not None:
            record("warn", message)

    valid_agents = set()
    for agent, config in configured.items():
        try:
            google_chat.people_for_agent(rows, agent)
            valid_agents.add(agent)
        except (google_chat.GoogleChatConfigError, KeyError) as exc:
            warn(f"Google Chat {agent}: private people record unavailable ({type(exc).__name__})")
            continue
        client = None
        try:
            client = subscriber_factory(config)
            try:
                response = client.pull(request={"subscription": config.subscription,
                                                "max_messages": 10}, timeout=20)
            except Exception as exc:
                if _empty_pull(exc):
                    response = None
                else:
                    raise
            if response is None:
                continue
            for received in response.received_messages:
                raw = None
                outcome = "failed"
                try:
                    raw = json.loads(received.message.data.decode("utf-8"))
                    event = google_intake.from_chat_event(raw, agent)
                    row, inserted = google_intake.collect_one(
                        store, event, rows[agent],
                        lambda ack_id=received.ack_id: client.acknowledge(request={
                            "subscription": config.subscription, "ack_ids": [ack_id]}))
                    accepted += int(inserted)
                    outcome = "stored" if inserted else "duplicate"
                except google_intake.NonMessageEvent:
                    try:
                        client.acknowledge(request={"subscription": config.subscription,
                                                    "ack_ids": [received.ack_id]})
                    except Exception as exc:
                        outcome = "ack_failed"
                        warn(f"Google Chat {agent}: could not acknowledge non-message event "
                             f"({type(exc).__name__})")
                    else:
                        outcome = ("ignored_add_on_form" if isinstance(raw, dict) and "chat" in raw
                                   else "ignored_no_message")
                except (ValueError, UnicodeError):
                    outcome = "malformed"
                    # Malformed messages remain unacknowledged, but cannot
                    # block the rest of this batch or the local queues.
                    warn(f"Google Chat {agent}: malformed event left unacknowledged")
                except Exception as exc:
                    warn(f"Google Chat {agent}: event failed ({type(exc).__name__}); left unacknowledged")
                finally:
                    if record is not None:
                        try:
                            record("info", _event_metadata(raw, outcome))
                        except Exception:
                            # A broken event logger cannot skip the remaining
                            # pull batch, handoffs, or pending replies.
                            failures.append("Google Chat event shape log failed")
        except Exception as exc:
            warn(f"Google Chat {agent}: pull failed ({type(exc).__name__})")
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception as exc:
                    warn(f"Google Chat {agent}: subscriber close failed ({type(exc).__name__})")
    # Pub/Sub is no longer on the critical path. This also resumes rows that
    # were committed before a prior process exit or a no-session result.
    for row in store.list():
        if row.agent in valid_agents and row.state == "queued":
            try:
                if media_download is None:
                    prepared = google_media.prepare_queued(store, row, configured[row.agent])
                else:
                    prepared = google_media.prepare_queued(store, row, configured[row.agent],
                                                           download=media_download)
                if prepared.state == "queued":
                    if seat_handoff is None:
                        if stand_in_run is None:
                            google_intake.deliver_queued(store, prepared)
                        else:
                            google_intake.deliver_queued(store, prepared,
                                                        stand_in_run=stand_in_run)
                    else:
                        google_intake.deliver_queued(store, prepared, handoff=seat_handoff,
                                                    stand_in_run=stand_in_run)
            except Exception as exc:
                warn(f"Google Chat {row.id}: handoff failed ({type(exc).__name__})")
    for outbound in store.pending_replies():
        created = datetime.fromisoformat(outbound.created_at.replace("Z", "+00:00"))
        if (datetime.now(timezone.utc) - created).total_seconds() >= google_reply.MAX_PENDING_SECONDS:
            store.mark_failed(outbound.id, "reply pending for 24 hours")
            if record is not None:
                record("warn", f"Google Chat {outbound.message_id}: reply failed after 24 hours")
            continue
        row = store.get(outbound.message_id)
        if row is None or row.agent not in configured:
            warn(f"Google Chat {outbound.message_id}: no configured agent for pending reply")
            continue
        if row.agent not in valid_agents:
            warn(f"Google Chat {outbound.message_id}: private people record unavailable for reply")
            continue
        try:
            if reply_post is None and reply_upload is None:
                result = google_reply.retry_pending(store, outbound, configured[row.agent], rows[row.agent])
            else:
                result = google_reply.retry_pending(
                    store, outbound, configured[row.agent], rows[row.agent],
                    post=reply_post or google_reply._post,
                    upload=reply_upload or google_reply.google_drive.upload_file)
            if result.state == "failed" and record is not None:
                record("warn", f"Google Chat {outbound.message_id}: reply failed; inspect Google Chat show")
        except Exception as exc:  # noqa: BLE001 - continue other pending replies
            warn(f"Google Chat {outbound.message_id}: reply retry failed ({type(exc).__name__})")
    for outbound in store.pending_notices():
        row = store.get(outbound.message_id)
        if row is None or row.agent not in valid_agents:
            continue
        try:
            if notice_post is None:
                result = google_reply.retry_notice(store, outbound, configured[row.agent])
            else:
                result = google_reply.retry_notice(store, outbound, configured[row.agent],
                                                   post=notice_post)
            if result.state == "failed" and record is not None:
                record("warn", f"Google Chat {outbound.message_id}: notice failed; inspect Google Chat log")
        except Exception as exc:
            warn(f"Google Chat {outbound.message_id}: notice retry failed ({type(exc).__name__})")
    if failures and raise_on_error:
        raise google_chat.GoogleChatConfigError(
            f"{len(failures)} Google Chat operation(s) failed: {'; '.join(failures[:3])}")
    return accepted


def run_forever(state_dir: Path, project: str, seat_name: str, stop: Event,
                record: Callable[[str, str], None],
                subscriber_factory: Callable = _subscriber) -> None:
    """Optional worker in the hub daemon process, with its own failure boundary.

    A private list is valid for at most the hub's existing five-minute config
    interval. A refresh failure discards it immediately: no stale list admits a
    person while directory state is unavailable.
    """
    from .operations import CONFIG_REFRESH_SECS

    rows = None
    refreshed_at = 0.0
    cleaned_at = 0.0
    while not stop.is_set():
        delay = 5
        try:
            if not google_chat.load_settings(state_dir):
                rows = None
                stop.wait(5)
                continue
            for message_id in GoogleStore(state_dir / "comms.db").expire_pending_replies():
                record("warn", f"Google Chat {message_id}: reply failed after 24 hours")
            if rows is None or time.monotonic() - refreshed_at >= CONFIG_REFRESH_SECS:
                rows = None
                rows = google_chat.private_assignments(project, seat_name)
                refreshed_at = time.monotonic()
            count = run_once(state_dir, project, seat_name,
                             subscriber_factory=subscriber_factory, private_rows=rows,
                             record=record, raise_on_error=False,
                             cleanup_files=time.monotonic() - cleaned_at >= 3600)
            if time.monotonic() - cleaned_at >= 3600:
                cleaned_at = time.monotonic()
            if count:
                record("info", f"Google Chat: stored {count} new message(s)")
        except Exception as exc:  # noqa: BLE001 - Google failure must not end hub
            rows = None
            delay = 60
            detail = str(exc) if isinstance(exc, google_chat.GoogleChatConfigError) else type(exc).__name__
            record("warn", f"Google Chat worker failed: {detail}")
        stop.wait(delay)
