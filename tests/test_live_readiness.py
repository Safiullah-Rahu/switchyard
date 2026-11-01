"""Live mode is not exercised in CI (no API key), so pin down what would break it."""

import pytest
from agents.agent_output import AgentOutputSchema

from switchyard.specialists import schemas
from switchyard.specialists.runtime import MCP_ACCESS
from switchyard.specialists.tools import AGENT_KEYS, get_estimate, propose_action


@pytest.mark.parametrize(
    "model",
    [schemas.QualifiedLead, schemas.ServiceCase, schemas.ProposalDraft, schemas.ReviewVerdict, schemas.SurveySlot],
)
def test_output_types_are_valid_strict_structured_outputs(model):
    assert AgentOutputSchema(model, strict_json_schema=True).json_schema()["additionalProperties"] is False


def test_function_tools_use_strict_schemas():
    assert propose_action.strict_json_schema and get_estimate.strict_json_schema


def test_every_agent_has_a_policy_entry_and_known_mcp_scope(settings):
    agents_in_policy = set(settings.policy["agents"])
    assert set(AGENT_KEYS.values()) <= agents_in_policy
    assert set(MCP_ACCESS) <= set(AGENT_KEYS)


def test_every_routed_model_has_a_price(settings):
    priced = set(settings.pricing["models"])
    assert set(settings.pricing["routing"].values()) <= priced
