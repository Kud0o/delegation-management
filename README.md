# delegate

**Let two coding agents work together on one machine — one assigns the work, the other does it.**

`delegate` is a [Claude Code](https://claude.com/claude-code) / Agent SDK skill that coordinates exactly two local agents:

- the **delegator** owns the objective, assigns tasks, and integrates the results;
- the **delegatee** executes each task inside the scope it was given.

**You never type the commands in this README.** You give each agent a role in plain English (or with `/delegate`), describe the task, and the agents run everything below on their own. The Python CLI is the machinery *they* use — it is documented here so you can see how it works and audit what they did.

Under the hood the agents communicate through plain JSON files in a shared `.delegation/` directory, with a process-based wake-up trick so a waiting agent reacts instantly instead of polling. No server, no broker, no dependencies — one Python file.

```
Delegator agent                        Delegatee agent
   |                                        |
   |  1. writes assignment  ──────────►  delegatee.message.json
   |  2. kills wake listener ─────────►  (delegatee wakes instantly)
   |                                        |
   |  4. wakes, reads result            3. works, writes result
 delegator.message.json  ◄──────────────  |
```

The message **file** is always authoritative; the wake signal is just an optimization. If a notification is ever missed, the message is still there. Works unchanged on **Linux, macOS, and Windows** (Python 3.9+, standard library only).

---

## Installation

```bash
git clone https://github.com/Kud0o/delegation-management.git
cp -r delegation-management ~/.claude/skills/delegate
```

Then verify the machine once — 18 end-to-end checks in a temporary directory. This is the **only command you ever run yourself** (and even this you can just ask an agent to do):

```bash
python ~/.claude/skills/delegate/scripts/delegation_bus.py selftest
# must end with "ok": true
```

## Quick start — this is all you do

Open **two Claude Code sessions in the same project directory** and type one line into each:

**Session A (the delegator):**

> /delegate delegator — assign the other agent to add input validation to src/signup.py, then wait for its result and review it.

**Session B (the delegatee):**

> /delegate delegatee — wait for tasks from the other agent and do them.

That's it. Agent A writes the assignment, agent B wakes up, acknowledges, works, asks questions if blocked, and delivers the result back to A — all through the protocol below, with no further input from you. Plain phrases work too ("delegate this to the other agent", "wait for delegated work"); `/delegate <role>` is just the most deterministic trigger.

Each agent reads only its own playbook (`references/delegator.md` or `references/delegatee.md`), keeping its context small.

---

## Under the hood: what the agents run

Everything in this section is executed **by the agents, not by you**. It is shown so you can understand the protocol and audit a session afterwards.

Terminal A is the delegator, terminal B is the delegatee. Both use the same `--dir`.

**A — announce yourself and send an assignment, waiting for the acknowledgement in one call:**

```bash
python delegation_bus.py init --dir .delegation --role delegator

python delegation_bus.py request --dir .delegation --from-role delegator \
  --type assignment --task-id TASK-001 \
  --subject "Add input validation" \
  --body "Validate email format in src/signup.py. Scope: that file only. Done when: tests in tests/test_signup.py pass." \
  --expect ack --timeout 300
```

**B — check that a delegator exists, then block until work arrives:**

```bash
python delegation_bus.py wait --dir .delegation --role delegatee --require-peer
# → {"wake":"listener-terminated","delivered":true,"message":{"type":"assignment",...}}
```

**B — accept before touching anything:**

```bash
python delegation_bus.py send --dir .delegation --from-role delegatee --type ack \
  --task-id TASK-001 --subject "Accepted" \
  --body "Will edit src/signup.py only; validating with tests/test_signup.py."
```

At this point A's `request` returns with the ack. A now waits for the outcome:

```bash
python delegation_bus.py await-reply --dir .delegation --role delegator \
  --task-id TASK-001 --expect result,error --timeout 900
```

**B — hit a blocker? Ask and wait for the answer in one call:**

```bash
python delegation_bus.py request --dir .delegation --from-role delegatee \
  --type question --task-id TASK-001 --subject "Decision needed" \
  --body "Reject plus-addressing (user+tag@x.com)? A: allow. B: reject." \
  --expect response --timeout 600
```

(A answers with `send --type response --reply-to <message_id>`. Interim `progress` messages pass through A's `await-reply` automatically without ending it.)

**B — deliver the result (long bodies go in a file):**

```bash
python delegation_bus.py send --dir .delegation --from-role delegatee --type result \
  --task-id TASK-001 --subject "Done" --body-file result.md
```

**A — audit the whole exchange afterwards:**

```bash
python delegation_bus.py history --dir .delegation --task-id TASK-001 --pretty
```

### When the other agent goes silent

A missed deadline never proves what happened (rate limit? crash? just slow?), so recovery is explicit and safe:

```bash
# await-reply exited 4 (timeout). Reclaim ownership:
python delegation_bus.py takeover --dir .delegation --role delegator \
  --task-id TASK-001 --reason "No result before deadline"
```

- Exit **7** — a late reply was already sitting in your inbox; the takeover is refused so you can `receive` and evaluate it instead.
- Exit **0** — a durable `takeover` message now replaces anything unconsumed in the peer's inbox. If the silent agent ever resumes, it reads that first, stops, and never publishes late changes.

---

## Command reference

| Command | What it does |
|---------|--------------|
| `init --role R` | Create the protocol files and announce your presence |
| `send` | Deliver one message and wake the peer |
| `request` | `send` + `await-reply` in a single call (default `--expect ack`) |
| `wait --role R` | Block until a message arrives; `--require-peer` fails fast if the peer never ran |
| `await-reply --expect T` | Wait for a specific reply type on a task |
| `receive [--peek]` | Read the inbox right now, without blocking |
| `takeover --reason ...` | Reclaim ownership after a missed deadline |
| `peers` | Show who has used this bus and how recently |
| `status` | Full dump: mailboxes, listeners, presence |
| `history` | Append-only audit trail with `--task-id` / `--type` / `--event` filters |
| `reset` | Stop listeners and clear all state |
| `selftest` | Verify this machine end to end |

**Message types:** `assignment` · `ack` · `progress` · `question` · `response` · `result` · `cancel` · `error` · `heartbeat` · `takeover`

**Exit codes:** `0` ok · `3` empty inbox · `4` timeout · `5` spurious wake · `6` unexpected reply (already consumed — evaluate it) · `7` takeover refused, late reply pending · `8` peer never ran

**Timeouts:** omitted `--timeout` defaults to 600 s for a waiting delegatee and 300 s for a waiting delegator. `wait --timeout 0` waits forever; `await-reply` and `request` reject 0 — a no-reply decision needs a real deadline.

**Output:** compact single-line JSON by default to keep agent context small (~70 % smaller than pretty-printing); add `--pretty` when a human is reading.

---

## Design notes

**Why files + process kill instead of sockets or a queue?** Two agents on one machine don't need infrastructure. An atomically-replaced JSON file gives durability and a natural single-slot handshake (a sender may not overwrite an unconsumed message). Killing a *disposable* listener child — never the agent itself — turns process termination into an instant, cross-platform wake-up. The `.pid` file stores a random token, and the sender verifies the process command line still carries it before signaling, so a recycled PID is never killed by mistake.

**Why Python?** It is the only zero-setup option that works everywhere coding agents run: preinstalled, single stdlib-only file, and it can reach the Windows process APIs (via ctypes) that portable shell scripts cannot.

**What this is not:** a message queue. No concurrent writers per inbox, no fan-out, no cross-host delivery, no untrusted users. For three or more agents or remote machines, use a real broker (Redis, NATS, RabbitMQ) or an orchestration platform.

## Repository layout

```
SKILL.md                     skill entry point — thin router the agent reads first
scripts/delegation_bus.py    the entire implementation (Python 3.9+, no deps)
references/delegator.md      delegator playbook (loaded only by that role)
references/delegatee.md      delegatee playbook (loaded only by that role)
references/protocol.md       wire format, state machines, exit codes (debugging)
```

## License

MIT — see [LICENSE](LICENSE).
