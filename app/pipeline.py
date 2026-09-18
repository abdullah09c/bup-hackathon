"""End-to-end flow: LLM interpretation -> guardrails -> optimizer -> replay validation -> response."""
from __future__ import annotations

import logging

from starlette.concurrency import run_in_threadpool

from .config import Settings
from .guardrails import Directive, fix_solar_meridiem
from .llm.interpreter import interpret
from .optimizer import InfeasibleError, Problem, build_problem, optimize, totals
from .schemas import OptimizeRequest
from .validator import replay

log = logging.getLogger("gridwise.pipeline")


class ScenarioError(Exception):
    """Well-formed request that cannot be scheduled (-> HTTP 422)."""


def check_semantics(req: OptimizeRequest) -> None:
    b = req.battery
    if b.minimum_energy_kwh > b.capacity_kwh:
        raise ScenarioError("battery.minimum_energy_kwh exceeds capacity_kwh")
    if not b.minimum_energy_kwh <= b.initial_energy_kwh <= b.capacity_kwh:
        raise ScenarioError("battery.initial_energy_kwh must be between minimum_energy_kwh and capacity_kwh")


def _solve(p: Problem):
    plan, solver = optimize(p)
    tot = totals(plan, p.tariff)
    errs = replay(p, plan, tot)
    if errs:
        raise RuntimeError(f"replay validation failed: {errs[:3]}")
    return plan, tot, solver


def _ranges(hours: list[int]) -> str:
    """[1, 10, 11, 12, 17, 18] -> "1, 10-12, 17-18"."""
    out, start = [], None
    for i, h in enumerate(hours):
        if start is None:
            start = h
        if i + 1 == len(hours) or hours[i + 1] != h + 1:
            out.append(f"{start}" if start == h else f"{start}-{h}")
            start = None
    return ", ".join(out)


def summarize(directives: list[Directive], plan: list[dict], cost: float) -> str:
    applied = [d.directive_type for d in directives if d.applies]
    ignored = sum(1 for d in directives if not d.applies)
    chg = [r for r in plan if r["battery_action"] == "charge"]
    dis = [r for r in plan if r["battery_action"] == "discharge"]
    parts = []
    if applied:
        parts.append("Applied " + ", ".join(applied))
    if ignored:
        parts.append(f"ignored {ignored} unrelated note(s)")
    head = "; ".join(parts) + ". " if parts else ""
    if not chg and not dis:
        strategy = "Battery stays idle; demand is met from solar first, then the grid"
    else:
        strategy = (f"Charges {sum(r['battery_kwh'] for r in chg):g} kWh in hours {_ranges([r['hour'] for r in chg])} "
                    f"and discharges {sum(r['battery_kwh'] for r in dis):g} kWh in hours {_ranges([r['hour'] for r in dis])}, "
                    "shifting energy to higher-tariff hours and ending at the initial battery level")
    return f"{head}{strategy}; total grid cost {cost:g} BDT."


async def run(req: OptimizeRequest, settings: Settings) -> dict:
    check_semantics(req)
    directives, source = await interpret(req.operator_notes, req.battery.capacity_kwh, settings)
    directives = fix_solar_meridiem(directives, [h.solar_kwh for h in req.hours])
    log.info("scenario=%s interpretation_source=%s types=%s", req.scenario_id, source,
             [d.directive_type for d in directives])

    p = build_problem(req.hours, req.battery, directives)
    try:
        plan, (total_grid, cost, peak), solver = await run_in_threadpool(_solve, p)
    except InfeasibleError:
        raise ScenarioError("no feasible schedule satisfies the battery rules and interpreted directives") from None

    return {
        "scenario_id": req.scenario_id,
        "directive_interpretation": [d.to_interpretation() for d in directives],
        "hourly_plan": plan,
        "total_grid_kwh": total_grid,
        "total_cost_bdt": cost,
        "peak_grid_kwh": peak,
        "plan_summary": summarize(directives, plan, cost),
    }
