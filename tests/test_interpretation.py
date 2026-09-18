"""Guardrails, rule-based parser and the LLM interpreter (with a mocked LLM)."""
import asyncio
import json

import pytest

from app.config import LLMEndpoint, Settings
from app.guardrails import GuardrailError, hours_from_window, normalize_all, normalize_item
from app.llm import client, interpreter
from app.llm.rule_parser import parse_note
from conftest import load_cases

CAP = 200.0


# ---------------------------------------------------------------- guardrails

def test_window_end_exclusive_and_wrap():
    assert hours_from_window(13, 15) == [13, 14]
    assert hours_from_window(19, 24) == [19, 20, 21, 22, 23]
    assert hours_from_window(19, 0) == [19, 20, 21, 22, 23]
    assert hours_from_window(22, 2) == [0, 1, 22, 23]
    with pytest.raises(GuardrailError):
        hours_from_window(5, 5)
    with pytest.raises(GuardrailError):
        hours_from_window(25, 3)


@pytest.mark.parametrize("value,kind,factor", [
    (80, "percent_reduction", 0.2), (20, "percent_remaining", 0.2),
    (0.2, "fraction_remaining", 0.2), (0.75, "fraction_reduction", 0.25), (0, "percent_remaining", 0.0),
])
def test_solar_factor_normalization(value, kind, factor):
    d = normalize_item({"directive_type": "solar_reduction", "start_hour": 13, "end_hour": 15,
                        "value": value, "value_kind": kind}, 0, CAP)
    assert d.factor == pytest.approx(factor)
    assert d.structured_adjustment() == {"hours": [13, 14], "factor": pytest.approx(factor)}


def test_reserve_percent_of_capacity():
    d = normalize_item({"directive_type": "minimum_battery_reserve", "start_hour": 18, "end_hour": 21,
                        "value": 50, "value_kind": "percent_of_capacity"}, 0, CAP)
    assert d.minimum_energy_kwh == 100
    assert d.hours == [18, 19, 20]


@pytest.mark.parametrize("item", [
    {"directive_type": "shed_load", "start_hour": 1, "end_hour": 2},
    {"directive_type": "solar_reduction", "start_hour": 1, "end_hour": 2, "value": 150, "value_kind": "percent_remaining"},
    {"directive_type": "minimum_battery_reserve", "start_hour": 1, "end_hour": 2, "value": 900, "value_kind": "kwh"},
    {"directive_type": "max_grid_window", "start_hour": 1, "end_hour": 2, "value": -5, "value_kind": "kwh"},
    {"directive_type": "max_grid_window", "start_hour": 1, "end_hour": 2, "value": "NaN", "value_kind": "kwh"},
    {"directive_type": "no_charge_window", "hours": [3, 99]},
    {"directive_type": "solar_reduction", "start_hour": 1, "end_hour": 2, "value": 20, "value_kind": "kwh"},
])
def test_guardrails_reject_bad_items(item):
    with pytest.raises(GuardrailError):
        normalize_item(item, 0, CAP)


def test_no_op_shape_and_duplicate_mapping():
    items = [{"note_index": 0, "directive_type": "no_op", "value": 5},
             {"note_index": 0, "directive_type": "no_charge_window", "start_hour": 1, "end_hour": 3}]
    ds, errs = normalize_all(items, 2, CAP)
    assert ds[0].to_interpretation() == {"note_index": 0, "applies": False, "directive_type": "no_op",
                                         "structured_adjustment": None, "explanation": ds[0].explanation}
    assert ds[1] is None and 1 in errs  # duplicate index 0 ignored, note 1 reported missing


def test_hours_sorted_unique():
    d = normalize_item({"directive_type": "no_discharge_window", "hours": [19, 18, 18.0]}, 0, CAP)
    assert d.hours == [18, 19]


# ---------------------------------------------------------------- rule parser (stub / last resort)

def _truth(case):
    return [(di["directive_type"], di["structured_adjustment"]) for di in case["expected_output"]["directive_interpretation"]]


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["id"])
def test_rule_parser_on_public_samples(case):
    cap = case["input"]["battery"]["capacity_kwh"]
    got = []
    for i, note in enumerate(case["input"]["operator_notes"]):
        d = normalize_item(parse_note(note, i), i, cap)
        got.append((d.directive_type, d.structured_adjustment()))
    assert got == _truth(case)


@pytest.mark.parametrize("note,expected", [
    ("PV production will drop to about 20% between 13:00 and 15:00.", ("solar_reduction", [13, 14], 0.2)),
    ("Panel washing from one until three will leave roughly one-fifth of normal solar output.", ("solar_reduction", [13, 14], 0.2)),
    ("Expect an 80% reduction in rooftop solar during the 1-3 PM maintenance window.", ("solar_reduction", [13, 14], 0.2)),
    ("Do not charge the battery between 2 PM and 4 PM.", ("no_charge_window", [14, 15], None)),
    ("Keep at least 120 kWh in reserve from 6 PM until 9 PM.", ("minimum_battery_reserve", [18, 19, 20], 120)),
    ("The cafeteria menu changes tomorrow.", ("no_op", None, None)),
])
def test_rule_parser_spec_paraphrases(note, expected):
    d = normalize_item(parse_note(note, 0), 0, 500)
    adj = d.structured_adjustment()
    val = None if adj is None else adj.get("factor", adj.get("minimum_energy_kwh", adj.get("max_grid_kwh")))
    assert (d.directive_type, adj and adj["hours"], val) == expected


