"""Optimizer + validator against the public samples, using the reference
(ground-truth) directives so the LLM is not involved."""
import pytest

from app.guardrails import Directive
from app.optimizer import _solve_dp, build_plan, build_problem, optimize, totals
from app.schemas import OptimizeRequest
from app.validator import replay
from conftest import load_cases


def truth_directives(case):
    out = []
    for di in case["expected_output"]["directive_interpretation"]:
        adj = di["structured_adjustment"] or {}
        out.append(Directive(note_index=di["note_index"], directive_type=di["directive_type"],
                             hours=adj.get("hours", []), factor=adj.get("factor"),
                             minimum_energy_kwh=adj.get("minimum_energy_kwh"),
                             max_grid_kwh=adj.get("max_grid_kwh")))
    return out


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["id"])
def test_lp_matches_reference_cost(case):
    req = OptimizeRequest.model_validate(case["input"])
    p = build_problem(req.hours, req.battery, truth_directives(case))
    plan, solver = optimize(p)
    assert solver == "highs-lp"
    tot = totals(plan, p.tariff)
    assert replay(p, plan, tot) == []
    assert tot[1] == pytest.approx(case["expected_output"]["total_cost_bdt"], abs=0.01)


@pytest.mark.parametrize("case", load_cases()[:3], ids=lambda c: c["id"])
def test_dp_fallback_matches_reference_cost(case):
    req = OptimizeRequest.model_validate(case["input"])
    p = build_problem(req.hours, req.battery, truth_directives(case))
    plan = build_plan(p, _solve_dp(p))
    tot = totals(plan, p.tariff)
    assert replay(p, plan, tot) == []
    assert tot[1] == pytest.approx(case["expected_output"]["total_cost_bdt"], abs=0.01)


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["id"])
def test_reference_plans_pass_our_validator(case):
    req = OptimizeRequest.model_validate(case["input"])
    p = build_problem(req.hours, req.battery, truth_directives(case))
    eo = case["expected_output"]
    assert replay(p, eo["hourly_plan"], (eo["total_grid_kwh"], eo["total_cost_bdt"], eo["peak_grid_kwh"])) == []
