---
name: delegation-management
description: Coordinate exactly two coding agents as delegator and delegatee through file-based mailboxes and process-kill wake notifications. Use for assigning work, acknowledgements, progress, questions, results, cancellation, and handoff between two local agents sharing a filesystem. Do not use for more than two agents, untrusted cross-user environments, or network-distributed agents.
---

# Delegation Management

Coordinate two agents with explicit roles and a deterministic local protocol.

## Roles

### Delegator

Owns the overall objective and remains accountable for integration.

1. Define the task, boundaries, expected output, acceptance checks, and relevant paths.
2. Send one `assignment` to the delegatee.
3. Wait for `ack`, `question`, `progress`, `result`, or `error` messages using finite reply deadlines.
4. Treat an expired reply deadline as suspected unavailability, not proof of failure. Check once for a late reply, then send `takeover` and reclaim the task when policy allows.
5. Answer blockers promptly and avoid changing delegated files unless cancellation or takeover is explicitly communicated.
6. Validate the result, integrate it, and send a final response or a corrective assignment.

The delegator must not delegate vague ownership. Every assignment needs a task ID, concrete deliverable, allowed scope, and completion criteria.

### Delegatee

Owns execution of the accepted assignment within the stated scope.

1. Receive and inspect the assignment.
2. Send `ack` before making changes, restating scope and assumptions.
3. Work only inside the assigned boundaries.
4. Send `question` before guessing when a blocker changes correctness or scope.
5. Send concise `progress` at meaningful milestones, not for every minor action.
6. Send `result` with changed paths, tests/checks run, limitations, and follow-up needs. The result remains durable even if the delegator is temporarily rate-limited.
7. If the delegator misses a response deadline, follow the assignment's fallback policy: continue only within pre-authorized scope, otherwise pause safely and leave a durable `result` or `error`.
8. Stop safely on `cancel` or `takeover`, preserve useful work, and do not publish late changes without a new assignment.

## Shared-files protocol

Use a shared directory, normally `.delegation/`. There are exactly two persistent files per role:

- `delegator.message.json`: delegator inbox; written by delegatee, read by delegator.
- `delegator.pid`: disposable delegator listener metadata; written by delegator-side tooling, terminated by delegatee-side tooling.
- `delegatee.message.json`: delegatee inbox; written by delegator, read by delegatee.
- `delegatee.pid`: disposable delegatee listener metadata; written by delegatee-side tooling, terminated by delegator-side tooling.

Never place the working agent process ID in a `.pid` file. The helper creates a disposable listener child. The peer writes the message first, then terminates that listener (`SIGTERM` on POSIX, `TerminateProcess` on Windows). Listener termination is the wake notification; the agent remains alive. The helper works unchanged on Linux, macOS, and Windows.

## Mandatory ordering

For every message:

1. Ensure the recipient inbox has no unconsumed message.
2. Atomically write the complete recipient message envelope.
3. Read the recipient PID file.
4. If an active listener exists, terminate that disposable listener.
5. Treat the message file as authoritative. A missing or stale listener means notification failed, not message delivery.
6. Do not send another message in the same direction until the prior message is consumed, unless deliberate overwrite with `--force` is explicitly justified.

This write-before-notify order prevents the receiver from waking before content is durable.

## Helper commands

Set the helper path once:

```bash
BUS_TOOL="<skill-directory>/scripts/delegation_bus.py"
BUS_DIR=".delegation"
python "$BUS_TOOL" init --dir "$BUS_DIR"
```

On Windows PowerShell the same commands work with PowerShell variable syntax:

```powershell
$BUS_TOOL = "<skill-directory>\scripts\delegation_bus.py"
python $BUS_TOOL init --dir .delegation
```

Send from delegator to delegatee:

```bash
python "$BUS_TOOL" send \
  --dir "$BUS_DIR" \
  --from-role delegator \
  --type assignment \
  --task-id TASK-001 \
  --subject "Implement bounded change" \
  --body "Deliverable, scope, constraints, and acceptance checks."
```

Wait as delegatee. The command creates a disposable child, publishes its PID, waits for the peer to terminate it, reads the inbox, and marks the message consumed. With no explicit timeout, a delegatee waiter runs for **10 minutes (600 seconds)**:

```bash
python "$BUS_TOOL" wait --dir "$BUS_DIR" --role delegatee
```

A delegator waiter defaults to **5 minutes (300 seconds)**:

```bash
python "$BUS_TOOL" wait --dir "$BUS_DIR" --role delegator
```

Reply from delegatee:

```bash
python "$BUS_TOOL" send \
  --dir "$BUS_DIR" \
  --from-role delegatee \
  --type ack \
  --task-id TASK-001 \
  --subject "Assignment accepted" \
  --body "I will change X only and validate with Y."
```

Timeouts are role-specific when `--timeout` is omitted:

- `--role delegatee`: 600 seconds (10 minutes).
- `--role delegator`: 300 seconds (5 minutes).

For passive `wait`, explicit `--timeout 0` means wait indefinitely. For `await-reply`, zero is rejected because a no-reply decision needs a finite deadline. The same role defaults apply to `await-reply`:

