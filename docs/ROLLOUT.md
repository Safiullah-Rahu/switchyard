# Phased rollout and acceptance criteria

Each phase ends with acceptance criteria agreed in writing before work starts. A phase is accepted when the
criteria pass on the client's systems, not on simulations. The criteria below are a starting point, based on
this reference build. The real ones are written with the client from their documented processes.

## Phase 1: functional pilot (one workflow, end to end)

**Scope.** One complete workflow on the client's real CRM, email, documents, calendar and messaging, using
the client's existing SOP as the specification. In this repository that workflow is `lead_to_proposal`.

**Acceptance criteria (example)**
- A batch of real historical enquiries agreed with the client (for example 20) is processed in shadow
  mode, writing to a sandbox or dry-run destination. At least 90 % need no correction to qualification
  fields, and 100 % of the proposal figures trace to the estimator.
- Internal steps complete with no human action. The only approvals requested are those in the agreed
  policy table.
- Fault drills on the real integrations pass: a duplicate webhook, a worker killed mid-send, a revoked token
  and a dropped write each produce zero duplicates and a clear exception.
- Median model cost per run is reported, and stays under the agreed cap.

## Phase 2: reliable core

**Scope.** Harden the pilot for daily use and make the engine the base for more workflows.
- Postgres, two workers, leader election for dispatch, backups, alerts.
- SSO for approvers. Approvals in Slack or Teams.
- Evaluation set from real cases, and a regression run on every prompt or model change.
- Dashboards for run outcomes, approval latency, exceptions by kind, and cost per workflow.
- Runbook rehearsal with the operations owner.

**Acceptance criteria (example)**
- Two weeks in production with no duplicate external action and no unexplained exception.
- Every exception has an owner and was handled within the agreed time.
- A restore from backup is tested; the audit chain verifies after the restore.

## Phase 3: broader rollout (sales and project operations)

**Scope.** Further workflows on the same engine, in an order the client sets. Candidates seen in this
reference build:
- service desk (`service_request`);
- survey to installation handover with a deposit invoice (`survey_to_install`);
- quote follow-ups;
- installer scheduling;
- grid-connection paperwork;
- commissioning documents;
- review requests after installation.

**Acceptance criteria.** The same form as Phase 1, for each workflow, plus no regressions in the
evaluation set.

## What does not change between phases

Policy file, approvals, outbox, audit, costs and exceptions are shared. Adding a workflow means writing its
step functions and prompts, and registering any new actions in `policy.toml`. The guarantees in
RELIABILITY.md are not rebuilt for each workflow.
