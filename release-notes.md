---
title: Release notes — agent-comms
status: active
updated: 2026-11-18
owner: agent-eco.component.agent-comms
about: one short entry per release, newest first
---

# Release notes — agent-comms

## Built, not yet released

- 2.9.10 — 2026-11-18: Keep Google Chat inbound, outbound, long-answer and stand-in files under `~/work/comms-files`, so Claude can open an incoming image without an outside-working-directory prompt. Existing `~/comms-files` trees are no longer read; removal is a separate operator-approved step.

## v2.9.9 — 2026-11-18

**TL;DR:** Google Chat hands each message to the seat on one line, preserving every labelled field and the full stored text. This avoids the multi-line typing burst associated with Claude not taking Enter.

- A rendered turn over 65,536 UTF-8 bytes is refused visibly, not shortened. Hub delivery and seat 2.9.1 are unchanged.
- Source suite: 466/466. Known limit: non-whitespace control characters, such as ESC, still pass through person-authored text to the seat.

**Action:** Install from this tag on test-claude and test-codex only, then run the full stand-in set on both seats. This tag is not operator beta acceptance.

## v2.9.8 — 2026-11-18

**TL;DR:** Allow more than one distinct Google Chat reply to one admitted message, so an agent can say it is working and then send its answer.

- Each distinct reply has its own durable request ID. Repeating a sent reply posts nothing new. A failed file reply without a Drive link can be tried again with the same text and file as a new row.
- A 2.9.7 store opens intact in 2.9.8. After 2.9.8 writes a second reply, 2.9.7 cannot open the Google store: its Google worker and commands fail, while hub delivery continues. Return by the next patch; do not restore `comms.db`, which also holds hub messages and cursor.
- Source suite: 464/464. No hub delivery code changed.

**Action:** Install from this tag on test-claude and test-codex only, then rerun the full Google stand-in set on both seats. This tag is not operator beta acceptance.

## v2.9.7 — 2026-11-18

**TL;DR:** Prepare and tighten Google Chat file directories before naming
the file-out path in an agent turn, so an agent-created group-writable
folder cannot break later file input or long answers.

- Owned real directories are made owner-only (0700) before a turn, inbound
  file, outbound file or generated long answer uses them. Links and
  foreign-owned paths remain refused.
- Source suite: 460/460. This patch leaves hub delivery unchanged.
- Known issue: a refusal for a linked or foreign-owned directory does not
  yet name the exact path in the local log; this is a later-build improvement.

**Action:** Install from the tag on test-codex and test-claude only. Rerun
the stand-in gate on test-codex. Do not run test-claude rows until arch
releases that seat after the separate typed-but-not-submitted handoff fault.
This tag is not the operator's Google beta acceptance.

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
