"""Request/response models for the GridWise API (Problem Statement §07 and §10)."""
from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Finite, non-negative number. allow_inf_nan=False rejects NaN/Infinity.
NonNeg = Annotated[float, Field(ge=0, allow_inf_nan=False)]

DIRECTIVE_TYPES = (
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
)
DirectiveType = Literal[
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
]


# ---------------------------------------------------------------- request

class HourInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hour: int = Field(ge=0, le=23)
    demand_kwh: NonNeg
    solar_kwh: NonNeg
    tariff_bdt_per_kwh: NonNeg


class BatteryInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    capacity_kwh: NonNeg
    initial_energy_kwh: NonNeg
    minimum_energy_kwh: NonNeg
    max_charge_kwh_per_hour: NonNeg
    max_discharge_kwh_per_hour: NonNeg


class OptimizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scenario_id: str = Field(min_length=1)
    operator_notes: list[str] = Field(min_length=1, max_length=3)
    hours: list[HourInput] = Field(min_length=24, max_length=24)
    battery: BatteryInput

    @field_validator("operator_notes")
    @classmethod
    def _notes_non_empty(cls, v: list[str]) -> list[str]:
        if any(not n.strip() for n in v):
            raise ValueError("operator_notes must be non-empty strings")
        return v

    @field_validator("hours")
    @classmethod
    def _hours_complete(cls, v: list[HourInput]) -> list[HourInput]:
        if sorted(h.hour for h in v) != list(range(24)):
            raise ValueError("hours must contain each hour 0..23 exactly once")
        return sorted(v, key=lambda h: h.hour)


# ---------------------------------------------------------------- response

class DirectiveInterpretation(BaseModel):
    note_index: int
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: Optional[dict]
    explanation: str

    @model_validator(mode="after")
    def _applies_semantics(self) -> "DirectiveInterpretation":
        if self.directive_type == "no_op":
            if self.applies or self.structured_adjustment is not None:
                raise ValueError("no_op requires applies=false and null adjustment")
        elif not self.applies or self.structured_adjustment is None:
            raise ValueError("non-no_op directives require applies=true and an adjustment")
        return self


class HourPlan(BaseModel):
    hour: int
    grid_kwh: float
    solar_used_kwh: float
    battery_action: Literal["charge", "discharge", "idle"]
    battery_kwh: float
    battery_energy_after_kwh: float


class OptimizeResponse(BaseModel):
    scenario_id: str
    directive_interpretation: list[DirectiveInterpretation]
    hourly_plan: list[HourPlan]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str
