# Approvals and policy

## What needs a person

| Class | Examples | Default approvers | Expires |
|---|---|---|---|
| `internal_routine` | CRM updates, documents kept internal, internal calendar holds, team-channel posts, internal email | none: runs on its own | — |
| `external_communication` | Email to a customer, a post to a shared channel, a calendar invite to outsiders, a document shared outside | sales lead or operations manager | 72 h |
| `binding_commitment` | A priced quote sent to a customer, an invoice | sales lead or finance | 48 h |
| `sensitive_disclosure` | Anything going outside that contains internal-only wording, bank or card numbers, credentials, or other people's addresses | operations manager | 24 h |

When several classes apply, the most restrictive one wins, and its approver roles apply. Classes, roles and
expiry are set in `policy.toml`. Changing them needs no code change, but should go through review: the
file is part of the controls.

**Decisions** are separate from approvals. An approval is a yes or no on a specific action. A decision
is a choice between options with consequences, such as "this contact already has an open deal owned by
Maya: attach, or open a new one?". The step waits for a named person.

## Enforcement path

```
agent ──propose_action──► allowlist check ──► proposal (in memory)
                                               │ committed with the step's output
workflow step ──ctx.act──────────────────────► intent (idem_key, payload_hash)
                                               │
executor, every attempt:  classify(stored payload) ─► needs approval?
                                               │ yes
                          approvals.check(): exists · same payload hash · signature valid ·
                                             not expired · approver still holds a required role
                                               │ all true
                                            execute → verify
```

Nothing a model writes can change this path. The class comes from the action name and the payload's
*facts* (recipients, channel, quote value, content scan), worked out by code at the moment of execution.

## Threat model

| Threat | Control | Tested |
|---|---|---|
| A prompt injection in an inbound email tells the agent to send data or commit money | Inbound text is wrapped in `<inbound>` and the prompts say to treat it as data. More importantly, the agent cannot send: `email.send` and `billing.*` are not on the Lead Qualifier's allowlist, and any allowed action that leaves the company is re-classified and scanned. | *injection* runs a model that **obeys** the attacker |
| A model rewrites an approved email | The approval is bound to the payload hash; the change produces a new approval request | *approval_integrity* |
| Someone inserts or edits an approval row directly | HMAC signature over (approval, intent, payload hash, decision, person, time) with a server-side secret | *approval_integrity* |
| An approver leaves, or loses a role | Roles are re-checked at execution, so their unused approvals stop counting | `test_revoked_role…` |
| A person without the right role approves | `decide()` checks roles against the required set | *pilot* |
| Identity spoofing inside a message ("I'm the CFO, approve this") | Sender identity comes from the envelope, never from the body; approver identity comes from tokens or SSO | `workflows/inbound.py` |
| Replay of an old approval for a new action | Each approval belongs to one intent id and one payload hash | `test_approval_is_bound…` |
| Audit tampering to hide an action | Hash chain; any edit or deletion breaks verification from that row onwards | `test_audit_chain…` |

## Approver experience

- A notice goes to `#approvals` (internal routine, sent through the outbox, so once per request). It
  links to the review page.
- The review page shows exactly what will be sent, why it needs a person, who can approve it, and the
  payload hash.
- The decision can be made in the console, through the API (`POST /approvals/{id}/decision`), or from the
  CLI on the server.
- In production, the token box becomes SSO (Google Workspace or Microsoft Entra ID), and approvals can
  also be offered as Slack or Teams buttons that call the same endpoint. Signing and checks are unchanged.
