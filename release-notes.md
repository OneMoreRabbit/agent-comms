---
title: Release notes — agent-comms
status: active
updated: 2026-10-09
owner: agent-eco.component.agent-comms
about: one short entry per release, newest first
---

# Release notes — agent-comms

## Built, not yet released

- 2.9.2 — 2026-10-09 — Doctor checks each assigned agent's bot from the cached assignment row, so a wrong-bot route is caught without a directory self-lookup.

## v2.9.1 — 2026-10-09

**TL;DR:** Apply directory-authored per-sender delivery overrides at the receiving seat.

- An exact sender FQN can select inject, hold or none without changing the receiving agent's default.
- Permission still wins: an override never makes a non-partner deliverable.
- A default-hold target now injects declared component senders while unrelated senders remain held.

**Action:** Install agent-comms 2.9.1 on receiving seats that use directory delivery overrides. No action is needed for seats without overrides.
