"""HTTP contract: status codes, schema, end-to-end on public samples (stub interpreter)."""
import copy

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import Settings
from app.optimizer import build_problem
from app.schemas import OptimizeRequest
from app.validator import replay
from conftest import load_cases
from test_optimizer import truth_directives

main.settings = Settings([], 5, 20)  # no LLM configured -> stub interpreter
client = TestClient(main.app, raise_server_exceptions=False)
CASES = load_cases()


def test_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_samples_end_to_end(case):
    r = client.post("/optimize-energy", json=case["input"])
    assert r.status_code == 200, r.text
    body = r.json()
    eo = case["expected_output"]
    assert list(body) == list(eo)  # same top-level keys, same order
    assert body["scenario_id"] == case["input"]["scenario_id"]
    assert [d["note_index"] for d in body["directive_interpretation"]] == list(range(len(case["input"]["operator_notes"])))
    assert [(d["applies"], d["directive_type"], d["structured_adjustment"]) for d in body["directive_interpretation"]] == \
        [(d["applies"], d["directive_type"], d["structured_adjustment"]) for d in eo["directive_interpretation"]]
    # judge-style replay against the TRUE directives
    req = OptimizeRequest.model_validate(case["input"])
    p = build_problem(req.hours, req.battery, truth_directives(case))
    assert replay(p, body["hourly_plan"], (body["total_grid_kwh"], body["total_cost_bdt"], body["peak_grid_kwh"])) == []
    assert body["total_cost_bdt"] == pytest.approx(eo["total_cost_bdt"], abs=0.01)
    assert isinstance(body["plan_summary"], str) and body["plan_summary"]


def _mutate(fn):
    data = copy.deepcopy(CASES[0]["input"])
    fn(data)
    return data


@pytest.mark.parametrize("payload", [
    _mutate(lambda d: d["hours"].pop()),                                   # 23 hours
    _mutate(lambda d: d["hours"][5].update(hour=4)),                       # duplicate hour
    _mutate(lambda d: d.update(operator_notes=[])),                        # no notes
    _mutate(lambda d: d.update(operator_notes=["a", "b", "c", "d"])),      # too many notes
    _mutate(lambda d: d.update(operator_notes=["   "])),                   # blank note
    _mutate(lambda d: d["hours"][3].update(demand_kwh=-1)),                # negative
    _mutate(lambda d: d["battery"].pop("capacity_kwh")),                   # missing field
    _mutate(lambda d: d["hours"][0].update(tariff_bdt_per_kwh="cheap")),   # wrong type
    _mutate(lambda d: d.pop("scenario_id")),
    [1, 2, 3],
])
def test_structural_errors_return_400(payload):
    r = client.post("/optimize-energy", json=payload)
    assert r.status_code == 400
    assert "error" in r.json()


def test_malformed_json_returns_400():
    r = client.post("/optimize-energy", content=b'{"scenario_id": ', headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_nan_rejected():
    r = client.post("/optimize-energy", content=b'{"scenario_id":"x","operator_notes":["a"],"hours":[],"battery":{"capacity_kwh":NaN}}',
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_semantic_error_returns_422():
    r = client.post("/optimize-energy", json=_mutate(lambda d: d["battery"].update(initial_energy_kwh=10_000)))
    assert r.status_code == 422


def test_infeasible_returns_422():
    # grid capped at 0 all day while demand exceeds what solar+battery can cover
    data = _mutate(lambda d: d.update(operator_notes=["Grid import must not exceed 0 kWh from midnight until 11 PM."]))
    r = client.post("/optimize-energy", json=data)
    assert r.status_code == 422
