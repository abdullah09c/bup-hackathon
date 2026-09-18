"""24-hour schedule optimizer.

Primary: linear program solved by HiGHS (scipy.optimize.linprog).
Fallback: exact dynamic programme over battery energy levels (pure Python).

Model (per hour h, Problem Statement §05.3 and §09):
    grid + solar_used + discharge = demand + charge
    E[h] = E[h-1] + charge - discharge,  E[-1] = initial
    max(base_min, reserve[h]) <= E[h] <= capacity
    0 <= charge <= max_charge   (0 inside no_charge windows)
    0 <= discharge <= max_discharge   (0 inside no_discharge windows)
    0 <= solar_used <= effective_solar[h]
    0 <= grid <= grid_cap[h]
    E[23] = initial
minimize sum(grid * tariff)
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from .guardrails import Directive

log = logging.getLogger("gridwise.optimizer")

DECIMALS = 4
EPS_THROUGHPUT = 1e-6  # tiny penalty on charge+discharge: avoids pointless cycling, never changes the optimum materially


class InfeasibleError(Exception):
    pass


@dataclass
class Problem:
    demand: list[float]
    solar: list[float]          # effective solar after solar_reduction
    tariff: list[float]
    e_min: list[float]          # max(base minimum, active reserve)
    e_max: float
    initial: float
    charge_max: list[float]
    discharge_max: list[float]
    grid_cap: list[float]       # math.inf when uncapped


def build_problem(hours, battery, directives: list[Directive]) -> Problem:
    demand = [h.demand_kwh for h in hours]
    solar = [h.solar_kwh for h in hours]
    tariff = [h.tariff_bdt_per_kwh for h in hours]
    e_min = [battery.minimum_energy_kwh] * 24
    charge_max = [battery.max_charge_kwh_per_hour] * 24
    discharge_max = [battery.max_discharge_kwh_per_hour] * 24
    grid_cap = [math.inf] * 24
    for d in directives:
        for h in d.hours:
            if d.directive_type == "solar_reduction":
                solar[h] = solar[h] * d.factor
            elif d.directive_type == "minimum_battery_reserve":
                e_min[h] = max(e_min[h], d.minimum_energy_kwh)
            elif d.directive_type == "no_charge_window":
                charge_max[h] = 0.0
            elif d.directive_type == "no_discharge_window":
                discharge_max[h] = 0.0
            elif d.directive_type == "max_grid_window":
                grid_cap[h] = min(grid_cap[h], d.max_grid_kwh)
    return Problem(demand, solar, tariff, e_min, battery.capacity_kwh,
                   battery.initial_energy_kwh, charge_max, discharge_max, grid_cap)


# ---------------------------------------------------------------- LP (HiGHS)

def _solve_lp(p: Problem) -> list[float]:
    """Return net battery flow per hour (+charge / -discharge)."""
    import numpy as np
    from scipy.optimize import linprog

    n = 24
    # variable layout: [grid(24), solar(24), charge(24), discharge(24), E(24)]
    G, S, C, D, E = (k * n for k in range(5))
    nv = 5 * n
    cost = np.zeros(nv)
    cost[G:G + n] = p.tariff
    cost[C:C + n] = EPS_THROUGHPUT
    cost[D:D + n] = EPS_THROUGHPUT

    A = np.zeros((2 * n, nv))
    b = np.zeros(2 * n)
    for h in range(n):
        # energy balance: grid + solar + discharge - charge = demand
        A[h, G + h] = 1
        A[h, S + h] = 1
        A[h, D + h] = 1
        A[h, C + h] = -1
        b[h] = p.demand[h]
        # battery transition: E[h] - E[h-1] - charge + discharge = 0
        r = n + h
        A[r, E + h] = 1
        A[r, C + h] = -1
        A[r, D + h] = 1
        if h == 0:
            b[r] = p.initial
        else:
            A[r, E + h - 1] = -1

    bounds = []
    bounds += [(0, None if math.isinf(c) else c) for c in p.grid_cap]
    bounds += [(0, s) for s in p.solar]
    bounds += [(0, c) for c in p.charge_max]
    bounds += [(0, d) for d in p.discharge_max]
    bounds += [(p.e_min[h], p.e_max) for h in range(n - 1)]
    bounds += [(p.initial, p.initial)]  # end-of-day neutrality

    res = linprog(cost, A_eq=A, b_eq=b, bounds=bounds, method="highs")
    if res.status == 2:
        raise InfeasibleError("no schedule satisfies all constraints")
    if res.status != 0:
        raise RuntimeError(f"LP solver failed: {res.message}")
    x = res.x
    return [float(x[C + h] - x[D + h]) for h in range(n)]


# ---------------------------------------------------------------- DP fallback

def _solve_dp(p: Problem, step: float = 0.5) -> list[float]:
    """Exact on a grid of battery levels initial + k*step. Used only if the LP is unavailable."""
    lo_k = math.ceil((min(p.e_min) - p.initial) / step - 1e-9)
    hi_k = math.floor((p.e_max - p.initial) / step + 1e-9)
    levels = list(range(lo_k, hi_k + 1))
    INF = math.inf
    cost = {0: 0.0}
    parent: list[dict[int, int]] = []
    for h in range(24):
        nxt: dict[int, float] = {}
        par: dict[int, int] = {}
        up = int(p.charge_max[h] / step + 1e-9)
        dn = int(p.discharge_max[h] / step + 1e-9)
        net_load = p.demand[h] - p.solar[h]
        for i, c in cost.items():
            for j in range(max(levels[0], i - dn), min(levels[-1], i + up) + 1):
                e = p.initial + j * step
                if e < p.e_min[h] - 1e-9 or (h == 23 and j != 0):
                    continue
                grid = max(0.0, net_load + (j - i) * step)
                if grid > p.grid_cap[h] + 1e-9:
                    continue
                v = c + grid * p.tariff[h]
                if v < nxt.get(j, INF):
                    nxt[j] = v
                    par[j] = i
        if not nxt:
            raise InfeasibleError("no schedule satisfies all constraints")
        cost = nxt
        parent.append(par)
    if 0 not in cost:
        raise InfeasibleError("no schedule satisfies all constraints")
    path = [0]
    for h in range(23, 0, -1):
        path.append(parent[h][path[-1]])
    path.reverse()  # level index after each hour 0..23
    prev = 0
    flows = []
    for j in path:
        flows.append((j - prev) * step)
        prev = j
    return flows


# ---------------------------------------------------------------- plan assembly

def _r(x: float) -> float:
    v = round(x, DECIMALS)
    return 0.0 if v == 0 else v


def build_plan(p: Problem, flows: list[float]) -> list[dict]:
    """Turn net battery flows into the hourly_plan, recomputing every value
    from rounded numbers so the judge's replay matches exactly."""
    flows = [_r(f) for f in flows]
    # force exact end-of-day neutrality after rounding
    drift = _r(sum(flows))
    if drift:
        flows[-1] = _r(flows[-1] - drift)

    plan = []
    e = p.initial
    for h in range(24):
        f = flows[h]
        # clamp tiny numerical overshoot against rate limits
        if f > 0:
            f = min(f, p.charge_max[h])
        elif f < 0:
            f = max(f, -p.discharge_max[h])
        e = _r(e + f)
        load = p.demand[h] + f           # demand + charge - discharge
        solar_used = _r(min(p.solar[h], max(0.0, load)))
        grid = _r(max(0.0, load - solar_used))
        action = "charge" if f > 0 else "discharge" if f < 0 else "idle"
        plan.append({
            "hour": h,
            "grid_kwh": grid,
            "solar_used_kwh": solar_used,
            "battery_action": action,
            "battery_kwh": _r(abs(f)),
            "battery_energy_after_kwh": e,
        })
    return plan


