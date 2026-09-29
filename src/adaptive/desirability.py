"""
src/adaptive/desirability.py
-----------------------------------------
How much a candidate deserves capital (Phase 25.9I-D).

THIS IS NOT AN EXPECTED RETURN, AND IT IS NOT ALLOWED TO PRETEND
--------------------------------------------------------------------
25.9I-B established what the session score actually measures:
liquidity, data completeness, abnormal volume, volatility and relative
strength — attention, not alpha — and that `expected_return` and
`model_confidence` are unavailable because no model is promoted in this
repository. Nothing in this module upgrades that. Desirability is a
preference ordering over candidates given what is known, and a caller
that reads it as a forecast is reading it wrong.

The practical consequence, and it is deliberate: with no model inputs,
desirability spreads candidates over a narrow band. The allocator that
consumes it is therefore conservative by construction rather than by a
magic number, and §30's rule holds — an unknown expected return is not
a positive expected return.

PENALTIES ARE FIRST-CLASS, NOT NEGATIVE BONUSES
---------------------------------------------------
Positives and penalties are separate maps so that a candidate's
downside is visible in the record instead of being netted into one
scalar. "Funded despite high volatility and existing sector exposure" is
a sentence the plan can produce from stored components; "score 0.62" is
not.

MISSING IS MISSING
----------------------
An input that could not be measured is named in `unavailable` and
dropped from the weighted mean, with the remaining weights renormalised.
It is never replaced by a default, because a default is a measurement
nobody took.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.domain.allocation_models import CapitalDesirability
from src.domain.adaptive_models import require_utc

#: Inputs this repository cannot supply today, each with the reason.
#: Kept as data so the diagnostics and the report cannot drift from it.
UNAVAILABLE_INPUTS: Dict[str, str] = {
    "expected_return": "no qualified predictive model is promoted",
    "model_confidence": "no qualified predictive model is promoted",
    "event_strength": "event intelligence is not wired into allocation yet "
                      "(25.9I-F)",
    "regime_compatibility": "no regime model exists",
    "spread_cost": "no spread history exists in this repository",
    "slippage_estimate": "no fitted slippage model exists",
    "market_data_cost": "per-instrument market-data cost is unconfirmed "
                        "(ticket #561227)",
}


@dataclass(frozen=True)
class DesirabilityConfig:
    """
    Weights over the inputs that exist. Not fitted to outcomes.

    Fitting these against realised returns is the lookahead the adaptive
    phases forbid, so they express an operator's preference and say so.
    """
    version: str = "desirability-v1"
    positive_weights: Dict[str, float] = field(default_factory=lambda: {
        "opportunity_rank": 0.45,
        "liquidity": 0.30,
        "data_quality": 0.25,
    })
    penalty_weights: Dict[str, float] = field(default_factory=lambda: {
        "volatility": 0.30,
        "existing_exposure": 0.25,
        "sector_crowding": 0.20,
        "risk_uncertainty": 0.25,
    })
    #: Desirability below this is not worth capital at all.
    minimum_desirability: float = 0.25
    #: Liquidity at or above this scores 1.0 (median daily dollar volume).
    liquidity_saturation_usd: float = 250_000_000.0
    #: Daily volatility at or above this is a full penalty.
    volatility_saturation: float = 0.05

    def __post_init__(self):
        for group in (self.positive_weights, self.penalty_weights):
            for name, weight in group.items():
                if weight < 0:
                    raise ValueError(f"weight {name} must be >= 0")
        if not self.positive_weights:
            raise ValueError("at least one positive weight is required")
        if not 0.0 <= self.minimum_desirability <= 1.0:
            raise ValueError("minimum_desirability must be between 0 and 1")

    def describe(self) -> Dict[str, Any]:
        return {"version": self.version,
                "positive_weights": dict(self.positive_weights),
                "penalty_weights": dict(self.penalty_weights),
                "minimum_desirability": self.minimum_desirability,
                "unavailable_inputs": sorted(UNAVAILABLE_INPUTS)}


def _bounded(value: float) -> float:
    return max(0.0, min(1.0, value))


class DesirabilityEngine:
    """
    Turns the measurable inputs into a preference, with its parts kept.

    Deterministic and free of I/O: every input is passed in, so the same
    candidate under the same config always scores the same.
    """

    def __init__(self, config: Optional[DesirabilityConfig] = None):
        self.config = config or DesirabilityConfig()

    def assess(self, instrument_id: str, *,
               opportunity_score: Optional[float] = None,
               score_completeness: Optional[float] = None,
               liquidity_notional: Optional[float] = None,
               volatility: Optional[float] = None,
               existing_exposure_fraction: Optional[float] = None,
               sector_exposure_fraction: Optional[float] = None,
               risk_is_known: bool = True) -> CapitalDesirability:
        config = self.config
        positives: Dict[str, float] = {}
        penalties: Dict[str, float] = {}
        unavailable: List[str] = list(UNAVAILABLE_INPUTS)
        notes: List[str] = []

        # ---- positives -------------------------------------------------
        if opportunity_score is None:
            unavailable.append("opportunity_rank")
        else:
            positives["opportunity_rank"] = _bounded(opportunity_score)
        if liquidity_notional is None:
            unavailable.append("liquidity")
        else:
            import math
            if liquidity_notional <= 0:
                positives["liquidity"] = 0.0
            else:
                ceiling = math.log10(config.liquidity_saturation_usd)
                floor = 5.0
                positives["liquidity"] = _bounded(
                    (math.log10(liquidity_notional) - floor) / (ceiling - floor))
        if score_completeness is None:
            unavailable.append("data_quality")
        else:
            positives["data_quality"] = _bounded(score_completeness)

        # ---- penalties -------------------------------------------------
        if volatility is None:
            unavailable.append("volatility")
        else:
            penalties["volatility"] = _bounded(volatility / config.volatility_saturation)
        if existing_exposure_fraction is None:
            unavailable.append("existing_exposure")
        else:
            penalties["existing_exposure"] = _bounded(existing_exposure_fraction)
        if sector_exposure_fraction is None:
            unavailable.append("sector_crowding")
        else:
            penalties["sector_crowding"] = _bounded(sector_exposure_fraction)

        # Unknown risk is itself a penalty, never a neutral. A candidate
        # whose downside cannot be established is less deserving of
        # capital than one whose downside is known and modest.
        penalties["risk_uncertainty"] = 0.0 if risk_is_known else 1.0
        if not risk_is_known:
            notes.append("downside could not be estimated; treated as a penalty, "
                         "never as zero risk")

        # ---- combine ---------------------------------------------------
        weights: Dict[str, float] = {}
        for name in positives:
            if name in config.positive_weights:
                weights[name] = config.positive_weights[name]
        for name in penalties:
            if name in config.penalty_weights:
                weights[name] = config.penalty_weights[name]

        positive_total = sum(config.positive_weights.get(n, 0.0) for n in positives)
        positive_value = (sum(positives[n] * config.positive_weights.get(n, 0.0)
                              for n in positives) / positive_total
                          if positive_total else 0.0)
        penalty_total = sum(config.penalty_weights.get(n, 0.0) for n in penalties)
        penalty_value = (sum(penalties[n] * config.penalty_weights.get(n, 0.0)
                             for n in penalties) / penalty_total
                         if penalty_total else 0.0)

        # Penalties scale the positive rather than subtracting from it:
        # subtraction lets a large penalty produce a negative preference,
        # which has no meaning when the answer is "how much capital".
        overall = _bounded(positive_value * (1.0 - penalty_value))

        return CapitalDesirability(
            instrument_id=instrument_id, overall=round(overall, 6),
            positives={k: round(v, 6) for k, v in positives.items()},
            penalties={k: round(v, 6) for k, v in penalties.items()},
            weights={k: round(v, 6) for k, v in weights.items()},
            unavailable=tuple(sorted(set(unavailable))),
            method_version=config.version, notes=tuple(notes))

    def meets_minimum(self, desirability: CapitalDesirability) -> bool:
        return desirability.overall >= self.config.minimum_desirability
