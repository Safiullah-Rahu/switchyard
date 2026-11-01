# Architecture

## Design rules

1. **Models decide; code acts.** Agents read and judge. Every write to another system goes through one
   code path, the outbox, which applies policy, approval, idempotency and verification.
2. **State lives in the database, not in a process.** Any worker can resume any run after a restart.
3. **Keys come from identity, not from content.** A side effect's idempotency key is `(run, step, slot)`.
   It is never derived from model output, which can change from one attempt to the next.
4. **"Done" means read back.** An intent is complete when the destination has been read and matches,
   not when the API returned 200.
5. **Nothing is guessed.** Missing data, conflicting records and repeated review failures become
   exceptions or decisions for a person.

## Components

| Component | File | Responsibility |
|---|---|---|
| API | `api.py` | Signed webhooks in; JSON API under `/api` for approvals, decisions, retries and reads |
| Console | `web/` | Server-rendered operations console (Jinja2): sign-in, approvals, runs, exceptions, audit, costs |
| Engine | `engine.py` | Runs, steps, the dependency graph, leases, fencing, retries, waits, rewinds, run status |
| Outbox | `outbox.py` | Intent state machine; the only caller of adapters' write methods |
| Policy | `policy.py` + `policy.toml` | Action registry, risk classes, dynamic rules, leak scan, agent allowlists, principals |
| Approvals | `approvals.py` | Payload-bound, HMAC-signed approval requests and decisions |
| Specialists | `specialists/` | Agents SDK agents, tools, prompts, the offline model |
| MCP server | `mcp_server.py` | Read-only tools over the business apps |
| Adapters | `adapters/` | `execute` / `lookup` / `verify` for each destination |
| Audit, costs, exceptions | `audit.py`, `costs.py`, `reporting.py` | Evidence, money, and the queue for people |

## Why this backend and not more

A pilot needs durable state, a scheduler and an HTTP surface. One Python process (FastAPI plus an asyncio
worker loop) and one SQLite file in WAL mode provide all three, and leave nothing to operate apart from a
VM or container with a disk.

**Add Temporal (or similar) when:** there are dozens of workflows with long timers, cross-service
compensation (sagas), or several teams building workflows. The engine's concepts map across directly:
steps become activities, intents become activities with idempotency keys, waits become signals. The adapters,
policy, approvals and agents carry over unchanged.

**Move to Postgres when:** more than one worker host is needed, or the client's IT standard requires it.
The schema in `db.py` is plain SQL. Two changes are needed:

- Claim with `SELECT … FOR UPDATE SKIP LOCKED` instead of `BEGIN IMMEDIATE`.
- Run side-intent dispatch under a Postgres advisory lock, so only one worker does it.

## Data model

| Table | One row per | Key columns |
|---|---|---|
| `runs` | workflow run | `dedup_key` (unique business key), `status`, `parent_run_id` |
| `steps` | step of a run | `status`, `depends_on`, `attempts`, `lease_owner`, `lease_token`, `lease_expires_at`, `waiting_on`, `output_json`, `notes_json`, `revisions` |
| `intents` | logical side effect | `idem_key` (unique), `action`, `payload_hash`, `risk_class`, `status`, `receipt_json`, `verification_json` |
| `approvals` | request for a person's decision | `intent_id`, `payload_hash`, `required_roles`, `status`, `decided_by`, `signature`, `expires_at` |
| `decisions` | open question to a person | `(run_id, step, key)` unique, `options_json`, `choice` |
| `exceptions` | thing a person should know | `kind`, `severity`, `suggested_action`, `dedup_key`, `status` |
| `usage` | model call batch | `agent`, `model`, tokens, `cost_usd`, `estimated` |
| `inbound_events` | webhook delivery | `(source, event_id)` primary key |
| `audit` | state change | `seq`, `prev_hash`, `hash` |

## State machines

**Step**

```
pending ──claim (lease + token)──► running ──► succeeded
   ▲                                  ├─► waiting      (approval:<id> | decision:<id>) ──person──► pending
   │                                  ├─► retry_wait   (next_attempt_at) ──time──► claimable
   │                                  ├─► failed       → transitive dependents → blocked
   └──── lease expired / rewind ◄─────┘
```

**Intent**

```
proposed → (policy) → awaiting_approval → approved → executing → executed → verified
                            │ rejected                  │ timeout: stays executing → lookup next time
                            ▼                           ├─ transient: retry (backoff)
                         rejected                       └─ permanent / verification mismatch: failed
```

## A step's life, end to end

1. `tick()` recovers expired leases, then claims the next step that is ready (all dependencies
   succeeded). The claim increments `lease_token`.
2. The handler runs. An agent step calls `ctx.run_agent()`: the budget is checked, the Agents SDK runner is
   called, and usage and cost are recorded. Proposals the agent makes are held in memory.
3. A side effect calls `ctx.act(action, payload, slot)`. The outbox finds or creates the intent, classifies
   it, enforces approval, and marks it `executing` (after a fencing check). It then calls the adapter with
   the idempotency key, records the receipt, reads the destination back, and marks it `verified`.
4. On `Done`, one transaction checks the fence, stores the output, and commits the agent's proposals as
   intents. On a wait, retry or failure, the step is parked accordingly, and an exception is raised where a
   person should know.
5. `_refresh_runs()` derives the run's status from its steps.

## Agents

| Agent | Tools | MCP tools (filtered per agent) | Output |
|---|---|---|---|
| Triage | handoffs only | none | hands off |
| Lead Qualifier | `propose_action` | `crm_search_contacts`, `crm_list_deals` | `QualifiedLead` |
| Service Desk | `propose_action` | `crm_search_contacts`, `crm_list_deals` | `ServiceCase` |
| Proposal Writer | `get_estimate`, `propose_action` | `docs_get_template` | `ProposalDraft` |
| Survey Scheduler | none | `calendar_free_slots` | `SurveySlot` |
| Independent Reviewer | `get_estimate` | none | `ReviewVerdict` |

All outputs are strict structured outputs (Pydantic models; `tests/test_live_readiness.py` checks they are
accepted in strict mode). Routing defaults to a small model for routine agents and a larger one for the
reviewer. Both are set in `pricing.toml`.

**Offline mode.** `PlaybookModel` implements the SDK's `Model` interface, with a rule-based playbook per
agent. The SDK runner, handoffs, MCP over stdio, tool calls and output parsing all run for real, so CI covers
the integration, not a mock of it.

## Tracing and observability

- Live mode: Agents SDK traces are grouped by `group_id = run_id`, with `workflow_name =
  switchyard.<workflow>.<step>`, and `trace_include_sensitive_data=False`.
- Each audit entry records actor, event, subject and data, and the chain can be verified (`switchyard audit-verify`).
- The console shows runs step by step (with the agent, model and cost of each), pending approvals, open
  exceptions, spend against the caps, and the audit log in plain language.
- `switchyard digest` gives a plain-text summary for a daily post.