def optimize(p: Problem) -> tuple[list[dict], str]:
    """Return (hourly_plan, solver_name)."""
    try:
        flows = _solve_lp(p)
        solver = "highs-lp"
    except InfeasibleError:
        raise
    except Exception as exc:  # scipy missing or solver error -> exact DP fallback
        log.warning("LP solver unavailable (%s); using DP fallback", type(exc).__name__)
        flows = _solve_dp(p)
        solver = "dp"
    return build_plan(p, flows), solver


def warm_up() -> None:
    """Import SciPy and run one tiny solve so HiGHS is loaded before the first request."""
    try:
        _solve_lp(Problem(demand=[10.0] * 24, solar=[0.0] * 24, tariff=[1.0] * 24, e_min=[0.0] * 24,
                          e_max=10.0, initial=5.0, charge_max=[1.0] * 24, discharge_max=[1.0] * 24,
                          grid_cap=[math.inf] * 24))
    except Exception as exc:  # never block startup; the DP fallback still works
        log.warning("solver warm-up failed: %s", type(exc).__name__)


def totals(plan: list[dict], tariff: list[float]) -> tuple[float, float, float]:
    grid = [row["grid_kwh"] for row in plan]
    return (_r(sum(grid)),
            _r(sum(g * t for g, t in zip(grid, tariff))),
            _r(max(grid)))
