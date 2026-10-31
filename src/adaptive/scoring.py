"""
src/adaptive/scoring.py
------------------------------------
Why an instrument might deserve a session slot (Phase 25.9I-B).

WHAT THIS SCORE IS, STATED PLAINLY
--------------------------------------
It is an ATTENTION score, not an alpha forecast. It ranks which
instruments are worth watching with the data this project actually has:
liquidity, recent volatility, unusual volume, relative strength against
the benchmark, and data completeness. None of that predicts returns, and
this module does not claim it does.

WHY NOT AN EXPECTED-RETURN COMPONENT
----------------------------------------
Because there is no qualified model to produce one. The repository has
two trained models, two evaluations, five signals and no promotion
record. §23 of the phase brief is explicit that a heuristic must not be
dressed as a qualified alpha model, so `expected_return` and
`model_confidence` are declared UNAVAILABLE rather than defaulted to
zero. The difference matters: 0.0 reads as "the model sees no edge",
while unavailable reads as "there is no model", and only one of those
is true.

MISSING IS NOT ZERO, ANYWHERE
---------------------------------
A component that cannot be measured is dropped from the weighted mean
and named in `unavailable`. The weights of the remaining components are
renormalised, and `completeness` reports how much of the intended score
was actually computed. An instrument scored on two of six components is
visibly a partial score rather than a low one.

DETERMINISM
---------------
Same history, same config, same `as_of` gives the same score, to the
last decimal. Every component is a pure function of candles at or
before `as_of`; there is no clock, no randomness and no I/O here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from src.adaptive.universe import InstrumentHistory, ParentUniverseMember
from src.domain.adaptive_models import OpportunityScore, require_utc

#: Components this project cannot compute today, with the reason. Named
#: so that "we have no model" stays visible instead of becoming a zero.
UNAVAILABLE_COMPONENTS: Dict[str, str] = {
    "expected_return": "no qualified predictive model is deployed",
    "model_confidence": "no qualified predictive model is deployed",
    "event_relevance": "event intelligence is not wired into selection yet",
    "spread_quality": "no spread history exists in this repository",
    "execution_cost": "no execution-cost model is fitted",
    "market_data_cost": "per-instrument market-data cost is unconfirmed "
                        "(ticket #561227)",
    "correlation_context": "deferred: see 25.9I-B docs, redundancy control",
}


@dataclass(frozen=True)
class ScoringConfig:
    """
    Weights and windows. Plain, documented, and not fitted to outcomes.

    The weights express what the operator wants attention spent on. They
    are not calibrated against returns, because calibrating them against
    returns is exactly the lookahead this phase forbids.
    """
    version: str = "attention-v1"
    weights: Dict[str, float] = field(default_factory=lambda: {
        "liquidity": 0.35,
        "data_completeness": 0.20,
        "abnormal_volume": 0.20,
        "volatility": 0.15,
        "relative_strength": 0.10,
    })
    volatility_window: int = 20
    momentum_window: int = 20
    liquidity_window: int = 60
    volume_recent: int = 5
    #: Dollar volume at or above this maps to a liquidity score of 1.0.
    liquidity_saturation_usd: float = 500_000_000.0
    #: Daily stdev at or above this maps to a volatility score of 1.0.
    volatility_saturation: float = 0.05
    benchmark_instrument_id: str = "benchmark-spy"

    def __post_init__(self):
        for name, weight in self.weights.items():
            if weight < 0:
                raise ValueError(f"weight {name} must be >= 0")
        if not self.weights:
            raise ValueError("at least one weight is required")
        if sum(self.weights.values()) <= 0:
            raise ValueError("weights must not sum to zero")
        for name in ("volatility_window", "momentum_window", "liquidity_window",
                     "volume_recent"):
            if getattr(self, name) < 2:
                raise ValueError(f"{name} must be >= 2")

    def describe(self) -> Dict[str, object]:
        return {"version": self.version, "weights": dict(self.weights),
                "unavailable_components": sorted(UNAVAILABLE_COMPONENTS)}


def _bounded(value: float) -> float:
    return max(0.0, min(1.0, value))


class SessionScorer:
    """
    Turns history into an attention score with its components intact.

    The benchmark history is optional: without it `relative_strength`
    becomes unavailable rather than being computed against nothing.
    """

    def __init__(self, config: Optional[ScoringConfig] = None):
        self.config = config or ScoringConfig()

    # ---------------- components ----------------

    def _liquidity(self, history: InstrumentHistory) -> Optional[float]:
        value = history.median_dollar_volume(self.config.liquidity_window)
        if value is None or value <= 0:
            return None
        # Log scale: the gap between 1M and 10M matters more than the gap
        # between 400M and 500M, and a linear scale would put every large
        # cap at the ceiling together.
        ceiling = math.log10(self.config.liquidity_saturation_usd)
        floor = 5.0                                    # 100k USD a day
        return _bounded((math.log10(value) - floor) / (ceiling - floor))

    def _volatility(self, history: InstrumentHistory) -> Optional[float]:
        value = history.realized_volatility(self.config.volatility_window)
        if value is None:
            return None
        return _bounded(value / self.config.volatility_saturation)

    def _abnormal_volume(self, history: InstrumentHistory) -> Optional[float]:
        ratio = history.volume_ratio(self.config.volume_recent,
                                     self.config.liquidity_window)
        if ratio is None:
            return None
        # 1.0 is ordinary, 3.0 or more is as remarkable as this scores.
        return _bounded((ratio - 1.0) / 2.0)

    def _relative_strength(self, history: InstrumentHistory,
                           benchmark: Optional[InstrumentHistory]
                           ) -> Optional[float]:
        if benchmark is None:
            return None
        own = history.trailing_return(self.config.momentum_window)
        market = benchmark.trailing_return(self.config.momentum_window)
        if own is None or market is None:
            return None
        # Excess return of +/-10% spans the range; the midpoint is "kept
        # pace with the market", which is deliberately unremarkable.
        return _bounded(0.5 + (own - market) / 0.2)

    def _data_completeness(self, history: InstrumentHistory) -> Optional[float]:
        if history.count == 0:
            return None
        target = max(self.config.liquidity_window, self.config.volatility_window)
        return _bounded(history.count / float(target))

    # ---------------- the score ----------------

    def score(self, member: ParentUniverseMember, history: InstrumentHistory,
              as_of: datetime, *, benchmark: Optional[InstrumentHistory] = None
              ) -> OpportunityScore:
        require_utc(as_of, "as_of")
        measured: Dict[str, float] = {}
        unavailable: List[str] = list(UNAVAILABLE_COMPONENTS)

        for name, value in (
                ("liquidity", self._liquidity(history)),
                ("volatility", self._volatility(history)),
                ("abnormal_volume", self._abnormal_volume(history)),
                ("relative_strength", self._relative_strength(history, benchmark)),
                ("data_completeness", self._data_completeness(history))):
            if value is None:
                unavailable.append(name)
            else:
                measured[name] = round(value, 6)

        # Renormalise over what was measured. An instrument missing half
        # its components is not penalised into a low score -- it gets a
        # score over what exists, and `completeness` says how much that is.
        weights = {name: self.config.weights[name] for name in measured
                   if name in self.config.weights}
        total_weight = sum(weights.values())
        if total_weight > 0:
            overall = sum(measured[name] * weight
                          for name, weight in weights.items()) / total_weight
        else:
            overall = 0.0

        return OpportunityScore(
            overall=round(overall, 6), components=measured,
            method_version=self.config.version,
            weights={name: round(w / total_weight, 6) for name, w in weights.items()}
            if total_weight else {},
            unavailable=tuple(sorted(unavailable)))


def explain(score: OpportunityScore) -> List[str]:
    """Per-component contribution, largest first. For the CLI and reports."""
    lines = []
    for name, value in sorted(score.components.items(),
                              key=lambda kv: -kv[1] * score.weights.get(kv[0], 0)):
        weight = score.weights.get(name, 0.0)
        lines.append(f"{name}: {value:.4f} x {weight:.3f} = "
                     f"{value * weight:.4f}")
    if score.unavailable:
        lines.append("unavailable: " + ", ".join(score.unavailable))
    return lines
