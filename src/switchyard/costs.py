"""Token usage → money, with per-run and per-day caps checked before each model call."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .audit import AuditLog
from .db import tx


class BudgetExceeded(Exception):
    def __init__(self, scope: str, spent: float, limit: float):
        super().__init__(f"{scope} budget reached: ${spent:.4f} spent of ${limit:.4f}")
        self.scope = scope
        self.spent = spent
        self.limit = limit


@dataclass
class UsageRecord:
    requests: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int


class CostMeter:
    def __init__(
        self, conn: sqlite3.Connection, pricing: dict[str, Any], audit: AuditLog, clock: Callable[[], float] = time.time
    ):
        self.conn = conn
        self.models: dict[str, dict[str, float]] = pricing.get("models", {})
        self.budgets = pricing.get("budgets", {})
        self.routing = pricing.get("routing", {})
        self.audit = audit
        self.clock = clock

    def model_for(self, agent_key: str) -> str:
        return self.routing.get(agent_key, self.routing.get("default", "gpt-5.4-mini"))

    def price(self, model: str, usage: UsageRecord) -> float:
        rates = self.models.get(model)
        if rates is None:
            raise KeyError(f"no price configured for model {model!r} — add it to pricing.toml")
        uncached = max(0, usage.input_tokens - usage.cached_input_tokens)
        return (
            uncached * rates["input"]
            + usage.cached_input_tokens * rates.get("cached_input", rates["input"])
            + usage.output_tokens * rates["output"]
        ) / 1_000_000

    def check_budget(self, run_id: str | None) -> None:
        per_run = float(self.budgets.get("per_run_usd", 0) or 0)
        per_day = float(self.budgets.get("per_day_usd", 0) or 0)
        if run_id and per_run:
            spent = self.run_total(run_id)
            if spent >= per_run:
                raise BudgetExceeded("per-run", spent, per_run)
        if per_day:
            spent = self.day_total()
            if spent >= per_day:
                raise BudgetExceeded("daily", spent, per_day)

    def record(
        self, *, run_id: str | None, step: str | None, agent: str, model: str, usage: UsageRecord, estimated: bool
    ) -> float:
        cost = self.price(model, usage)
        with tx(self.conn):
            self.conn.execute(
                "INSERT INTO usage (run_id, step, agent, model, requests, input_tokens, cached_input_tokens, output_tokens,"
                " cost_usd, estimated, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    step,
                    agent,
                    model,
                    usage.requests,
                    usage.input_tokens,
                    usage.cached_input_tokens,
                    usage.output_tokens,
                    cost,
                    int(estimated),
                    self.clock(),
                ),
            )
            self.audit.record(
                "cost.recorded",
                actor=agent,
                run_id=run_id,
                subject=step,
                data={
                    "model": model,
                    "requests": usage.requests,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cost_usd": round(cost, 6),
                    "estimated": estimated,
                },
            )
        return cost

    def run_total(self, run_id: str) -> float:
        return float(
            self.conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM usage WHERE run_id = ?", (run_id,)).fetchone()[0]
        )

    def day_total(self) -> float:
        start = datetime.fromtimestamp(self.clock(), UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return float(
            self.conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM usage WHERE created_at >= ?", (start,)
            ).fetchone()[0]
        )

    def breakdown(self, run_id: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT agent, model, SUM(requests) AS requests, SUM(input_tokens) AS input_tokens,"
            " SUM(output_tokens) AS output_tokens, SUM(cost_usd) AS cost_usd, MAX(estimated) AS estimated FROM usage"
        )
        args: list[Any] = []
        if run_id:
            sql += " WHERE run_id = ?"
            args.append(run_id)
        sql += " GROUP BY agent, model ORDER BY cost_usd DESC"
        return [dict(r) for r in self.conn.execute(sql, args)]
