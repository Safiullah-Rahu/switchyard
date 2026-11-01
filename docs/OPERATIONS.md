# Operations runbook

## Daily

| When | What | Command |
|---|---|---|
| Morning | Read the digest: runs, approvals waiting, spend, open exceptions | `switchyard digest` (cron: `switchyard digest --post "#ops-exceptions"` at 08:00) |
| Through the day | Approve or reject what is waiting | console → *Waiting for a person*, or `switchyard approvals` |
| Through the day | Clear exceptions | console → *Open exceptions*, or `switchyard exceptions` |
| Weekly | Verify the audit chain; review spend by agent | `switchyard audit-verify`, `switchyard costs` |

## Exception kinds and what to do

| Kind | Meaning | Action |
|---|---|---|
| `tool_permanent` | An integration refused the request (scope, credentials, deleted record) | Fix the cause, then `switchyard retry <run> <step> --as <you>` |
| `tool_retries_exhausted` | A destination kept failing transiently | Check the provider's status page, then retry the step |
| `verification_failed` | The destination does not hold what was written | Look at the destination first. A retry reconciles before writing, so it is safe |
| `review_rejected` | The draft failed review repeatedly | Read the findings in `switchyard show <run>`, then correct the inputs or close the lead |
| `insufficient_data` | The estimate cannot be sized (no usage and no roof area) | Ask the customer, then submit the enriched enquiry |
| `needs_decision` | A choice with consequences is waiting | `switchyard decide <decision_id> <choice> --as <you>` |
| `policy_violation` | An agent tried something outside its permissions | Treat the inbound message as hostile. Nothing was done; review and close |
| `budget_exceeded` | A run hit its model budget | Investigate the run (loops, very long inputs) before raising the cap in `pricing.toml` |
| `agent_error` | A model call failed or returned an invalid shape repeatedly | Check the OpenAI status page and the trace for the run |
| `lease_recovered` | A worker stopped mid-step and another resumed it | None. Investigate if it recurs (memory, host restarts) |
| `approval_rejected` | A person rejected an action | None. The run ends without the action |

## Common tasks

**A worker host was restarted.** Nothing to do. On start, a worker recovers expired leases. Interrupted side
effects are reconciled with their destination before anything is retried.

**Pause all outbound actions** (incident, legal hold):
1. Stop the worker (`systemctl stop switchyard-worker`, or scale it to 0). Webhooks still land and wait.
2. To also stop new runs, rotate `SWITCHYARD_WEBHOOK_SECRET`; senders get 401 until it is restored.
3. Restart the worker to resume. Waiting runs continue from where they stopped.

**Rotate secrets.**
- `SWITCHYARD_APPROVAL_SECRET`: rotating it invalidates *pending and unused* approvals by design, so
  they will be requested again. Rotate after confirming nothing urgent is waiting.
- Integration tokens: update them in the secret store; no restart is needed if the token provider reloads.

**Change who can approve what.** Edit `policy.toml` (classes, roles, principals) through a reviewed pull
request and deploy. Approvals already given by someone who no longer holds the role stop counting.

**Change model routing or prices.** Edit `pricing.toml`. Cost reports use the new prices for calls made
after the change.

**Back up.** Back up the SQLite database file and its `-wal` file with `sqlite3 switchyard.db ".backup
backup.db"`, hourly. On Postgres, use the provider's point-in-time recovery. Export the audit-chain head
(`SELECT seq, hash FROM audit ORDER BY seq DESC LIMIT 1`) to write-once storage each day.

## Health

`GET /health` returns mode, pending approvals and open exceptions. Alert when:
- `open_exceptions` with severity high has been non-zero for 30 minutes;
- no step has completed in 60 minutes during working hours;
- spend today reaches 80 % of `per_day_usd`.