```bash
python "$BUS_TOOL" await-reply \
  --dir "$BUS_DIR" \
  --role delegator \
  --task-id TASK-001 \
  --expect ack
```

The example above waits 300 seconds. Override `--timeout` for a task-specific lease.

If this exits with code `4`, the peer is unresponsive. Reclaim ownership after the helper performs a final late-reply check:

```bash
python "$BUS_TOOL" takeover \
  --dir "$BUS_DIR" \
  --role delegator \
  --task-id TASK-001 \
  --reason "No acknowledgement before the deadline; peer may be rate-limited"
```

Inspect without consuming:

```bash
python "$BUS_TOOL" receive --dir "$BUS_DIR" --role delegator --peek
```

Inspect all state:

```bash
python "$BUS_TOOL" status --dir "$BUS_DIR"
```

Reset stale state only when no valid message needs preservation:

```bash
python "$BUS_TOOL" reset --dir "$BUS_DIR" --role all
```

## Message types and required content

- `assignment`: objective, deliverable, allowed paths/scope, forbidden changes, acceptance checks, task ID.
- `ack`: restated scope, assumptions, immediate plan.
- `progress`: completed milestone, current state, next step, blocker if any.
- `question`: one precise decision needed, options and impact when known.
- `response`: direct answer to a prior question; set `--reply-to` when available.
- `result`: outcome, changed files, checks run and results, unresolved risks.
- `cancel`: stop condition, whether to preserve or revert partial work.
- `error`: failure details, reproducibility, safe recovery suggestion.
- `heartbeat`: use sparingly only to renew a progress lease during long work.
- `takeover`: revokes the peer's ownership after a no-reply deadline; a resumed peer must stop and avoid publishing late changes.

## No-reply and rate-limit recovery

A missed deadline cannot prove that an agent hit a model limit; it can also mean a crash, interruption, or slow work. The protocol therefore uses **leases and safe takeover**, not assumptions.

Every assignment should define these values in its body or metadata:

- `ack_timeout_seconds`: default 300 seconds while the delegator is waiting. The delegatee should still acknowledge as soon as practical.
- `progress_timeout_seconds`: default 300 seconds while the delegator is waiting. For tasks that naturally need longer between updates, set a larger explicit timeout; otherwise send `progress` or `heartbeat` before the five-minute lease expires.
- `delegator_reply_timeout_seconds`: default 600 seconds while the delegatee waits for an answer to a blocking question.
- `on_delegatee_timeout`: normally `delegator_takeover`.
- `on_delegator_timeout`: `continue_safe_default` only when the default is written in advance; otherwise `pause_and_persist`.

Required sequence after a deadline expires:

1. Run `await-reply`; exit code `4` means no valid reply arrived by the deadline.
2. Inspect the local inbox one final time. The `takeover` command performs this check automatically.
3. If a late reply exists, do not take over; consume and evaluate it.
4. Otherwise write a durable `takeover` message to the silent peer. This can replace an unconsumed assignment for the same task.
5. Reclaim, reassign, or safely pause according to the assignment policy.
6. If the silent agent resumes later, it reads `takeover`, stops duplicate work, and reports any useful partial state only after receiving a new assignment.

Special case: after the delegatee sends `result`, it does not need an immediate reply. The result remains in `delegator.message.json` until the delegator returns and consumes it.

## Operating rules

- One outstanding message per direction. This prevents overwrite because each role has only one message file.
- The receiver consumes a message before the sender sends the next one in that direction.
- Use task IDs consistently across all messages for one delegation.
- Do not infer success from listener termination; read and validate the inbox envelope.
- Do not infer delivery failure from absent listener; the receiver may poll and still consume the durable message.
- Omitted timeouts use 600 seconds for a waiting delegatee and 300 seconds for a waiting delegator. Use `wait --timeout 0` only when an explicitly indefinite passive listener is desired.
- Use finite `await-reply` whenever correctness depends on the peer answering. Its omitted timeout uses the same role-specific defaults.
- Never assume a timeout identifies the cause. Report `peer-unresponsive`, preserve state, and apply the declared fallback policy.
- Avoid `--force`. Use it only to replace a known obsolete message, and record why in metadata.
- Keep message bodies self-contained because the agents may not share conversational context.
- Never use this protocol across mutually untrusted OS users. A PID signal and shared writable directory require a trusted local boundary. The helper verifies the listener token before signaling to reduce stale-PID reuse risk.
- For three or more agents, queues, concurrent senders, or remote machines, replace this skill with a real broker or orchestrator.

## Recovery

When notification appears lost:

1. Run `status`.
2. Check the recipient inbox first.
3. If the message is pending, the recipient should `receive` it; do not resend.
4. If the listener record is stale, the next `wait` replaces it.
5. When an expected reply misses its deadline, run `takeover`; it refuses takeover if a late same-task reply is already pending.
6. Reset only after preserving any pending message content.

Read `references/protocol.md` for the envelope schema, state transitions, race handling, and security notes.
