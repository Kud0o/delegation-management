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
python scripts/delegation_bus.py init --dir .delegation
python scripts/delegation_bus.py send --dir .delegation --from-role delegator \
  --type assignment --task-id TASK-001 --subject "Do X" --body "Scope, checks."
python scripts/delegation_bus.py wait --dir .delegation --role delegatee
python scripts/delegation_bus.py await-reply --dir .delegation --role delegator \
  --task-id TASK-001 --expect ack
```

See `SKILL.md` for the full protocol: acknowledgements, progress leases, blocking questions, results, cancellation, and safe takeover after missed deadlines.

## Install as a skill

Copy this directory to `~/.claude/skills/delegation-management/`.

## Scope

Two trusted agents on one machine sharing a filesystem. Not for 3+ agents, untrusted users, or networked delivery — use a real broker for that.