# ---------------------------------------------------------------- interpreter with mocked LLM

EP = LLMEndpoint(name="primary:test:model", base_url="http://llm.invalid", api_key="k", model="m")
EP2 = LLMEndpoint(name="fallback:test:model", base_url="http://llm2.invalid", api_key="k2", model="m2")
NOTES = ["Do not charge the battery between 2 PM and 4 PM.", "The cafeteria menu changes tomorrow."]
GOOD = {"interpretations": [
    {"note_index": 0, "directive_type": "no_charge_window", "start_hour": 14, "end_hour": 16, "explanation": "x"},
    {"note_index": 1, "directive_type": "no_op", "explanation": "y"}]}


def _run(notes, settings):
    interpreter._CACHE.clear()
    return asyncio.run(interpreter.interpret(notes, CAP, settings))


def test_llm_success(monkeypatch):
    async def fake(ep, messages, timeout):
        return GOOD, json.dumps(GOOD)
    monkeypatch.setattr(client, "chat_json", fake)
    ds, src = _run(NOTES, Settings([EP], 5, 20))
    assert src == EP.name
    assert [d.directive_type for d in ds] == ["no_charge_window", "no_op"]
    assert ds[0].hours == [14, 15]


def test_llm_retry_with_feedback(monkeypatch):
    calls = []
    bad = {"interpretations": [{"note_index": 0, "directive_type": "charging_ban", "start_hour": 14, "end_hour": 16},
                               {"note_index": 1, "directive_type": "no_op"}]}

    async def fake(ep, messages, timeout):
        calls.append(messages)
        return (bad if len(calls) == 1 else GOOD), "{}"
    monkeypatch.setattr(client, "chat_json", fake)
    ds, _ = _run(NOTES, Settings([EP], 5, 20))
    assert len(calls) == 2 and "failed validation" in calls[1][-1]["content"]
    assert ds[0].directive_type == "no_charge_window"


def test_llm_primary_down_uses_fallback(monkeypatch):
    async def fake(ep, messages, timeout):
        if ep is EP:
            raise client.LLMError("LLM HTTP 429")
        return GOOD, "{}"
    monkeypatch.setattr(client, "chat_json", fake)
    ds, src = _run(NOTES, Settings([EP, EP2], 5, 20))
    assert src == EP2.name and ds[0].directive_type == "no_charge_window"


def test_all_llms_down_uses_rule_parser(monkeypatch):
    async def fake(ep, messages, timeout):
        raise client.LLMError("LLM request timed out")
    monkeypatch.setattr(client, "chat_json", fake)
    ds, src = _run(NOTES, Settings([EP, EP2], 5, 20))
    assert src == "rule-parser"
    assert [d.directive_type for d in ds] == ["no_charge_window", "no_op"]


def test_cache_skips_llm(monkeypatch):
    calls = []

    async def fake(ep, messages, timeout):
        calls.append(1)
        return GOOD, "{}"
    monkeypatch.setattr(client, "chat_json", fake)
    s = Settings([EP], 5, 20)
    _run(NOTES, s)
    ds, src = asyncio.run(interpreter.interpret(NOTES, CAP, s))
    assert src == "cache" and len(calls) == 1


def test_extract_json_tolerates_fences():
    assert client.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert client.extract_json('Sure! {"a": 2} hope that helps') == {"a": 2}
    with pytest.raises(client.LLMError):
        client.extract_json("no json here")


def test_fallback_tiers_in_order(monkeypatch):
    """Every primary model rate-limited -> fallback tier; fallback down too -> fallback2."""
    p1 = LLMEndpoint(name="primary:a", base_url="http://p.invalid", api_key="k", model="a")
    p2 = LLMEndpoint(name="primary:b", base_url="http://p.invalid", api_key="k", model="b")
    f1 = LLMEndpoint(name="fallback:c", base_url="http://f.invalid", api_key="k2", model="c")
    f2 = LLMEndpoint(name="fallback2:d", base_url="http://g.invalid", api_key="k3", model="d")
    tried = []

    async def fake(ep, messages, timeout):
        tried.append(ep.name)
        if ep is f2:
            return GOOD, "{}"
        raise client.LLMError("LLM HTTP 429", status=429)
    monkeypatch.setattr(client, "chat_json", fake)
    interpreter._COOLDOWN.clear()
    ds, src = _run(NOTES, Settings([p1, p2, f1, f2], 5, 20, n_primary=2))
    assert src == "fallback2:d"
    assert set(tried[:2]) == {"primary:a", "primary:b"} and tried[2:] == ["fallback:c", "fallback2:d"]
    interpreter._COOLDOWN.clear()
