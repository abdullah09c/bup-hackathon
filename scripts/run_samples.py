"""POST every public sample case to a running service and verify it like the judge.

Usage:
    python scripts/run_samples.py [BASE_URL] [--repeat N]
    python scripts/run_samples.py http://localhost:8000
    python scripts/run_samples.py https://your-app.example.com --repeat 3

Checks per case: HTTP 200, interpretation vs reference (type/hours/values),
judge-style replay of hourly_plan against the TRUE directives, and cost vs the
reference optimum. Prints p50/p95 latency at the end. Exit code 1 on any failure.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.guardrails import Directive  # noqa: E402
from app.optimizer import build_problem  # noqa: E402
from app.schemas import OptimizeRequest  # noqa: E402
from app.validator import replay  # noqa: E402

SAMPLES = ROOT / "BUP_CSE_FEST_2026_Participant_Docs" / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"


def truth(case):
    out = []
    for di in case["expected_output"]["directive_interpretation"]:
        adj = di["structured_adjustment"] or {}
        out.append(Directive(di["note_index"], di["directive_type"], adj.get("hours", []), adj.get("factor"),
                             adj.get("minimum_energy_kwh"), adj.get("max_grid_kwh")))
    return out


def same_adj(a, b):
    if a is None or b is None:
        return a is b
    if set(a) != set(b) or a.get("hours") != b.get("hours"):
        return False
    return all(abs(a[k] - b[k]) <= 0.01 for k in a if k != "hours")


def post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            return resp.status, json.loads(resp.read()), time.perf_counter() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:200], time.perf_counter() - t0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    base = (args[0] if args else "http://localhost:8000").rstrip("/")
    repeat = int(sys.argv[sys.argv.index("--repeat") + 1]) if "--repeat" in sys.argv else 1
    cases = json.loads(SAMPLES.read_text(encoding="utf-8"))["cases"]

    with urllib.request.urlopen(f"{base}/health", timeout=10) as r:
        print(f"GET /health -> {r.status} {r.read().decode()}")

    latencies, failures = [], 0
    for _ in range(repeat):
        for case in cases:
            status, body, dt = post(f"{base}/optimize-energy", case["input"])
            latencies.append(dt)
            problems = []
            if status != 200:
                problems.append(f"HTTP {status}: {body}")
            else:
                eo = case["expected_output"]
                for got, exp in zip(body["directive_interpretation"], eo["directive_interpretation"]):
                    if (got["applies"], got["directive_type"]) != (exp["applies"], exp["directive_type"]) or \
                            not same_adj(got["structured_adjustment"], exp["structured_adjustment"]):
                        problems.append(f"note {exp['note_index']}: got {got['directive_type']} {got['structured_adjustment']}")
                if len(body["directive_interpretation"]) != len(eo["directive_interpretation"]):
                    problems.append("wrong number of interpretations")
                req = OptimizeRequest.model_validate(case["input"])
                p = build_problem(req.hours, req.battery, truth(case))
                problems += replay(p, body["hourly_plan"], (body["total_grid_kwh"], body["total_cost_bdt"], body["peak_grid_kwh"]))
                if abs(body["total_cost_bdt"] - eo["total_cost_bdt"]) > 0.01:
                    problems.append(f"cost {body['total_cost_bdt']} vs optimal {eo['total_cost_bdt']}")
            failures += bool(problems)
            print(f"{case['id']}: {'PASS' if not problems else 'FAIL'}  {dt * 1000:.0f} ms"
                  + ("" if not problems else "\n    " + "\n    ".join(map(str, problems[:5]))))

    lat = sorted(latencies)
    p95 = lat[min(len(lat) - 1, int(round(0.95 * len(lat) + 0.5)) - 1)]
    print(f"\n{len(lat) - failures}/{len(lat)} passed | p50 {statistics.median(lat) * 1000:.0f} ms | p95 {p95 * 1000:.0f} ms")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
