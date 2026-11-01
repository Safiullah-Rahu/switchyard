# Handover checklist

The client owns everything below from the first commit: the repository lives in the client's GitHub or
GitLab organisation, and the delivery team works there as collaborators.

## Code and configuration
- [ ] Repository in the client's organisation, with history; the main branch is protected.
- [ ] `policy.toml`, `pricing.toml` and the estimator/pricing assumptions reviewed and signed off by named
      owners (operations, finance).
- [ ] Prompts, the evaluation set and the regression results committed alongside the code.
- [ ] CI green: lint, unit, contract and scenario tests.

## Infrastructure and access
- [ ] Infrastructure defined as code (container image + deployment manifest, or VM provisioning script).
- [ ] Secrets in the client's vault. Delivery-team access is removed at the end of the support window.
- [ ] Integration service accounts are owned by client admins, with scopes listed per integration.
- [ ] OpenAI organisation and project owned by the client, with usage limits set.

## Operations
- [ ] Runbook (OPERATIONS.md) walked through with the operations owner, including a live fault drill.
- [ ] Alerts routed to the client's on-call or ops channel.
- [ ] Backups scheduled and one restore tested.
- [ ] Audit-chain head exported daily.

## Knowledge transfer
- [ ] Architecture walkthrough (recorded): engine, outbox, policy, agents.
- [ ] "Add a workflow" exercise, done by a client developer with the delivery team pairing.
- [ ] A list of open issues and planned next steps, each with a short rationale.

## Support window
- [ ] Agreed post-launch period (for example 30 days) with response times for incidents and for changes.
- [ ] Afterwards: optional retainer, or a clean exit with everything above in place.
