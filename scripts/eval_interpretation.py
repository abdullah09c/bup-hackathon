"""Evaluate LLM note interpretation on paraphrased notes (uses the configured LLM from .env).

Usage:
    python scripts/eval_interpretation.py                         # models from .env (with rotation/fallback)
    python scripts/eval_interpretation.py openai/gpt-oss-120b@low qwen/qwen3.8-27b@none --pace 8
    python scripts/eval_interpretation.py fallback:gemini-3.8-flash --pace 8      (fallback provider)

Every note is checked for directive type, hours and value (factor / kWh) after
the guardrails. A result that came from the rule-based fallback instead of the
LLM is reported as a failure of the LLM path.
"""
from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings  # noqa: E402
from app.llm import client, interpreter  # noqa: E402

S, NC, ND, R, G, NO = ("solar_reduction", "no_charge_window", "no_discharge_window",
                       "minimum_battery_reserve", "max_grid_window", "no_op")

# (note, expected (type, hours, value))  value = factor | kWh | None
NOTES = [
    ("PV production will drop to about 20% between 13:00 and 15:00.", (S, [13, 14], 0.2)),
    ("Panel washing from one until three will leave roughly one-fifth of normal solar output.", (S, [13, 14], 0.2)),
    ("Expect an 80% reduction in rooftop solar during the 1-3 PM maintenance window.", (S, [13, 14], 0.2)),
    ("Solar generation is expected to be cut by 40 percent from 9 AM to noon.", (S, [9, 10, 11], 0.6)),
    ("Haze will limit the rooftop array to half its forecast between 2 and 5 in the afternoon.", (S, [14, 15, 16], 0.5)),
    ("The PV inverters will be fully offline from 11:00 to 13:00.", (S, [11, 12], 0.0)),
    ("Only a quarter of the usual solar yield is expected 10 AM–1 PM due to dust.", (S, [10, 11, 12], 0.25)),
    ("Solar output will be reduced by three quarters from 12 to 2 PM.", (S, [12, 13], 0.25)),
    ("Between 3 and 6 PM, only 35% of normal solar will be available.", (S, [15, 16, 17], 0.35)),
    ("The battery must not be charged from 1 AM to 4 AM.", (NC, [1, 2, 3], None)),
    ("Charger maintenance: no battery charging for three hours starting at 9 PM.", (NC, [21, 22, 23], None)),
    ("From 10 PM to 2 AM the battery cannot be charged.", (NC, [0, 1, 22, 23], None)),
    ("Battery discharge is prohibited between 17:00 and 19:00 for relay testing.", (ND, [17, 18], None)),
    ("Please don't pull energy out of the battery from 6 to 8 in the evening.", (ND, [18, 19], None)),
    ("No discharging the battery from noon to 1 PM.", (ND, [12], None)),
    ("Hold a minimum of 60 kWh in the battery from 7 PM till 11 PM.", (R, [19, 20, 21, 22], 60)),
    ("Keep the battery at least 30% charged from 8 PM until midnight.", (R, [20, 21, 22, 23], 60)),       # cap 200
    ("Maintain half of the battery capacity as backup between 17:00 and 20:00.", (R, [17, 18, 19], 100)),  # cap 200
    ("Grid draw must stay under 150 kWh per hour from 6 PM to 9 PM.", (G, [18, 19, 20], 150)),
    ("The utility has capped our import at 120 kWh between noon and 3 PM.", (G, [12, 13, 14], 120)),
    ("Transformer overload risk: limit grid imports to 200 kWh from 17:00 until 21:00.", (G, [17, 18, 19, 20], 200)),
    ("Evening feeder constraint — no more than 175 kWh from the grid in any hour from 7 to 10 PM.", (G, [19, 20, 21], 175)),
    ("Grid import may not exceed 90 kWh from midnight to 5 AM.", (G, [0, 1, 2, 3, 4], 90)),
    ("The cafeteria menu changes tomorrow.", (NO, None, None)),
    ("A solar panel vendor will visit next week to discuss an upgrade.", (NO, None, None)),
    ("The electricity tariff will rise next month.", (NO, None, None)),
    ("Facilities staff meeting at 3 PM in room 204.", (NO, None, None)),
    ("Battery warranty paperwork was filed yesterday.", (NO, None, None)),
    ("Last week's grid outage report has been published.", (NO, None, None)),
    ("The library is extending book-return hours next week.", (NO, None, None)),
]
CAPACITY = 200.0


def value_of(d):
    return d.factor if d.directive_type == S else d.minimum_energy_kwh if d.directive_type == R \
        else d.max_grid_kwh if d.directive_type == G else None


def matches(d, exp):
    t, hours, val = exp
    if d.directive_type != t:
        return False
    if t == NO:
        return True
    if d.hours != hours:
        return False
    return val is None or (value_of(d) is not None and abs(value_of(d) - val) <= 0.01)


async def run_model(settings, model, pace):
    if model:  # evaluate one model alone; "fallback:<model>" uses the fallback provider's endpoint
        use_fallback = model.startswith("fallback:")
        ep = settings.endpoints[settings.n_primary if use_fallback else 0]
        ep.model, _, effort = model.removeprefix("fallback:").partition("@")
        ep.reasoning_effort = effort or None
        ep.name = f"eval:{model}"
        settings.endpoints, settings.n_primary = [ep], 1
    label = model or ",".join(e.model for e in settings.endpoints)
    interpreter._CACHE.clear()
    # requests of 3 notes (mixes directives and distractors like hidden cases)
    order = list(range(len(NOTES)))
    order = order[::3] + order[1::3] + order[2::3]
    groups = [order[i:i + 3] for i in range(0, len(order), 3)]
    ok, fails, lat = 0, [], []
    for gi, g in enumerate(groups):
        if gi and pace:
            await asyncio.sleep(pace)
        notes = [NOTES[i][0] for i in g]
        t = time.perf_counter()
        ds, src = await interpreter.interpret(notes, CAPACITY, settings)
        lat.append(time.perf_counter() - t)
        for i, d in zip(g, ds):
            good = matches(d, NOTES[i][1]) and "rule-parser" not in src
            ok += good
            if not good:
                fails.append(f"  [{src}] {NOTES[i][0]!r}\n      expected {NOTES[i][1]}, got "
                             f"({d.directive_type}, {d.hours or None}, {value_of(d)})")
    p95 = sorted(lat)[max(0, int(round(0.95 * len(lat))) - 1)]
    print(f"\n=== {label}: {ok}/{len(NOTES)} correct | request latency p50 {statistics.median(lat):.2f}s "
          f"p95 {p95:.2f}s max {max(lat):.2f}s")
    print("\n".join(fails) if fails else "  all correct")


async def main():
    args = sys.argv[1:]
    pace = 0.0
    if "--pace" in args:
        i = args.index("--pace")
        pace = float(args[i + 1])
        del args[i:i + 2]
    for m in args or [None]:
        await run_model(load_settings(), m, pace)
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
