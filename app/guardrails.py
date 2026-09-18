"""Deterministic guardrails (Problem Statement §05.1 and §08).

The LLM never produces final numbers directly. It returns *raw* fields
(start/end hour, a value and what kind of value it is) and this module does
all the arithmetic and validation. Anything that fails here is rejected and
never reaches the optimizer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from .schemas import DIRECTIVE_TYPES

HOURLY_TYPES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
}

# value_kind values accepted for each directive type
VALUE_KINDS = {
    "solar_reduction": {"percent_reduction", "percent_remaining", "fraction_remaining", "fraction_reduction"},
    "minimum_battery_reserve": {"kwh", "percent_of_capacity", "fraction_of_capacity"},
    "max_grid_window": {"kwh"},
}


class GuardrailError(ValueError):
    pass


@dataclass
class Directive:
    note_index: int
    directive_type: str
    hours: list[int] = field(default_factory=list)
    factor: Optional[float] = None
    minimum_energy_kwh: Optional[float] = None
    max_grid_kwh: Optional[float] = None
    explanation: str = ""

    @property
    def applies(self) -> bool:
        return self.directive_type != "no_op"

    def structured_adjustment(self) -> Optional[dict]:
        t = self.directive_type
        if t == "no_op":
            return None
        adj: dict[str, Any] = {"hours": list(self.hours)}
        if t == "solar_reduction":
            adj["factor"] = self.factor
        elif t == "minimum_battery_reserve":
            adj["minimum_energy_kwh"] = self.minimum_energy_kwh
        elif t == "max_grid_window":
            adj["max_grid_kwh"] = self.max_grid_kwh
        return adj

    def to_interpretation(self) -> dict:
        return {
            "note_index": self.note_index,
            "applies": self.applies,
            "directive_type": self.directive_type,
            "structured_adjustment": self.structured_adjustment(),
            "explanation": self.explanation or _default_explanation(self),
        }


def no_op(note_index: int, explanation: str = "") -> Directive:
    return Directive(note_index=note_index, directive_type="no_op",
                     explanation=explanation or "This note does not affect today's 24-hour energy schedule.")


# ---------------------------------------------------------------- helpers

def _as_int(v: Any, name: str) -> int:
    if isinstance(v, bool) or v is None:
        raise GuardrailError(f"{name} must be an integer")
    if isinstance(v, str):
        v = v.strip()
        try:
            v = float(v)
        except ValueError:
            raise GuardrailError(f"{name} must be an integer") from None
    if isinstance(v, float):
        if not math.isfinite(v) or v != int(v):
            raise GuardrailError(f"{name} must be a whole hour")
        v = int(v)
    if not isinstance(v, int):
        raise GuardrailError(f"{name} must be an integer")
    return v


def _as_number(v: Any, name: str) -> float:
    if isinstance(v, bool) or v is None:
        raise GuardrailError(f"{name} is required")
    if isinstance(v, str):
        try:
            v = float(v.strip().rstrip("%"))
        except ValueError:
            raise GuardrailError(f"{name} must be a number") from None
    if not isinstance(v, (int, float)) or not math.isfinite(v):
        raise GuardrailError(f"{name} must be a finite number")
    return float(v)


def hours_from_window(start: Any, end: Any) -> list[int]:
    """Start-inclusive, end-exclusive whole-hour window. end may be 24 (midnight).
    A window that crosses midnight (e.g. 22 -> 2) wraps within the same horizon."""
    s = _as_int(start, "start_hour")
    e = _as_int(end, "end_hour")
    if not 0 <= s <= 23:
        raise GuardrailError("start_hour must be within 0..23")
    if not 0 <= e <= 24:
        raise GuardrailError("end_hour must be within 0..24")
    if e == 0:
        e = 24
    if s == e:
        raise GuardrailError("empty time window (start_hour == end_hour)")
    if e > s:
        return list(range(s, e))
    return sorted(set(range(s, 24)) | set(range(0, e)))


def normalize_hours(hours: Any) -> list[int]:
    if not isinstance(hours, list) or not hours:
        raise GuardrailError("hours must be a non-empty list")
    out = sorted({_as_int(h, "hour") for h in hours})
    if out[0] < 0 or out[-1] > 23:
        raise GuardrailError("hours must be integers within 0..23")
    return out


def _clean_explanation(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())[:240]


# ---------------------------------------------------------------- main entry

def normalize_item(item: Any, note_index: int, capacity_kwh: float) -> Directive:
    """Validate one raw interpretation item and return a normalized Directive."""
    if not isinstance(item, dict):
        raise GuardrailError("interpretation item must be an object")
    t = item.get("directive_type")
    t = t.strip().lower() if isinstance(t, str) else t
    if t not in DIRECTIVE_TYPES:
        raise GuardrailError(f"unsupported directive_type {t!r}")
    explanation = _clean_explanation(item.get("explanation"))
    if t == "no_op":
        return no_op(note_index, explanation)

    if item.get("hours"):
        hours = normalize_hours(item["hours"])
    else:
        hours = hours_from_window(item.get("start_hour"), item.get("end_hour"))

    d = Directive(note_index=note_index, directive_type=t, hours=hours, explanation=explanation)
    if t in VALUE_KINDS:
        kind = item.get("value_kind")
        kind = kind.strip().lower() if isinstance(kind, str) else kind
        if kind not in VALUE_KINDS[t]:
            raise GuardrailError(f"value_kind {kind!r} is not valid for {t}; use one of {sorted(VALUE_KINDS[t])}")
        v = _as_number(item.get("value"), "value")

        if t == "solar_reduction":
            factor = {
                "percent_reduction": 1 - v / 100,
                "percent_remaining": v / 100,
                "fraction_remaining": v,
                "fraction_reduction": 1 - v,
            }[kind]
            if not -1e-9 <= factor <= 1 + 1e-9:
                raise GuardrailError(f"solar factor {factor} outside 0..1")
            d.factor = round(min(1.0, max(0.0, factor)), 6)

        elif t == "minimum_battery_reserve":
            kwh = {
                "kwh": v,
                "percent_of_capacity": v / 100 * capacity_kwh,
                "fraction_of_capacity": v * capacity_kwh,
            }[kind]
            if kwh < 0 or kwh > capacity_kwh + 1e-9:
                raise GuardrailError(f"reserve {kwh} kWh outside 0..capacity ({capacity_kwh})")
            d.minimum_energy_kwh = round(kwh, 6)

        elif t == "max_grid_window":
            if v < 0:
                raise GuardrailError("max_grid_kwh must be non-negative")
            d.max_grid_kwh = round(v, 6)
    return d


def normalize_all(items: Any, n_notes: int, capacity_kwh: float) -> tuple[list[Optional[Directive]], dict[int, str]]:
    """Map raw LLM items to one Directive per note.

    Returns (directives, errors). directives[i] is None when note i has no
    valid interpretation; errors[i] says why (used as retry feedback)."""
    result: list[Optional[Directive]] = [None] * n_notes
    errors: dict[int, str] = {}
    if not isinstance(items, list):
        return result, {i: "response must contain an 'interpretations' list" for i in range(n_notes)}

    seen: set[int] = set()
    for pos, item in enumerate(items):
        idx = item.get("note_index", pos) if isinstance(item, dict) else pos
        try:
            idx = _as_int(idx, "note_index")
        except GuardrailError:
            continue
        if not 0 <= idx < n_notes or idx in seen:
            continue  # unknown or duplicate mapping is ignored; missing notes are reported below
        seen.add(idx)
        try:
            result[idx] = normalize_item(item, idx, capacity_kwh)
        except GuardrailError as exc:
            errors[idx] = str(exc)
    for i in range(n_notes):
        if result[i] is None and i not in errors:
            errors[i] = "missing interpretation for this note_index"
    return result, errors


def fix_solar_meridiem(directives: list[Directive], base_solar: list[float]) -> list[Directive]:
    """Scenario-aware sanity check for the classic AM/PM slip ("from one until three" read as 1-3 AM).
    A solar_reduction that only touches hours with zero forecast solar has no effect and is almost
    certainly a 12-hour error; if the same window 12 hours later has solar, shift it there."""
    for d in directives:
        if d.directive_type != "solar_reduction" or not d.hours:
            continue
        if any(base_solar[h] > 0 for h in d.hours):
            continue
        shifted = [h + 12 for h in d.hours]
        if shifted[-1] <= 23 and any(base_solar[h] > 0 for h in shifted):
            d.hours = shifted
    return directives


def _default_explanation(d: Directive) -> str:
    span = f"hours {d.hours[0]}-{d.hours[-1]}" if d.hours else ""
    return {
        "solar_reduction": f"Usable solar reduced to {d.factor:g} of forecast for {span}.",
        "minimum_battery_reserve": f"Battery must hold at least {d.minimum_energy_kwh:g} kWh during {span}.",
        "no_charge_window": f"Battery charging is unavailable during {span}.",
        "no_discharge_window": f"Battery discharging is unavailable during {span}.",
        "max_grid_window": f"Grid import capped at {d.max_grid_kwh:g} kWh per hour during {span}.",
    }.get(d.directive_type, "This note does not affect today's 24-hour energy schedule.")
