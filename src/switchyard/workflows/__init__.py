"""Workflow definitions. The pilot is lead_to_proposal; the others show the phased rollout."""

from __future__ import annotations

from ..engine import WorkflowDef


def default_workflows() -> list[WorkflowDef]:
    from . import inbound, lead_to_proposal, service_request, survey_to_install

    return [inbound.WORKFLOW, lead_to_proposal.WORKFLOW, service_request.WORKFLOW, survey_to_install.WORKFLOW]
