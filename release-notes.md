---
title: Release notes — agent-comms
status: active
updated: 2026-10-09
owner: agent-eco.component.agent-comms
about: one short entry per release, newest first
---

# Release notes — agent-comms

## Built, not yet released

- **2.9.4 — 2026-11-16:** Add opt-in Google Chat collection in the existing daemon, private-list admission, bounded text handoff, and a durable text-reply outbox with stable request IDs; acknowledge non-message events, isolate failures, end failed or aged replies, and erase refused text; keep hub behavior unchanged. Why: prove a stand-in text round trip while refusing unknown people and preserving retry safety. This is not a released tag or the operator beta; files, fixed notices and live Google acceptance remain open.

## v2.9.3 — 2026-11-16

**TL;DR:** Add opt-in Google Chat settings validation, private-assignment diagnostics and a separate local message store, without changing hub delivery.

- `comms google-chat` now has status, doctor and local message inspection commands. A hub-only seat needs no Google settings or credential and makes no Google or private-directory call.
- A configured seat checks its fixed local settings and reads its own private 0.3 assignment record separately from the hub's 0.2 cache. Google-only records use separate tables in the existing database, with deduplication by source message name.
- This foundation release does **not** collect Google events, deliver them to an agent, or post replies. It is not the beta. Rolling back to 2.9.2 preserves hub service but cannot read the Google-only tables until a newer version returns.

**Action:** No estate rollout. Install on test-claude or test-codex only after arch grants a window; otherwise leave existing seats on 2.9.2. The next build adds collection and admission.

## v2.9.2 — 2026-10-09

**TL;DR:** Doctor checks assigned agents' bot transports from its refreshed local assignment cache, without directory self-lookups.

- A wrong-bot assignment still fails the check and names the affected agent and bot.
- Missing or malformed transport is reported as unchecked; an explicitly empty transport remains undeclared.
- On both DEV test seats, doctor passed 15/15 checks and made zero directory calls in the instrumented run.

**Action:** Install agent-comms 2.9.2 on seats monitored with `comms doctor`; restart the comms daemon after installation so its build matches the CLI.

## v2.9.1 — 2026-10-09

**TL;DR:** Apply directory-authored per-sender delivery overrides at the receiving seat.

- An exact sender FQN can select inject, hold or none without changing the receiving agent's default.
- Permission still wins: an override never makes a non-partner deliverable.
- A default-hold target now injects declared component senders while unrelated senders remain held.

**Action:** Install agent-comms 2.9.1 on receiving seats that use directory delivery overrides. No action is needed for seats without overrides.
