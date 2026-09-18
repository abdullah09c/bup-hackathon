"""Final replay validator: re-checks a finished plan the same way the judge does (§09, §11.3)."""
from __future__ import annotations

import math

from .optimizer import Problem

TOL = 0.01


def replay(p: Problem, plan: list[dict], reported: tuple[float, float, float] | None = None) -> list[str]:
    errs: list[str] = []
    if [row["hour"] for row in plan] != list(range(24)):
        return ["hourly_plan must list hours 0..23 in order"]
    e = p.initial
    for h, row in enumerate(plan):
        act, k = row["battery_action"], row["battery_kwh"]
        vals = (row["grid_kwh"], row["solar_used_kwh"], k, row["battery_energy_after_kwh"])
        if any(not math.isfinite(v) or v < -TOL for v in vals):
            errs.append(f"h{h}: negative or non-finite value")
        chg = k if act == "charge" else 0.0
        dis = k if act == "discharge" else 0.0
        if act == "idle" and abs(k) > TOL:
            errs.append(f"h{h}: idle with non-zero battery_kwh")
        if chg > p.charge_max[h] + TOL:
            errs.append(f"h{h}: charge limit / no_charge window violated")
        if dis > p.discharge_max[h] + TOL:
            errs.append(f"h{h}: discharge limit / no_discharge window violated")
        e = e + chg - dis
        if abs(e - row["battery_energy_after_kwh"]) > TOL:
            errs.append(f"h{h}: battery transition mismatch")
        if e < p.e_min[h] - TOL or e > p.e_max + TOL:
            errs.append(f"h{h}: battery energy {e:.4f} outside [{p.e_min[h]}, {p.e_max}]")
        if row["solar_used_kwh"] > p.solar[h] + TOL:
            errs.append(f"h{h}: solar used exceeds effective solar")
        if row["grid_kwh"] > p.grid_cap[h] + TOL:
            errs.append(f"h{h}: grid cap violated")
        if abs(row["grid_kwh"] + row["solar_used_kwh"] + dis - p.demand[h] - chg) > TOL:
            errs.append(f"h{h}: energy balance violated")
    if abs(e - p.initial) > TOL:
        errs.append("end-of-day battery energy differs from initial")
    if reported is not None:
        grid = [row["grid_kwh"] for row in plan]
        total, cost, peak = reported
        if abs(sum(grid) - total) > TOL:
            errs.append("total_grid_kwh mismatch")
        if abs(sum(g * t for g, t in zip(grid, p.tariff)) - cost) > TOL:
            errs.append("total_cost_bdt mismatch")
        if abs(max(grid) - peak) > TOL:
            errs.append("peak_grid_kwh mismatch")
    return errs
