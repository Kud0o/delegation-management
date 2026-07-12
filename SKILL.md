---
name: delegate
description: Delegate tasks between exactly two local coding agents — a delegator and a delegatee — through file-based mailboxes with process-kill wake notifications. Use when asked to delegate work to another agent, hand off a task, act as delegator or delegatee, wait for delegated work or replies, exchange progress, questions, or results between two agents, or take over from a silent peer. Two trusted agents sharing one filesystem only; not for three or more agents, untrusted users, or remote machines.
---

# Delegate

Coordinate two agents with explicit roles and a deterministic local protocol. The message file is authoritative; the wake signal (terminating a disposable listener process) is only an optimization. Works unchanged on Linux, macOS, and Windows.

## Chat triggers

- "Delegate this to the other agent" / "hand off X" / "assign X to agent B" → act as **delegator**.
- "Wait for tasks from the other agent" / "be the delegatee" / "listen for delegated work" → act as **delegatee**.
- "Check on the delegated task" / "the other agent is silent" → `await-reply`, then `takeover` if the deadline passed.

## Setup

```bash
BUS_TOOL="<skill-directory>/scripts/delegation_bus.py"
BUS_DIR=".delegation"
python "$BUS_TOOL" init --dir "$BUS_DIR" --role delegator   # or --role delegatee
```

`--role` announces your presence so the peer can verify you started. Run `python "$BUS_TOOL" selftest` once per machine; it must print `"ok": true`.

All commands print compact single-line JSON to keep agent context small; add `--pretty` only when a human is reading. For long or multi-line bodies use `--body-file <path>` (`-` for stdin) instead of `--body`.

## Delegator flow

Owns the objective, remains accountable for integration. Every assignment needs a task ID, concrete deliverable, allowed scope, and completion criteria — never delegate vague ownership. Message bodies must be self-contained; the agents do not share conversational context.

```bash
# one call: send the assignment AND wait for the acknowledgement
python "$BUS_TOOL" request --dir "$BUS_DIR" --from-role delegator \
  --type assignment --task-id TASK-001 \
  --subject "Implement bounded change" \
  --body "Deliverable, scope, forbidden changes, acceptance checks." \
  --expect ack

# later: wait for the outcome
python "$BUS_TOOL" await-reply --dir "$BUS_DIR" --role delegator \
  --task-id TASK-001 --expect result,error --timeout 900
```

Answer blockers promptly; do not change delegated files unless you have sent `cancel` or `takeover`. Validate the result, then integrate or send a corrective assignment.

## Delegatee flow

Owns execution inside the stated scope.

```bash
# verify a delegator exists, then block until work arrives
python "$BUS_TOOL" wait --dir "$BUS_DIR" --role delegatee --require-peer

# accept before changing anything
python "$BUS_TOOL" send --dir "$BUS_DIR" --from-role delegatee --type ack \
  --task-id TASK-001 --subject "Accepted" --body "I will change X only; validate with Y."
```

Send `question` before guessing when a blocker changes correctness or scope (use `request --type question --expect response` to block on the answer). Send `progress` at meaningful milestones. Finish with `result`: changed paths, checks run, limitations. Stop safely on `cancel` or `takeover`; never publish late changes without a new assignment.

## Commands

| Command | Purpose |
|---------|---------|
| `init --role R` | Create protocol files, announce presence |
| `send` | Deliver one message, wake the peer |
| `request` | `send` + `await-reply` in one call (default `--expect ack`) |
| `wait --role R` | Block until a message arrives; `--require-peer` exits 8 if the peer never ran |
| `await-reply --expect T` | Wait for a specific reply type on a task; interim `progress`/`heartbeat` pass through |
| `receive [--peek]` | Read inbox now, without blocking |
| `takeover --reason ...` | Reclaim ownership after a missed deadline (refuses if a late reply is pending) |
| `peers` | Who has used this bus and how recently |
| `status` / `history` / `reset` / `selftest` | Inspect, audit, clear, verify |

Exit codes: 0 ok, 3 empty inbox, 4 timeout, 5 spurious wake, 6 unexpected reply (already consumed — evaluate it), 7 takeover refused (late reply pending), 8 peer never ran.

## Message types

`assignment` (objective, deliverable, scope, forbidden changes, acceptance checks) · `ack` (restated scope, assumptions, plan) · `progress` · `question` (one precise decision) · `response` (set `--reply-to`) · `result` (outcome, changed files, checks, risks) · `cancel` · `error` · `heartbeat` (renew a lease during long work) · `takeover` (revokes peer ownership).

## Timeouts and recovery

Omitted `--timeout`: 600 s for a waiting delegatee, 300 s for a waiting delegator. `wait --timeout 0` waits indefinitely; `await-reply`/`request` reject 0 because a no-reply decision needs a finite deadline. Set task-specific leases in the assignment body: `ack_timeout_seconds`, `progress_timeout_seconds`, `delegator_reply_timeout_seconds`, plus fallback policies `on_delegatee_timeout: delegator_takeover` and `on_delegator_timeout: pause_and_persist` (or `continue_safe_default` only when the default is written in advance).

A missed deadline proves nothing about the cause (rate limit, crash, slow work). Sequence after `await-reply` exits 4:

1. `takeover --task-id ... --reason ...` — it performs a final late-reply check itself.
2. Exit 7 means a late reply is pending: `receive` and evaluate it instead.
3. Exit 0 means the revocation is durable in the peer inbox (it replaces any unconsumed message). Reclaim, reassign, or pause per policy.
4. A resumed peer reads `takeover`, stops duplicate work, and reports partial state only after a new assignment.

After the delegatee sends `result` it need not wait; the result stays durable until the delegator consumes it.

## Rules

- One outstanding message per direction; consume before the sender sends the next. Avoid `--force` except to replace a known-obsolete message, and record why.
- Use the same task ID across all messages of one delegation.
- Never infer delivery from the wake signal or its absence; read the inbox (`status`, then `receive`) when in doubt — do not resend.
- Never use across mutually untrusted OS users; for 3+ agents, queues, or remote machines use a real broker instead.

Read `references/protocol.md` for the envelope schema, state transitions, race handling, exit codes, and platform notes.
