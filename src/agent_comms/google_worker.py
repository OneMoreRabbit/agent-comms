"""One bounded, opt-in Google collection pass; stand-ins use the same path."""

from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Event
from typing import Callable

from . import google_chat, google_intake, google_reply
from .google_store import GoogleStore


def _subscriber(config: google_chat.AgentConfig):
    try:
        from google.cloud import pubsub_v1
    except ImportError as exc:
        raise google_chat.GoogleChatConfigError(
            "Google Chat is configured but google-cloud-pubsub is not installed"
        ) from exc
    return pubsub_v1.SubscriberClient.from_service_account_file(str(config.credential_file))


def run_once(state_dir: Path, project: str, seat_name: str,
             subscriber_factory: Callable = _subscriber,
             private_rows: dict[str, dict] | None = None) -> int:
    """Pull one batch per configured agent; ack after commit, then hand off.

    No settings means no private directory, Google, or store call. A failed
    private read prevents all pulls, so no old people list admits anyone.
    """
    configured = google_chat.load_settings(state_dir)
    if not configured:
        return 0
    rows = (private_rows if private_rows is not None else
            google_chat.private_assignments(project, seat_name))
    for agent in configured:
        google_chat.people_for_agent(rows, agent)
    store = GoogleStore(state_dir / "comms.db")
    accepted = 0
    for agent, config in configured.items():
        client = subscriber_factory(config)
        try:
            response = client.pull(request={"subscription": config.subscription,
                                            "max_messages": 10}, timeout=20)
            for received in response.received_messages:
                try:
                    raw = json.loads(received.message.data.decode("utf-8"))
                    event = google_intake.from_chat_event(raw, agent)
                    row, inserted = google_intake.collect_one(
                        store, event, rows[agent],
                        lambda ack_id=received.ack_id: client.acknowledge(request={
                            "subscription": config.subscription, "ack_ids": [ack_id]}))
                    accepted += int(inserted)
                except (ValueError, UnicodeError) as exc:
                    # A malformed event has no trustworthy source name to
                    # dedupe, so do not acknowledge it or invent an identity.
                    raise google_chat.GoogleChatConfigError(
                        f"{agent}: malformed Google event; Pub/Sub delivery left unacknowledged"
                    ) from exc
        finally:
            client.close()
    # Pub/Sub is no longer on the critical path. This also resumes rows that
    # were committed before a prior process exit or a no-session result.
    for row in store.list():
        if row.agent in configured and row.state == "queued":
            google_intake.deliver_queued(store, row)
    failures = []
    for outbound in store.pending_replies():
        row = store.get(outbound.message_id)
        if row is None or row.agent not in configured:
            failures.append(f"{outbound.message_id}: no configured agent")
            continue
        try:
            google_reply.retry_pending(store, outbound, configured[row.agent], rows[row.agent])
        except Exception as exc:  # noqa: BLE001 - continue other pending replies
            failures.append(f"{outbound.message_id}: {type(exc).__name__}: {exc}")
    if failures:
        raise google_chat.GoogleChatConfigError(
            f"{len(failures)} Google Chat reply/replies remain pending: {'; '.join(failures[:3])}")
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
    while not stop.is_set():
        delay = 5
        try:
            if not google_chat.load_settings(state_dir):
                rows = None
                stop.wait(5)
                continue
            if rows is None or time.monotonic() - refreshed_at >= CONFIG_REFRESH_SECS:
                rows = None
                rows = google_chat.private_assignments(project, seat_name)
                refreshed_at = time.monotonic()
            count = run_once(state_dir, project, seat_name,
                             subscriber_factory=subscriber_factory, private_rows=rows)
            if count:
                record("info", f"Google Chat: stored {count} new message(s)")
        except Exception as exc:  # noqa: BLE001 - Google failure must not end hub
            rows = None
            delay = 60
            record("warn", f"Google Chat worker failed: {type(exc).__name__}: {exc}")
        stop.wait(delay)
