---
title: Release notes — agent-comms
status: active
updated: 2026-11-18
owner: agent-eco.component.agent-comms
about: one short entry per release, newest first
---

# Release notes — agent-comms

## Built, not yet released

## v2.9.6 — 2026-11-18

**TL;DR:** Keep stand-in inbound files and their 30-day cleanup inside the marked test run, so synthetic `gc-` IDs cannot overwrite or remove real Google files on a test seat.

- The stand-in turn names its own file path below the run directory. The live Google inbound path and hub behavior are unchanged.
- A regression verifies that a same-ID real-path file is untouched after stand-in handoff and cleanup.
- The known 2.9.5 second-upload race remains open; this patch does not change reply uploading.

**Action:** Install on test-claude and test-codex for the full G-S03–G-S07 stand-in rerun. Do not install on other seats before beta acceptance.

## v2.9.5 — 2026-11-17

**TL;DR:** Add bounded Google Chat file intake, shared-drive reply links, durable notices, stand-in reply testing and long-answer links without changing hub-only seats.

- Admitted photos and documents are saved under owner-only paths before the agent turn; file replies and complete long answers are uploaded to a configured shared-drive folder and posted as links.
- Fixed one-time notices cover delivery and file failures. A hidden, isolated stand-in reply option supports the test-seat acceptance scripts without calling live Google.
- Known issue: if the reply command records an upload link between a worker pass reading the pending row and claiming an upload, the worker may upload the same file a second time. The Chat reply is still posted once. This race remains for a later build.

**Action:** Install only on test-claude and test-codex for the stand-in and operator Google Chat tests. The Google beta is not yet accepted; do not install on other seats.

## v2.9.4 — 2026-11-16

**TL;DR:** Add opt-in Google Chat text collection and replies while leaving hub behavior unchanged.

- The existing daemon can pull Chat events, check people against the owning seat's private directory record, and hand admitted text to `seat msg` under the agent's exact FQN. It stores a reply with a stable request ID before posting it to the original direct message.
- Non-message and add-on-form events are acknowledged and identified in content-free logs. Refused text is erased; a failure in one pull, handoff or reply does not stop the remaining work.
- This is a first real-Google text build, **not** the beta. Files, fixed notices, full test-seat scripts and real-Google acceptance remain open.

**Action:** Estate-manage installs the tag only on test-claude and test-codex with the `agent-comms[google]` extra. With no Google settings file, the hub remains as on 2.9.2. Configure the Google side and restart `comms daemon` only for the operator's text try.

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
