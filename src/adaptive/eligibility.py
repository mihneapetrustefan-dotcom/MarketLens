"""
src/adaptive/eligibility.py
----------------------------------------
May this instrument be evaluated today? (Phase 25.9I-B)

THE RULE THAT MATTERS
-------------------------
UNKNOWN IS NOT GOOD. Every filter can return three answers, not two:
the instrument qualifies, it does not, or there was not enough data to
say. A system that folds the third into the first admits whatever it
failed to measure, and the failure is invisible precisely because
nothing was measured.

WHAT THIS CHECKS, AND WHY EACH ONE
--------------------------------------
Only properties this repository can actually establish from its own
data at `as_of`:

  security type / exchange / currency   the capture and execution paths
                                        support US-listed equities; a
                                        member that is not one cannot be
                                        acted on later
  candle coverage                       a score computed from four bars
                                        is not a score
  data staleness                        the research cache is refreshed
                                        in batches; a name whose newest
                                        candle is weeks old is being
                                        ranked on history, and the
                                        filter says so rather than
                                        letting it compete unnoticed
  median dollar volume                  liquidity, in the only form this
                                        project measures
  last price                            a penny-stock sanity bound
  sector metadata                       concentration limits are
                                        meaningless without it

WHAT IS DELIBERATELY ABSENT
-------------------------------
Spread quality, borrow availability, corporate-action status and trading
halts. This repository has no spread history, no borrow feed and no
halt record; inventing thresholds against fields that do not exist
would produce filters that always pass. They are named in the config as
unsupported so the gap is visible rather than forgotten.

DEFAULTS ARE PERMISSIVE ON PURPOSE
--------------------------------------
`max_data_age_days` defaults to None. As this was written the research
cache ended 2026-08-27 while the calendar said 2026-09-28, so any
staleness bound would have marked the entire universe
INSUFFICIENT_DATA. The honest behaviour is to report the age and let
the operator set the bound, not to hard-code a number that makes the
output look healthy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from src.adaptive.universe import InstrumentHistory, ParentUniverseMember
from src.domain.adaptive_models import (
    EligibilityDecision, EligibilityStatus, require_utc,
)

#: Filters this repository cannot support today, named so the absence is
#: explicit. Adding one means adding its data source first.
UNSUPPORTED_FILTERS = (
    "spread_quality", "borrow_availability", "halt_status",
    "corporate_action_validity",
)


@dataclass(frozen=True)
class EligibilityConfig:
    """
    Thresholds, all configurable, none invented from performance data.

    `version` travels with every decision so a replay can prove which
    thresholds produced it.
    """
    version: str = "eligibility-v1"
    min_daily_candles: int = 60
    min_median_dollar_volume: float = 1_000_000.0
    min_last_price: float = 3.0
    max_data_age_days: Optional[float] = None
    require_sector_metadata: bool = True
    supported_sec_types: Tuple[str, ...] = ("STK",)
    supported_currencies: Tuple[str, ...] = ("USD",)
    supported_asset_classes: Tuple[str, ...] = ("stock",)

    def __post_init__(self):
        for name in ("min_daily_candles", "min_median_dollar_volume",
                     "min_last_price"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        if self.max_data_age_days is not None and self.max_data_age_days <= 0:
            raise ValueError("max_data_age_days must be positive when set")

    def describe(self) -> Dict[str, object]:
        return {"version": self.version,
                "min_daily_candles": self.min_daily_candles,
                "min_median_dollar_volume": self.min_median_dollar_volume,
                "min_last_price": self.min_last_price,
                "max_data_age_days": self.max_data_age_days,
                "unsupported_filters": list(UNSUPPORTED_FILTERS)}


class EligibilityEvaluator:
    """Judges one member at one `as_of`. Deterministic; holds no clock."""

    def __init__(self, config: Optional[EligibilityConfig] = None):
        self.config = config or EligibilityConfig()

    def evaluate(self, member: ParentUniverseMember, history: InstrumentHistory,
                 as_of: datetime) -> EligibilityDecision:
        as_of = require_utc(as_of, "as_of")
        config = self.config
        reasons: List[str] = []
        unknown: List[str] = []

        measurements: Dict[str, object] = {
            "daily_candles": history.count,
            "data_age_days": history.data_age_days(),
            "last_close": history.last_close(),
            "median_dollar_volume": history.median_dollar_volume(),
            "sector_id": member.sector_id or None,
        }

        # --- static properties, known from the artifact ---------------
        if member.sec_type not in config.supported_sec_types:
            reasons.append(f"security type {member.sec_type!r} is not supported")
        if member.currency not in config.supported_currencies:
            reasons.append(f"currency {member.currency!r} is not supported")
        if member.asset_class not in config.supported_asset_classes:
            reasons.append(f"asset class {member.asset_class!r} is not supported")
        if config.require_sector_metadata and not member.sector_id:
            reasons.append("no sector metadata, so concentration limits cannot apply")

        # --- measured properties --------------------------------------
        if history.count == 0:
            unknown.append("no daily candles at or before as_of")
        elif history.count < config.min_daily_candles:
            reasons.append(f"{history.count} daily candles, below the minimum of "
                           f"{config.min_daily_candles}")

        age = history.data_age_days()
        if age is None:
            unknown.append("data age unknown: no candle to measure it from")
        elif config.max_data_age_days is not None and age > config.max_data_age_days:
            reasons.append(f"newest candle is {age:.1f} days old at as_of, above "
                           f"the limit of {config.max_data_age_days}")

        dollar_volume = history.median_dollar_volume()
        if dollar_volume is None:
            unknown.append("median dollar volume could not be measured")
        elif dollar_volume < config.min_median_dollar_volume:
            reasons.append(f"median dollar volume {dollar_volume:,.0f} is below "
                           f"{config.min_median_dollar_volume:,.0f}")

        last_close = history.last_close()
        if last_close is None:
            unknown.append("no close price at or before as_of")
        elif last_close < config.min_last_price:
            reasons.append(f"last close {last_close} is below the minimum price "
                           f"of {config.min_last_price}")

        # A hard failure outranks missing data: an instrument that is the
        # wrong security type stays ineligible whatever else is unknown.
        if reasons:
            status = EligibilityStatus.INELIGIBLE
        elif unknown:
            status = EligibilityStatus.INSUFFICIENT_DATA
            reasons = unknown
        else:
            status = EligibilityStatus.ELIGIBLE

        return EligibilityDecision(
            instrument_id=member.instrument_id, status=status, as_of=as_of,
            reasons=tuple(reasons), measurements=measurements)


def summarize(decisions: Sequence[EligibilityDecision]) -> Dict[str, int]:
    """Counts by status, with every status present even at zero."""
    counts = {status.value: 0 for status in EligibilityStatus}
    for decision in decisions:
        counts[decision.status.value] += 1
    return counts
