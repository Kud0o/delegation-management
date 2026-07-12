# delegate

**Let two coding agents work together on one machine — one assigns the work, the other does it.**

`delegate` is a [Claude Code](https://claude.com/claude-code) / Agent SDK skill. You give each agent a role in one chat line; the agents handle everything else themselves — assignments, acknowledgements, questions, progress, results, and recovery when one side goes silent.

No server, no broker, no dependencies: the agents coordinate through JSON files in a shared `.delegation/` directory, with an instant process-based wake-up. Works on Linux, macOS, and Windows.

## Install

```bash
git clone https://github.com/Kud0o/delegation-management.git
cp -r delegation-management ~/.claude/skills/delegate
```

Optionally verify the machine once (or ask an agent to): `python ~/.claude/skills/delegate/scripts/delegation_bus.py selftest` must end with `"ok": true`.

## Use

Open **two Claude Code sessions in the same project directory** and type one line into each:

**Session A:**

> /delegate delegator — assign the other agent to add input validation to src/signup.py, then wait for its result and review it.

**Session B:**

> /delegate delegatee — wait for tasks from the other agent and do them.

That's all. Plain phrases work too ("delegate this to the other agent", "wait for delegated work").

If something looks stuck, ask either agent to check — it will read the mailbox state and the audit trail (`status`, `history`) and tell you what happened.

## How it works, in one paragraph

The delegator writes a task file into the delegatee's mailbox, then terminates a small disposable listener process the delegatee left behind — that termination is the instant wake-up call (the agent itself is never touched). The file is authoritative, the signal is just speed: a missed notification loses nothing. One message per direction at a time, explicit acknowledgements, finite deadlines, and a safe takeover procedure when a peer goes silent.

## Want the details?

You only need them if you're debugging the bus or extending it — the agents read these themselves:

- [`SKILL.md`](SKILL.md) — role routing, command reference, rules
- [`references/delegator.md`](references/delegator.md) / [`references/delegatee.md`](references/delegatee.md) — per-role playbooks
- [`references/protocol.md`](references/protocol.md) — wire format, state transitions, exit codes, platform notes

Scope: exactly two trusted agents on one machine. For 3+ agents or remote machines, use a real broker instead.

## License

MIT — see [LICENSE](LICENSE).
