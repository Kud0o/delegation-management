# Delegation mailbox protocol

## Topology

The protocol has two trusted roles and two directed single-slot channels:

- delegator -> `delegatee.message.json`
- delegatee -> `delegator.message.json`

Each role also owns one disposable wake-listener PID record. The sender never kills the peer agent. It terminates only the peer's listener child after the message is durable.

## Mailbox envelope

```json
{
  "protocol_version": 1,
  "role": "delegatee",
  "sequence": 7,
  "status": "pending",
  "message": {
    "message_id": "uuid",
    "task_id": "TASK-001",
    "sequence": 7,
    "type": "assignment",
    "from": "delegator",
    "to": "delegatee",
    "subject": "Implement bounded change",
    "body": "Self-contained instructions",
    "reply_to": null,
    "created_at": "UTC ISO-8601 timestamp",
    "metadata": {}
  },
  "updated_at": "UTC ISO-8601 timestamp"
}
```

Mailbox states:

- `empty`: no message has been written since reset.
- `pending`: durable unread/unconsumed message.
- `consumed`: receiver read and accepted responsibility for processing the message.

The mailbox stays as an audit snapshot until the next message overwrites it.

## Presence files

`<role>.presence.json` records `{role, pid, updated_at}` and is touched by every role-bearing command (`init --role`, `send`, `receive`, `wait`, `await-reply`, `request`, `takeover`). It answers "has the peer ever run, and when was it last seen" — used by `peers` and by `--require-peer` (exit 8 when the peer file is missing). `reset` deletes it. Presence is advisory: it proves past activity, not current liveness.

## Output modes

Transaction commands (`send`, `receive`, `wait`, `await-reply`, `request`, `takeover`, `init`, `peers`) print compact single-line JSON with a token-lean message projection (`message_id`, `task_id`, `type`, `from`, `subject`, `body`, `reply_to`, non-empty `metadata`); constant envelope fields are omitted. `--pretty` switches to indented output. `status` and `history` always print full detail. The on-disk envelope always retains every field.

## History file

`history.jsonl` in the bus directory is an append-only diagnostic trail: one JSON record per line, `{"event": "sent", "at": ..., "message": {...}}` on delivery and `{"event": "consumed", "at": ..., "role", "message_id", "task_id", "type"}` on consumption. It survives mailbox overwrites and resets. It is not part of delivery semantics: history append failures never fail a send, torn lines are skipped on read, and agents must not treat it as an inbox. Inspect with the `history` command.

## PID record

```json
{
  "protocol_version": 1,
  "role": "delegatee",
  "pid": 12345,
  "token": "uuid",
  "state": "listening",
  "created_at": "UTC ISO-8601 timestamp",
  "updated_at": "UTC ISO-8601 timestamp"
}
```

`pid` is the disposable child process. `token` prevents an older waiter from clearing a newer listener registration. `state` is normally `idle`, `listening`, or `stale`; `identity-mismatch` marks a record whose PID no longer belongs to the tokenized listener.

## Send transition

1. Read recipient mailbox.
2. Refuse to overwrite `pending` unless `--force` is used (`takeover` forces deliberately).
3. Increment recipient mailbox sequence.
4. Write a complete new envelope to a temporary file in the same directory.
5. Flush and `fsync` the temporary file.
6. Atomically replace recipient mailbox with `os.replace`.
7. Read recipient PID record.
8. Verify the live PID command line contains the expected listener command and unique token, preventing stale-PID reuse.
9. If identity matches, terminate the listener (SIGTERM on POSIX, TerminateProcess on Windows).
10. Return message delivery and notification status separately.

## Wait transition

1. Read own mailbox. If already `pending`, return immediately.
2. Start a disposable child process.
3. Atomically publish its PID and a unique token.
4. Re-read own mailbox to close the registration race.
5. Wait for child exit or timeout.
6. Read own mailbox and mark pending message `consumed` unless peeking.
7. Stop any remaining child.
8. Clear PID record only when its token still matches this waiter.

Omitted `--timeout` uses role defaults: 600 seconds for `delegatee`, 300 seconds for `delegator`. `--timeout 0` waits indefinitely (passive `wait` only).

## Await-reply transition

`await-reply` layers a reply contract on top of `wait`:

1. Resolve the deadline (role default when omitted; `--timeout 0` is rejected because a no-reply decision needs a finite deadline).
2. Loop `wait` with the remaining time.
3. A consumed message whose `task_id` matches and whose `type` is in `--expect` ends the wait with exit 0.
4. A same-task `progress` or `heartbeat` that is not expected is recorded as an interim message and the loop continues; interim messages are included in the final JSON.
5. Any other consumed message (wrong task, or a decision-changing type such as `error` when only `result` was expected) ends the wait with exit 6 and the message in the output. It is already consumed; the caller must evaluate it.
6. Deadline expiry ends the wait with exit 4.

## Takeover transition

1. Read own mailbox one final time. A pending message for the same task refuses the takeover with exit 7; consume and evaluate it instead.
2. Otherwise write a `takeover` message to the peer inbox with force semantics, replacing any unconsumed message so the resumed peer sees the revocation first.
3. The resumed peer must stop the revoked task, keep useful partial state, and wait for a new assignment.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success: delivered, consumed, reply received, or takeover sent |
| 2 | Runtime error (invalid JSON state, I/O failure) |
| 3 | `receive`: inbox empty |
| 4 | `wait` / `await-reply`: deadline expired without a message / valid reply |
| 5 | `wait`: woke without a pending message (spurious wake) |
| 6 | `await-reply`: terminal message did not match `--expect` (already consumed) |
| 7 | `takeover`: refused because a same-task reply is already pending |
| 8 | `--require-peer`: the peer role has never used this bus |

`selftest` exits 0 when every end-to-end check passes, 1 otherwise. `request` exits with its `await-reply` phase's code.

## Why the listener is disposable

Putting the agent or terminal PID in the file would make a normal notification terminate useful work. A dedicated child makes process termination an event primitive while preserving the agent and its parent command.

## Platform notes

- POSIX: liveness via `os.kill(pid, 0)`, identity via `/proc/<pid>/cmdline` or `ps`, wake via `SIGTERM`.
- Windows: liveness via `OpenProcess`/`GetExitCodeProcess` (never `os.kill(pid, 0)`, which would terminate the process), identity via `Get-CimInstance Win32_Process`, wake via `TerminateProcess` (`os.kill` with `SIGTERM`). The listener child dies hard without running handlers; it holds no state, so this is safe. The identity check adds one PowerShell invocation (~1-3 s) per notify; the message file is durable before it runs.
- The listener child is started detached (`start_new_session` on POSIX, `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP` on Windows) so terminating it never affects the agent's process group.

## Delivery semantics

- Message durability: at-least-once visibility through the message file.
- Wake signal: best effort.
- Processing: effectively once only when both agents honor the single-pending-message rule and task/message IDs.
- Ordering: monotonically increasing per recipient mailbox.

The file is authoritative; the signal is only an optimization.

## Known boundaries

This is intentionally a small, two-agent local protocol. It is not a transactional queue. It does not support concurrent writers to one inbox, multiple pending messages, hostile users, cross-host delivery, or guaranteed recovery after arbitrary filesystem corruption.

For stronger needs, use SQLite with transactions, a Unix domain socket, Redis, NATS, RabbitMQ, or an orchestration platform.
