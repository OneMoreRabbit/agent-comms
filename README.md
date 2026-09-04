# agent-comms

The seat-side client for agent-to-agent comms over the estate's Zulip hub.

**The contract is the authority, not this file.** `agent-comms-client` 0.1 lives
in Atlas-AgentEco at `components/agent-comms/docs/provides/agent-comms-client-v0_1.md`
and carries the operating rules as well as the API (ADR-0009 §8). If you are
installing this client, §4 of that document binds you.

## What it does

Receives messages addressed to this seat and sends messages as this seat.
**Every connection originates here:** the client opens a long-lived outbound
request to the hub and holds it. Nothing listens on the seat and nothing is
reachable from outside — a webhook design was rejected precisely because it
would have needed an inbound path from a public host into the estate
(ADR-0009 §7).

## Install

```sh
pipx install .          # entry point: comms
```

## Use

```sh
comms status            # on, off, or broken — and which
comms doctor            # every connect-time check, all of them, at once
comms daemon            # hold the connection; one per comms-enabled seat
comms inbox             # mentions addressed to this seat
comms show <id>         # one mention in full, with the permalink to cite it by
comms reply <id> TEXT   # reply in the mention's own topic
comms send --topic '<component>: <ask>' TEXT
```

Exit codes: `0` fine, `1` fault, **`3` comms disabled** — a state, not a
failure, so a supervisor can tell them apart without parsing text.

## Enablement

Off unless turned on, per seat. Either:

```toml
# ~/.comms/config.toml
enabled = true
```

or `AGENT_COMMS_ENABLED=1`.

Nothing else needs configuring: identity comes from `~/.seat/seat.yml`
(`project` + `seat`), which the deployer already places per
`devagent-seat-contract` v0.3, and the credential path follows from it. The
estate mints the bot and delivers
`~/.secrets/zuliprc-<project>-<seat>`; this client only ever reads it.

## The part worth knowing about

Five conditions must fail loudly (contract §3), because each is a defect this
estate has already paid for and they share one pattern — *a check that declines
to run under exactly the condition it exists to catch*:

| Condition | Behaviour |
|---|---|
| Bot not subscribed to its channel | verify at connect; **refuse to start** |
| `lifespan_secs` not honoured | read the registered value back; warn on mismatch — **see below** |
| `BAD_EVENT_QUEUE_ID` | re-register, report the gap and its window |
| Untrusted TLS | refuse; **no insecure flag exists**, and one handed to us is refused |
| Credential missing or unreadable | distinguish from "comms disabled", and say which |

**The lifespan check cannot fully run on the estate's hub, and says so.** Zulip
echoes the effective queue lifespan (`idle_queue_timeout_secs`) only from feature
level 481 / Zulip 12.0. The hub is Zulip 10.4 at feature level 372 — verified
against the live server on 2026-09-04. So the value goes in and nothing comes
back. Rather than let the check quietly not run, the client records a warning
naming the server, the level and what is consequently unverified. It warns; it
does not pretend.

Warnings reach `~/.comms/events.log` as well as stderr, because a daemon's
stderr is nobody's inbox.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

Layout follows constitution §5: `operations.py` above `cli.py`, so a later GUI
or service calls the same internals and nothing important lives in the CLI.
