# Delegator playbook

You own the overall objective and remain accountable for integration. The delegatee owns execution inside the scope you define.

## Assigning work

Never delegate vague ownership. Every assignment needs: task ID, concrete deliverable, allowed paths/scope, forbidden changes, acceptance checks. Bodies must be self-contained — the delegatee does not share your conversation context. Long bodies: write to a file, use `--body-file`.

```bash
# announce yourself once, so the delegatee's --require-peer check passes
python "$BUS_TOOL" init --dir "$BUS_DIR" --role delegator

# send the assignment AND wait for the acknowledgement in one call
python "$BUS_TOOL" request --dir "$BUS_DIR" --from-role delegator \
  --type assignment --task-id TASK-001 \
  --subject "Implement bounded change" \
  --body-file assignment.md --expect ack --timeout 300
```

Declare leases in the assignment body so both sides share expectations:

- `ack_timeout_seconds` (default 300)
- `progress_timeout_seconds` (default 300; set larger for naturally slow work)
- `delegator_reply_timeout_seconds` (default 600 — how long the delegatee waits for your answers)
- `on_delegatee_timeout: delegator_takeover`
- `on_delegator_timeout: pause_and_persist` (or `continue_safe_default` only when the safe default is written in the assignment)

## While the delegatee works

Wait for the outcome with a finite deadline; interim `progress`/`heartbeat` pass through automatically:

```bash
python "$BUS_TOOL" await-reply --dir "$BUS_DIR" --role delegator \
  --task-id TASK-001 --expect result,error --timeout 900
```

Answer `question` messages promptly with `--type response --reply-to <message_id>`. Do not modify delegated files while the task is live — only after `cancel`, `takeover`, or an accepted `result`.

Exit 6 means a terminal message arrived that you did not expect (already consumed — evaluate it). Exit 4 means the deadline passed.

## Recovery after a missed deadline

A timeout proves nothing about the cause (rate limit, crash, slow work). Sequence:

1. `takeover --dir "$BUS_DIR" --role delegator --task-id TASK-001 --reason "..."` — it performs a final late-reply check itself.
2. Exit 7: a late same-task reply is pending. `receive` and evaluate it; do not take over.
3. Exit 0: the revocation is durable in the delegatee inbox (it replaces any unconsumed message there). Reclaim the files, reassign, or pause per policy.
4. If the silent delegatee resumes later, it reads `takeover`, stops, and reports partial state only after you send a new assignment.

## Closing the loop

Validate the `result` against the acceptance checks before integrating. Then either integrate and (optionally) send a closing `response`, or send a corrective `assignment` with the same task ID and what failed.
