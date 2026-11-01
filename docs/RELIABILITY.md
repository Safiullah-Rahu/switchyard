# Reliability: guarantees, how they are kept, and their limits

"Exactly once" is not something a client can be promised in general: networks lose replies. What this
system promises is **effectively once**. Each logical action has one identity, is never repeated while
there is any chance the earlier attempt landed, and is confirmed by reading the destination back. That
promise depends on each destination offering some way to *find* a write by our key. Where an API offers
none, the gap is named below rather than hidden.

## Guarantees

| # | Guarantee | Mechanism | Test |
|---|---|---|---|
| G1 | A business event starts at most one run | `runs.dedup_key` unique; webhook `(source, event_id)` unique; content hash for inbound messages; business key for leads (email + site) | *duplicates*, `test_engine::test_business_dedup…` |
| G2 | A step is run by at most one worker at a time | Lease with expiry; claims are conditional updates | `test_engine::test_crashed_worker…` |
| G3 | A worker that lost its lease cannot change anything | Fencing token checked before an intent goes to `executing`, and when a step commits | `test_engine::test_zombie_worker…` |
| G4 | A side effect is attempted again only when it is known not to have landed | `executing` is written before the call. Afterwards, `lookup(idem_key)` runs before any new call | *crash*, *ambiguous*, `test_approvals_outbox::test_crash…` |
| G5 | A side effect is complete only when the destination holds it | `verify()` reads back by id and compares fields, or a body hash | *verification* |
| G6 | Agents' proposals happen only if their step completes | Proposals are committed in the same transaction as the step's output | `engine._complete` |
| G7 | Gated actions run only with a valid, signed approval of this exact content | Re-classification at execution time, payload-hash binding, HMAC signature, role re-check | *approval_integrity*, `test_approvals_outbox` |
| G8 | History cannot be edited without detection | Hash-chained audit, verified on demand | `test_checks_audit::test_audit_chain…` |

## Failure catalogue

| Failure | Classified as | Behaviour | Person involved? |
|---|---|---|---|
| 429 / 5xx / connection refused | Transient | Exponential backoff with jitter (`retry_base_seconds` × 2ⁿ, capped). The step waits in `retry_wait`. | Only if attempts run out (`tool_retries_exhausted`) |
| Timeout or connection reset after the request was sent | Ambiguous | The intent stays `executing`. The next attempt runs `lookup` first: if found, the receipt is adopted; if not, the call is made again. | No |
| 400 / 401 / 403 / 404 | Permanent | Intent and step fail; dependents are blocked; independent branches continue | Yes — fix the cause, then `switchyard retry` |
| 409 on a unique key | Conflict | The existing object is adopted as the receipt (an earlier attempt created it) | No |
| 200 but nothing stored, or different content stored | Verification failure | Intent and step fail; nothing downstream runs | Yes — inspect the destination, then retry (which reconciles first) |
| Worker process dies | Lease expiry | Another worker resumes after `lease_seconds`; G4 prevents a duplicate | Info-level exception only |
| Model error, invalid output shape, max turns | Step error | Retried up to `max_attempts`, then an `agent_error` exception | If it keeps failing |
| Model proposes an action outside its allowlist | Policy violation | Refused at the tool; an exception is raised for review | Yes (likely injection) |
| Draft fails numeric or claim checks | Review | Rewind with findings, at most `max_review_revisions` times, then a `review_rejected` exception | After the bound |
| Budget cap reached | Budget | The step fails before the model is called; `budget_exceeded` exception | Yes |
| Approval not given in time | Expiry | Request expires; a new request is raised on the next attempt | Yes |
| Approval rejected | Rejection | Intent `rejected`; the step fails as `approval_rejected` (low severity) | Already involved |

## Known limits, stated plainly

- **Destinations with no way to find a write by key.** Plain SMTP is one example. Here the Message-ID
  header is our key and we search the Sent folder. If a provider ignored client Message-IDs, the fallback is
  a custom header plus search; this is confirmed per account during integration (see INTEGRATIONS.md).
- **Search indexes that lag** (HubSpot search, some mail search). These are only used to *find* an object
  after a conflict or a crash, never to prove a write. Verification reads by id.
- **Check-then-act adapters** (notes, docs, chat) look the key up before creating. Without a native unique
  constraint, two workers racing on the *same* intent could both create. G2 and G3 prevent two workers from
  holding the same step, so this race needs a lease expiry *during* the call. Keep `lease_seconds` well above
  the slowest call (the default is 60 s against 10 s HTTP timeouts).
- **Side-intent dispatch** (agent proposals, notices) assumes one dispatcher. On Postgres this becomes an
  advisory lock.
- **Clock skew** between workers affects lease expiry only. Use NTP and leave margin.
