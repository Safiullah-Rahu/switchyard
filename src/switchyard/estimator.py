"""Deterministic solar + battery estimate.

Models never do arithmetic in this system. They call this function (through
a tool) and quote its numbers; the review step then checks every number in
the draft against this output.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel

Segment = Literal["residential", "commercial", "agricultural"]


class EstimateInputs(BaseModel):
    segment: Segment
    region: str
    annual_consumption_kwh: float | None
    roof_area_m2: float | None
    include_battery: bool


class Estimate(BaseModel):
    assumptions_version: str
    region: str
    region_label: str
    currency: str
    symbol: str
    segment: Segment
    system_kwp: float
    panel_count: int
    annual_generation_kwh: int
    self_consumption_pct: int
    battery_kwh: int
    annual_savings: int
    capex: int
    payback_years: float
    lifetime_years: int
    lifetime_savings: int
    co2_tonnes_per_year: float
    limited_by: str
    import_tariff: float
    export_tariff: float
    specific_yield: int
    degradation_pct: float
    assumptions: list[str]
    inputs: EstimateInputs

    def figures(self) -> dict[str, tuple[float, str]]:
        """Every number a proposal may quote, with its unit."""
        out: dict[str, tuple[float, str]] = {
            "system_kwp": (self.system_kwp, "kWp"),
            "panel_count": (self.panel_count, "panels"),
            "annual_generation_kwh": (self.annual_generation_kwh, "kWh"),
            "self_consumption_pct": (self.self_consumption_pct, "%"),
            "annual_savings": (self.annual_savings, "currency"),
            "capex": (self.capex, "currency"),
            "payback_years": (self.payback_years, "years"),
            "lifetime_years": (self.lifetime_years, "years"),
            "lifetime_savings": (self.lifetime_savings, "currency"),
            "co2_tonnes_per_year": (self.co2_tonnes_per_year, "tonnes"),
        }
        # Assumption values a proposal may legitimately restate.
        out["import_tariff"] = (self.import_tariff, "currency")
        out["export_tariff"] = (self.export_tariff, "currency")
        out["specific_yield"] = (self.specific_yield, "kWh")
        out["degradation_pct"] = (self.degradation_pct, "%")
        if self.battery_kwh:
            out["battery_kwh"] = (self.battery_kwh, "kWh")
        if self.inputs.annual_consumption_kwh:
            out["annual_consumption_kwh"] = (self.inputs.annual_consumption_kwh, "kWh")
        if self.inputs.roof_area_m2:
            out["roof_area_m2"] = (self.inputs.roof_area_m2, "m2")
        return out


def _round_to(value: float, step: int) -> int:
    return int(round(value / step) * step)


def estimate(inputs: EstimateInputs, config: dict[str, Any]) -> Estimate:
    if inputs.region not in config["regions"]:
        raise ValueError(f"unknown region {inputs.region!r}; known: {sorted(config['regions'])}")
    if not inputs.annual_consumption_kwh and not inputs.roof_area_m2:
        raise ValueError("need annual consumption or roof area to size a system")
    region = config["regions"][inputs.region]
    seg = config["segments"][inputs.segment]
    panel_kwp = config["panel_watts"] / 1000
    assumptions: list[str] = [
        f"Specific yield {region['yield_kwh_per_kwp']} kWh per kWp per year ({region['label']})",
        f"Import tariff {region['symbol']}{region['import_tariff']:.3f}/kWh, export {region['symbol']}{region['export_tariff']:.3f}/kWh, flat",
        f"{config['panel_watts']} W panels, {config['degradation_per_year'] * 100:.1f}% output loss per year",
    ]

    roof_panels = None
    if inputs.roof_area_m2:
        roof_panels = math.floor(inputs.roof_area_m2 * config["usable_roof_fraction"] / config["panel_area_m2"])
    target_panels = None
    if inputs.annual_consumption_kwh:
        target_kwp = inputs.annual_consumption_kwh / region["yield_kwh_per_kwp"] * seg["target_offset"]
        target_panels = math.ceil(target_kwp / panel_kwp)

    if target_panels is not None and roof_panels is not None:
        panels, limited_by = (roof_panels, "roof") if roof_panels < target_panels else (target_panels, "consumption")
    elif target_panels is not None:
        panels, limited_by = target_panels, "consumption"
        assumptions.append("Roof area not given: sized on consumption only; the survey must confirm space")
    else:
        panels, limited_by = roof_panels, "roof_only_estimate"  # type: ignore[assignment]
        assumptions.append("Annual consumption not given: sized on roof area; savings assume typical self-use")
    if not panels or panels <= 0:
        raise ValueError("roof too small for a single panel")

    kwp = round(panels * panel_kwp, 2)
    generation = kwp * region["yield_kwh_per_kwp"]

    battery_kwh = 0
    if inputs.include_battery:
        if inputs.segment == "residential" or not inputs.annual_consumption_kwh:
            battery_kwh = int(seg.get("battery_kwh_default", 10))
        else:
            battery_kwh = max(5, _round_to(inputs.annual_consumption_kwh / 365 * seg["battery_daily_fraction"], 5))
    scr = min(0.9, seg["self_consumption"] + (seg["battery_uplift"] if battery_kwh else 0))
    self_used = generation * scr
    if inputs.annual_consumption_kwh:
        self_used = min(self_used, inputs.annual_consumption_kwh)
    exported = generation - self_used

    savings = _round_to(self_used * region["import_tariff"] + exported * region["export_tariff"], 10)
    capex = _round_to(kwp * region["capex_per_kwp"] + battery_kwh * region["battery_cost_per_kwh"], 100)
    lifetime = config["lifetime_years"]
    lifetime_savings = _round_to(sum(savings * (1 - config["degradation_per_year"]) ** y for y in range(lifetime)), 100)

    return Estimate(
        assumptions_version=config["assumptions_version"],
        region=inputs.region,
        region_label=region["label"],
        currency=region["currency"],
        symbol=region["symbol"],
        segment=inputs.segment,
        system_kwp=kwp,
        panel_count=panels,
        annual_generation_kwh=_round_to(generation, 10),
        self_consumption_pct=round(self_used / generation * 100),
        battery_kwh=battery_kwh,
        annual_savings=savings,
        capex=capex,
        payback_years=round(capex / savings, 1),
        lifetime_years=lifetime,
        lifetime_savings=lifetime_savings,
        co2_tonnes_per_year=round(generation * region["grid_kg_co2_per_kwh"] / 1000, 1),
        limited_by=limited_by,
        import_tariff=region["import_tariff"],
        export_tariff=region["export_tariff"],
        specific_yield=region["yield_kwh_per_kwp"],
        degradation_pct=round(config["degradation_per_year"] * 100, 2),
        assumptions=assumptions,
        inputs=inputs,
    )


# ---- formatting shared by writers and checks -------------------------------------


def money(symbol: str, value: float) -> str:
    return f"{symbol}{value:,.0f}"


def kwh(value: float) -> str:
    return f"{value:,.0f} kWh"
