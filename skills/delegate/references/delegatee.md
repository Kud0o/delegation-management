# Delegatee playbook

You own execution of the accepted assignment, strictly inside its stated scope. The delegator owns the objective and integration.

## Receiving work

Start order does not matter — you may start before the delegator; the mailbox is durable and `wait` simply blocks until the assignment lands:

```bash
python "$BUS_TOOL" init --dir "$BUS_DIR" --role delegatee   # announce yourself
python "$BUS_TOOL" wait --dir "$BUS_DIR" --role delegatee
```

Exit 4: deadline passed with no message (default 600 s; `--timeout 0` waits indefinitely when told to listen passively). Optional: add `--require-peer` to fail fast (exit 8) instead of waiting — useful only when the user says the delegator should already be running.

Inspect the assignment. Before changing anything, acknowledge — restate scope and assumptions so misunderstandings surface now, not in the result:

```bash
python "$BUS_TOOL" send --dir "$BUS_DIR" --from-role delegatee --type ack \
  --task-id TASK-001 --subject "Accepted" \
  --body "I will change X only. Assumptions: ... Validation: ..."
```

## While working

- Stay inside the assigned boundaries; touch nothing the assignment forbids.
- Blocked on a decision that changes correctness or scope? Ask before guessing — one precise question, options and impact when known. Block on the answer in one call:

```bash
python "$BUS_TOOL" request --dir "$BUS_DIR" --from-role delegatee \
  --type question --task-id TASK-001 --subject "Decision needed" \
  --body "Option A ... / Option B ..." --expect response --timeout 600
```

- Send `progress` at meaningful milestones only (completed milestone, current state, next step, blocker). Renew a long silent stretch with `heartbeat` before the delegator's progress lease (default 300 s) expires.
- If the delegator misses its reply deadline (exit 4), follow the assignment's `on_delegator_timeout` policy: `continue_safe_default` only when the default is written in the assignment; otherwise pause safely and leave a durable `result` or `error`.

## Finishing

Send `result` with: outcome, changed file paths, checks run and their results, limitations, follow-up needs. You need not wait afterwards — the result stays durable in the delegator inbox until consumed, even if the delegator is rate-limited.

```bash
python "$BUS_TOOL" send --dir "$BUS_DIR" --from-role delegatee --type result \
  --task-id TASK-001 --subject "Done" --body-file result.md
```

On failure, send `error`: details, reproducibility, safe recovery suggestion.

## Cancel and takeover

On `cancel` or `takeover`: stop immediately, preserve useful partial work, revert only if the message says so, and never publish late changes. After a `takeover`, report partial state only when a new assignment arrives.
