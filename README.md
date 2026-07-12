# delegate

**Let two coding agents work together on one machine — one assigns the work, the other does it.**

`delegate` is an agent skill for any AI coding agent that can run shell commands — Claude Code, Cursor, Codex CLI, a custom Agent SDK loop, or two different agents entirely. You give each agent a role; the agents handle the rest themselves.

- The **delegator** owns the objective: it assigns tasks, answers questions, and reviews results.
- The **delegatee** executes each task inside the scope it was given.

No server, no broker, no dependencies: the agents coordinate through JSON files in a shared `.delegation/` directory, with an instant process-based wake-up (one stdlib-only Python file). Works on Linux, macOS, and Windows.

```
Delegator agent                        Delegatee agent
   |                                        |
   |  1. writes assignment  ──────────►  delegatee.message.json
   |  2. wakes the delegatee ──────────►  (reacts instantly, no polling)
   |                                        |
   |  4. wakes, reads result            3. works, writes result
 delegator.message.json  ◄──────────────  |
```

The message file is always authoritative; the wake signal is just speed. A missed notification loses nothing.

## Install

One command — auto-detects your installed agents (Claude Code, Cursor, Codex, OpenCode, and 70+ more) and installs to the ones you pick:

```bash
npx skills add Kud0o/delegation-management
```

Manual alternative:

```bash
git clone https://github.com/Kud0o/delegation-management.git
cp -r delegation-management/skills/delegate ~/.claude/skills/delegate
```

For an agent without a skills directory: clone anywhere and tell the agent to read `skills/delegate/SKILL.md` — that file routes it to its role instructions.

Optionally verify the machine once (or ask an agent to): `python ~/.claude/skills/delegate/scripts/delegation_bus.py selftest` must end with `"ok": true`.

## Update

Pull the latest version of the skill:

```bash
npx skills update delegate
```

Omit the name to update every installed skill (`npx skills update`). To see what's installed use `npx skills list`, and to remove it `npx skills remove delegate`. If you installed manually, re-run the `git clone` and `cp` from above.

## Use

Open **two agent sessions in the same project directory** and give each one a role:

**Session A:**

> /delegate delegator — assign the other agent to add input validation to src/signup.py, then wait for its result and review it.

**Session B:**

> /delegate delegatee — wait for tasks from the other agent and do them.

That's all. The skill triggers **only** on explicit `/delegate` invocation — it never auto-loads from prompt text, so it costs nothing and interferes with nothing until you call it. If something looks stuck, ask either agent to check — it reads the mailbox state and audit trail and tells you what happened.

## The flow

What the two agents do between your two chat lines and the finished task:

1. **Assign.** The delegator announces itself, then sends an `assignment` with a task ID, concrete deliverable, allowed scope, and acceptance checks — and waits for the acknowledgement in the same call (`request --expect ack`).
2. **Accept.** The delegatee blocks until the assignment arrives and replies with an `ack` restating scope and assumptions, *before touching any file*. Misunderstandings surface here, not in the result. Start order doesn't matter — either agent may start first; messages are durable until consumed. (An optional `--require-peer` check lets the delegatee fail fast instead of waiting when the delegator is expected to be already running.)
3. **Work.** The delegatee stays inside the assigned scope. Blocked on a decision that changes correctness? It sends a `question` and blocks on the `response` — the delegator answers with the decision. Long tasks emit `progress` at milestones; these pass through the delegator's wait automatically without ending it.
4. **Deliver.** The delegatee sends a `result`: outcome, changed files, checks run, limitations. It doesn't need to wait around afterwards — the result stays durable until the delegator consumes it, even if the delegator is rate-limited at that moment.
5. **Review.** The delegator validates the result against the acceptance checks, then integrates it or sends a corrective assignment under the same task ID. The whole exchange is replayable afterwards from the append-only audit trail (`history --task-id ...`).

**One message per direction at a time.** A sender may not overwrite an unconsumed message, so nothing is ever lost mid-conversation.

### When one agent goes silent

A missed deadline never proves what happened — rate limit, crash, or just slow work — so recovery is explicit:

1. The waiting agent's deadline expires (all waits are finite by default: 5 min for a delegator, 10 min for a delegatee).
2. The delegator runs `takeover`, which first makes a final check for a late reply. If one is already sitting in its inbox, the takeover is **refused** — evaluate the reply instead.
3. Otherwise a durable `takeover` message replaces whatever is unconsumed in the peer's inbox. If the silent agent ever resumes, it reads that first, stops immediately, and never publishes late changes.
4. A delegatee whose *delegator* went silent follows the fallback declared in the assignment: continue within a pre-authorized safe default, or pause and leave a durable `result`/`error`.

## Commands at a glance

These are run by the agents; you'll mostly see them in the transcript.

| Command | Purpose |
|---------|---------|
| `init --role R` | Create the protocol files, announce presence |
| `send` / `receive` | Deliver one message / read the inbox now |
| `request` | Send and wait for the reply in one call |
| `wait` | Block until a message arrives (`--require-peer` = fail fast if the peer never ran) |
| `await-reply --expect T` | Wait for a specific reply type on a task |
| `takeover` | Reclaim ownership after a missed deadline |
| `peers` / `status` / `history` | Who's here · full state · audit trail |
| `reset` / `selftest` | Clear state · verify the machine (18 checks) |

Message types: `assignment` `ack` `progress` `question` `response` `result` `cancel` `error` `heartbeat` `takeover`.

## Details

Only needed when debugging or extending the bus — the agents read these themselves:

- [`SKILL.md`](skills/delegate/SKILL.md) — role routing, full command reference, rules
- [`delegator.md`](skills/delegate/references/delegator.md) / [`delegatee.md`](skills/delegate/references/delegatee.md) — per-role playbooks
- [`protocol.md`](skills/delegate/references/protocol.md) — wire format, state transitions, exit codes, platform notes

Scope: exactly two trusted agents on one machine. For 3+ agents or remote machines, use a real broker instead.

## License

MIT — see [LICENSE](LICENSE).
