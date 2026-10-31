"""
src/adaptive/correlation.py
----------------------------------------
As-of correlation between candidates (Phase 25.9I-D).

WHAT IT IS FOR, AND WHAT IT IS NOT FOR
------------------------------------------
Universe-selection and allocation redundancy: noticing that two
candidates are the same bet wearing different tickers. It is NOT the
portfolio risk engine, and it does not decide anything by itself. The
canonical Risk engine remains the hard gate.

MISSING CORRELATION IS NOT ZERO CORRELATION
-----------------------------------------------
`correlation()` returns None when the overlapping sample is shorter than
`min_observations`, and the allocator treats None as "unknown", never as
"uncorrelated". Zero would be the most dangerous possible default: it
says these positions diversify each other, which is exactly the claim
the missing data cannot support.

AS-OF BOUNDED, LIKE EVERY OTHER HISTORICAL READ
---------------------------------------------------
Returns are computed from `MarketHistory`, which opens the database
read-only and bounds every query at the decision timestamp. A
correlation that quietly used tomorrow's bars would make a portfolio
look diversified using information the portfolio did not have.

THE THRESHOLD IS OFF BY DEFAULT
-----------------------------------
`max_pairwise_correlation` defaults to None. This repository has no
evidence for a particular cut-off, and §15 is explicit that a threshold
must not be invented to look decisive. Correlation is measured and
reported; whether it constrains anything is an operator's decision.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from src.adaptive.universe import MarketHistory

#: Below this many overlapping daily returns a correlation is noise.
DEFAULT_MIN_OBSERVATIONS = 30


@dataclass(frozen=True)
class CorrelationResult:
    """One pair, measured or explicitly not."""
    a: str
    b: str
    value: Optional[float]
    observations: int
    reason: str = ""

    @property
    def is_measured(self) -> bool:
        return self.value is not None


class AsOfCorrelationProvider:
    """
    Pairwise correlation of daily returns, bounded at `as_of`.

    Deterministic and cached per pair, so a plan computed twice from the
    same history produces identical numbers.
    """

    def __init__(self, history: MarketHistory, *,
                 window: int = 120,
                 min_observations: int = DEFAULT_MIN_OBSERVATIONS):
        if window < 3:
            raise ValueError("window must be >= 3")
        if min_observations < 3:
            raise ValueError("min_observations must be >= 3")
        self.history = history
        self.window = window
        self.min_observations = min_observations
        self._returns: Dict[str, Optional[List[Tuple[str, float]]]] = {}
        self._pairs: Dict[Tuple[str, str], CorrelationResult] = {}

    def _series(self, instrument_id: str) -> Optional[List[Tuple[str, float]]]:
        """Dated daily returns, newest last. None when too short."""
        if instrument_id in self._returns:
            return self._returns[instrument_id]
        candles = self.history.history(instrument_id)
        stamps, closes = candles.timestamps[-(self.window + 1):], \
            candles.closes[-(self.window + 1):]
        series: Optional[List[Tuple[str, float]]] = None
        if len(closes) >= 2 and len(stamps) == len(closes):
            series = [(stamps[i], (closes[i] / closes[i - 1]) - 1.0)
                      for i in range(1, len(closes)) if closes[i - 1]]
        self._returns[instrument_id] = series
        return series

    def correlation(self, a: str, b: str) -> CorrelationResult:
        key = (a, b) if a <= b else (b, a)
        if key in self._pairs:
            return self._pairs[key]

        left, right = self._series(key[0]), self._series(key[1])
        if not left or not right:
            result = CorrelationResult(key[0], key[1], None, 0,
                                       "no return series at as_of")
        else:
            by_date = dict(right)
            paired = [(value, by_date[stamp]) for stamp, value in left
                      if stamp in by_date]
            if len(paired) < self.min_observations:
                result = CorrelationResult(
                    key[0], key[1], None, len(paired),
                    f"only {len(paired)} overlapping observations, below the "
                    f"minimum of {self.min_observations}")
            else:
                xs = [p[0] for p in paired]
                ys = [p[1] for p in paired]
                try:
                    value = statistics.correlation(xs, ys)
                except (statistics.StatisticsError, ValueError):
                    result = CorrelationResult(
                        key[0], key[1], None, len(paired),
                        "a series had zero variance over the window")
                else:
                    result = CorrelationResult(key[0], key[1], round(value, 6),
                                               len(paired))
        self._pairs[key] = result
        return result

    def max_correlation_with(self, candidate: str, others: Iterable[str]
                             ) -> CorrelationResult:
        """
        The strongest measured correlation against a set. Unknowns are
        reported rather than skipped silently: a candidate whose
        correlations are all unmeasured is not a diversified candidate,
        it is an unmeasured one.
        """
        best: Optional[CorrelationResult] = None
        unmeasured = 0
        for other in others:
            if other == candidate:
                continue
            result = self.correlation(candidate, other)
            if not result.is_measured:
                unmeasured += 1
                continue
            if best is None or result.value > best.value:
                best = result
        if best is not None:
            return best
        return CorrelationResult(
            candidate, "", None, 0,
            f"no measurable correlation against {unmeasured} holding(s)"
            if unmeasured else "no other positions to correlate against")
