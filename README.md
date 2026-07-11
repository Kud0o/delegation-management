# delegation-management

A Claude Code / Agent SDK skill that coordinates exactly two local coding agents — a **delegator** and a **delegatee** — through file-based mailboxes with process-kill wake notifications.

## How it works

- Each role has one inbox file (`<role>.message.json`) and one disposable listener PID record (`<role>.pid`) in a shared `.delegation/` directory.
- A sender atomically writes the peer's inbox, then terminates the peer's disposable listener child. Listener death is the wake signal; the agent process is never touched.
- The message file is authoritative; the wake signal is only an optimization. Works on Linux, macOS, and Windows.

## Layout

```
SKILL.md                     skill entry point (roles, commands, policies)
scripts/delegation_bus.py    zero-dependency Python 3.9+ helper CLI
references/protocol.md       envelope schema, state transitions, exit codes
```

## Quick start

```bash
python scripts/delegation_bus.py selftest    # verify this machine end-to-end
python scripts/delegation_bus.py init --dir .delegation
python scripts/delegation_bus.py send --dir .delegation --from-role delegator \
  --type assignment --task-id TASK-001 --subject "Do X" --body "Scope, checks."
python scripts/delegation_bus.py wait --dir .delegation --role delegatee
python scripts/delegation_bus.py await-reply --dir .delegation --role delegator \
  --task-id TASK-001 --expect ack
python scripts/delegation_bus.py history --dir .delegation --task-id TASK-001
```

## Features

- Atomic, durable single-slot mailboxes; the wake signal (listener kill) is only an optimization.
- `await-reply` with reply contracts: interim `progress`/`heartbeat` pass through, unexpected terminal messages and timeouts get distinct exit codes.
- Safe `takeover` after missed deadlines with a built-in final late-reply check.
- Append-only `history.jsonl` audit trail surviving mailbox overwrites (`history` command with task/type/event filters).
- `--body-file` / stdin for long multi-line message bodies.
- Built-in `selftest` (14 end-to-end checks) — no test framework, no dependencies, Python 3.9+ stdlib only.

See `SKILL.md` for the full protocol: acknowledgements, progress leases, blocking questions, results, cancellation, and safe takeover after missed deadlines.

## Install as a skill

Copy this directory to `~/.claude/skills/delegation-management/`.

## Scope

Two trusted agents on one machine sharing a filesystem. Not for 3+ agents, untrusted users, or networked delivery — use a real broker for that.
