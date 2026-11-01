"""Builds the specialist agents and runs them for a workflow step.

Each run: check the budget → run the agent with the OpenAI Agents SDK →
record token usage and cost → write an audit entry with the tools it used.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any

from agents import Agent, ModelSettings, RunConfig, Runner
from agents.mcp import MCPServerStdio, ToolFilterContext

from ..costs import UsageRecord
from .offline import PlaybookModel
from .prompts import INSTRUCTIONS
from .schemas import ProposalDraft, QualifiedLead, ReviewVerdict, ServiceCase, SurveySlot
from .tools import AgentContext, get_estimate, propose_action

# Least privilege on the shared MCP connection: which read tools each agent may see.
MCP_ACCESS: dict[str, set[str]] = {
    "Lead Qualifier": {"crm_search_contacts", "crm_list_deals"},
    "Service Desk": {"crm_search_contacts", "crm_list_deals"},
    "Proposal Writer": {"docs_get_template"},
    "Survey Scheduler": {"calendar_free_slots"},
}


def mcp_tool_filter(context: ToolFilterContext, tool: Any) -> bool:
    return tool.name in MCP_ACCESS.get(context.agent.name, set())


@dataclass
class AgentOutcome:
    output: Any
    agent: str
    model: str
    cost_usd: float
    tool_calls: list[str] = field(default_factory=list)
    refused: list[dict[str, Any]] = field(default_factory=list)


class AgentRuntime:
    def __init__(self, engine: Any):
        self.engine = engine
        self.offline = engine.settings.mode == "offline"
        self._mcp: MCPServerStdio | None = None

    async def mcp(self) -> MCPServerStdio:
        if self._mcp is None:
            env = {**os.environ, "SWITCHYARD_SANDBOX_DB": str(self.engine.settings.sandbox_db_path)}
            env.pop("SWITCHYARD_HARD_CRASH", None)  # the MCP process only reads
            server = MCPServerStdio(
                params={"command": sys.executable, "args": ["-m", "switchyard.mcp_server"], "env": env},
                name="business-apps",
                cache_tools_list=True,
                tool_filter=mcp_tool_filter,  # type: ignore[arg-type]
                client_session_timeout_seconds=30,
                max_retry_attempts=2,
            )
            await server.connect()
            self._mcp = server
        return self._mcp

    async def aclose(self) -> None:
        if self._mcp is not None:
            await self._mcp.cleanup()
            self._mcp = None

    def _model(self, key: str, flags: set[str]) -> Any:
        return PlaybookModel(key, flags) if self.offline else self.engine.costs.model_for(key)

    def build(self, flags: set[str], mcp: MCPServerStdio) -> dict[str, Agent[AgentContext]]:
        settings = ModelSettings(parallel_tool_calls=True)
        m = lambda key: self._model(key, flags)  # noqa: E731
        qualifier = Agent[AgentContext](
            name="Lead Qualifier",
            instructions=INSTRUCTIONS["lead_qualifier"],
            model=m("lead_qualifier"),
            tools=[propose_action],
            mcp_servers=[mcp],
            output_type=QualifiedLead,
            model_settings=settings,
            handoff_description="New installation enquiries (solar, battery, EV charging, heat pumps).",
        )
        service = Agent[AgentContext](
            name="Service Desk",
            instructions=INSTRUCTIONS["service_desk"],
            model=m("service_desk"),
            tools=[propose_action],
            mcp_servers=[mcp],
            output_type=ServiceCase,
            model_settings=settings,
            handoff_description="Existing customers: faults, billing, maintenance.",
        )
        triage = Agent[AgentContext](
            name="Triage",
            instructions=INSTRUCTIONS["triage"],
            model=m("triage"),
            handoffs=[qualifier, service],
        )
        writer = Agent[AgentContext](
            name="Proposal Writer",
            instructions=INSTRUCTIONS["proposal_writer"],
            model=m("proposal_writer"),
            tools=[get_estimate, propose_action],
            mcp_servers=[mcp],
            output_type=ProposalDraft,
            model_settings=settings,
        )
        scheduler = Agent[AgentContext](
            name="Survey Scheduler",
            instructions=INSTRUCTIONS["survey_scheduler"],
            model=m("survey_scheduler"),
            mcp_servers=[mcp],
            output_type=SurveySlot,
        )
        reviewer = Agent[AgentContext](
            name="Independent Reviewer",
            instructions=INSTRUCTIONS["reviewer"],
            model=m("reviewer"),
            tools=[get_estimate],
            output_type=ReviewVerdict,
        )
        return {
            "triage": triage,
            "lead_qualifier": qualifier,
            "service_desk": service,
            "proposal_writer": writer,
            "survey_scheduler": scheduler,
            "reviewer": reviewer,
        }

    async def run(
        self, ctx: Any, agent_key: str, prompt: str, *, estimate: dict[str, Any] | None = None
    ) -> AgentOutcome:
        engine = self.engine
        engine.costs.check_budget(ctx.run_id)
        flags = set(engine.settings.demo_flags) | set(ctx.input.get("demo_flags", []))
        agents = self.build(flags, await self.mcp())
        actx = AgentContext(
            engine=engine, run_id=ctx.run_id, step=ctx.step, proposals=ctx.proposals, estimate=estimate, flags=flags
        )
        result = await Runner.run(
            agents[agent_key],
            prompt,
            context=actx,
            max_turns=8,
            run_config=RunConfig(
                workflow_name=f"switchyard.{ctx.workflow}.{ctx.step}",
                group_id=ctx.run_id,
                trace_metadata={"run_id": ctx.run_id, "step": ctx.step, "attempt": str(ctx.attempt)},
                tracing_disabled=self.offline,
                trace_include_sensitive_data=False,
            ),
        )
        last = result.last_agent.name
        last_key = next(k for k, a in agents.items() if a.name == last)
        model = engine.costs.model_for(last_key)
        u = result.context_wrapper.usage
        cached = getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0
        cost = engine.costs.record(
            run_id=ctx.run_id,
            step=ctx.step,
            agent=last,
            model=model,
            usage=UsageRecord(u.requests, u.input_tokens, cached, u.output_tokens),
            estimated=self.offline,
        )
        calls = [getattr(i.raw_item, "name", "?") for i in result.new_items if i.type == "tool_call_item"]
        handoffs = [last] if last != agents[agent_key].name else []
        engine.audit.record(
            "agent.completed",
            actor=f"agent:{last_key}",
            run_id=ctx.run_id,
            subject=ctx.step,
            data={
                "started_with": agents[agent_key].name,
                "finished_with": last,
                "handoffs": handoffs,
                "tool_calls": calls,
                "proposals": len(actx.proposals),
                "refused_proposals": actx.refused,
                "model": "offline-playbook" if self.offline else model,
                "cost_usd": round(cost, 6),
            },
        )
        if actx.refused:  # an agent reaching beyond its permissions usually means injected instructions
            engine.reporter.raise_exception(
                kind="policy_violation",
                severity="high",
                message=f"{last} proposed {', '.join(r['action'] for r in actx.refused)}, which it is not permitted to do; refused",
                run_id=ctx.run_id,
                step=ctx.step,
                details={"refused": actx.refused},
            )
        return AgentOutcome(
            output=result.final_output, agent=last, model=model, cost_usd=cost, tool_calls=calls, refused=actx.refused
        )
