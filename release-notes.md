---
title: Release notes — agent-comms
status: active
updated: 2026-10-09
owner: agent-eco.component.agent-comms
about: one short entry per release, newest first
---

# Release notes — agent-comms

## Built, not yet released

None.

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
