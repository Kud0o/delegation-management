---
name: delegate
description: Coordinate exactly two local coding agents — a delegator that assigns work and a delegatee that executes it — through file-based mailboxes with instant wake notifications. Run /delegate delegator in one session and /delegate delegatee in another, both in the same project directory. Two trusted agents on one machine only.
argument-hint: "[delegator|delegatee]"
disable-model-invocation: true
---

# Delegate

Coordinate two agents with explicit roles and a deterministic local protocol. The message file is authoritative; the wake signal (terminating a disposable listener process) is only an optimization. Works unchanged on Linux, macOS, and Windows.

## Pick your role, read only its playbook

This skill is invoked explicitly: `/delegate delegator` or `/delegate delegatee`.

| Argument | You are | Read now |
|----------|---------|----------|
| `delegator` | You assign work, answer questions, review results | `references/delegator.md` |
| `delegatee` | You receive work and execute it in scope | `references/delegatee.md` |

Read exactly one playbook; do not load both. No argument given? Infer the role from the user's request (assigning work → delegator; waiting for work → delegatee) and confirm it in one line before starting.

## Setup (both roles)

```bash
BUS_TOOL="<skill-directory>/scripts/delegation_bus.py"
BUS_DIR=".delegation"   # must be the same directory for both agents
```

First use on a machine: `python "$BUS_TOOL" selftest` must print `"ok": true`.

All commands print compact single-line JSON to keep context small; add `--pretty` only for humans. Long or multi-line bodies: `--body-file <path>` (`-` for stdin) instead of `--body`.

## Command reference (shared)

| Command | Purpose |
|---------|---------|
| `init --role R` | Create protocol files, announce presence |
| `send` | Deliver one message, wake the peer |
| `request` | `send` + `await-reply` in one call (default `--expect ack`) |
| `wait --role R` | Block until a message arrives; `--require-peer` exits 8 if the peer never ran |
| `await-reply --expect T` | Wait for a reply type on a task; interim `progress`/`heartbeat` pass through |
| `receive [--peek]` | Read inbox now, without blocking |
| `takeover --reason ...` | Reclaim ownership after a missed deadline (refuses if a late reply is pending) |
| `peers` | Who has used this bus and how recently |
| `status` / `history` / `reset` / `selftest` | Inspect, audit, clear, verify |

Exit codes: 0 ok, 3 empty inbox, 4 timeout, 5 spurious wake, 6 unexpected reply (already consumed — evaluate it), 7 takeover refused (late reply pending), 8 peer never ran.

Message types: `assignment` `ack` `progress` `question` `response` `result` `cancel` `error` `heartbeat` `takeover`.

Default `--timeout` when omitted: 600 s waiting as delegatee, 300 s as delegator. `wait --timeout 0` waits indefinitely; `await-reply`/`request` reject 0.

## Rules (both roles)

- One outstanding message per direction; consume before the sender sends the next. Avoid `--force` except to replace a known-obsolete message, and record why.
- Use the same task ID across all messages of one delegation. Bodies must be self-contained — the agents do not share conversational context.
- Never infer delivery from the wake signal or its absence; when in doubt read the inbox (`status`, then `receive`) — do not resend.
- Never use across mutually untrusted OS users; for 3+ agents, queues, or remote machines use a real broker instead.

Wire format, state transitions, race handling, platform notes: `references/protocol.md` (only needed when debugging the bus itself).
