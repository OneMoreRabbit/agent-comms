"""Fakes for the hub, so every §3 behaviour is testable without a server."""

from __future__ import annotations

import pytest


class FakeTransport:
    """A Zulip transport with scriptable responses."""

    def __init__(
        self,
        subscriptions: list[str] | None = None,
        register_result: dict | None = None,
        event_batches: list[dict] | None = None,
        full_name: str = "agent-eco-agent-comms",
        is_bot: bool = True,
        realm: list[str] | None = None,
        channel_members: list[str] | None = None,
    ) -> None:
        self.subscriptions = subscriptions if subscriptions is not None else ["agent-eco"]
        # Modelled on the live hub, measured 2026-09-10: `blocks-android` is a
        # real bot in the realm and is NOT subscribed to `agent-eco`, so it
        # exists and is unreachable from this seat. That distinction is the
        # whole point of the reachability check, so the fake carries it.
        self.realm = realm if realm is not None else [
            "agent-comms", "agent-eco-arch", "agent-skeleton", "orchestrator",
            "blocks-android", "blocks-service", "blocks-arch",
        ]
        self.channel_members = channel_members if channel_members is not None else [
            "agent-comms", "agent-eco-arch", "agent-skeleton", "orchestrator",
        ]
        self.full_name = full_name
        self.is_bot = is_bot
        self.register_result = register_result
        self.event_batches = list(event_batches or [])
        self.register_calls: list[dict] = []
        self.sent: list[dict] = []

    def call_endpoint(self, url: str, method: str = "GET", request: dict | None = None) -> dict:
        if url == "users/me":
            return {
                "result": "success",
                "full_name": self.full_name,
                "email": "agent-eco-agent-comms-bot@example.com",
                "is_bot": self.is_bot,
            }
        if url == "users/me/subscriptions":
            return {
                "result": "success",
                "subscriptions": [
                    {"name": n, "stream_id": 100 + i}
                    for i, n in enumerate(self.subscriptions)
                ],
            }
        if url == "users":
            return {
                "result": "success",
                "members": [
                    {"full_name": n, "user_id": 1 + i, "is_active": True, "is_bot": True}
                    for i, n in enumerate(self.realm)
                ],
            }
        if url.startswith("streams/") and url.endswith("/members"):
            return {
                "result": "success",
                "subscribers": [
                    1 + self.realm.index(n)
                    for n in self.channel_members if n in self.realm
                ],
            }
        if url == "messages":
            self.sent.append(request or {})
            return {"result": "success", "id": 999}
        raise AssertionError(f"unexpected endpoint {url}")

    def register(self, **kwargs):
        self.register_calls.append(kwargs)
        return self.register_result or {
            "result": "success",
            "queue_id": "q1",
            "last_event_id": 0,
            "zulip_version": "10.4",
            "zulip_feature_level": 372,
        }

    def get_events(self, **kwargs):
        if self.event_batches:
            return self.event_batches.pop(0)
        return {"result": "success", "events": []}


@pytest.fixture
def seat(tmp_path, monkeypatch):
    """A seat with comms enabled, a valid credential, and an isolated home."""
    home = tmp_path / "home"
    (home / ".secrets").mkdir(parents=True)
    (home / ".seat").mkdir(parents=True)
    (home / ".seat" / "seat.yml").write_text(
        "project: agent-eco\nseat: agent-comms\nhost: marten\n", encoding="utf-8"
    )
    cred = home / ".secrets" / "zuliprc-agent-eco-agent-comms"
    cred.write_text(
        "[api]\nemail=agent-eco-agent-comms-bot@example.com\n"
        "key=secret\nsite=https://agent.onemorerabbit.co.uk\n",
        encoding="utf-8",
    )
    cred.chmod(0o600)

    state = home / ".comms"
    state.mkdir()
    (state / "config.toml").write_text("enabled = true\n", encoding="utf-8")

    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))
    for var in (
        "AGENT_COMMS_ENABLED", "AGENT_COMMS_HOME", "AGENT_COMMS_PROJECT",
        "AGENT_COMMS_SEAT", "AGENT_COMMS_CHANNEL",
        "ZULIP_API_KEY", "ZULIP_EMAIL", "ZULIP_SITE", "ZULIP_ALLOW_INSECURE",
    ):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture
def running_daemon(seat):
    """A seat with a daemon actually holding the lock and ticking.

    Holds the real `flock` rather than faking the probe, because the probe *is*
    the thing under test everywhere else: the seat's stale lock file sat on disk
    for four days across several dead daemons, and only the lock told the truth.
    """
    from agent_comms.config import load_settings
    from agent_comms.store import Store

    store = Store(load_settings().state_dir)
    store.ensure()
    handle = store.acquire_daemon_lock()
    store.save_position("queue-under-test", 1)
    yield store
    handle.close()
