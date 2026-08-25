"""
src/impact/calculations.py
-------------------------------
Deterministic numerical calculations for Market Impact (Phase 6,
spec §7, §8, §11, §12, §13, §33).

EVERY FUNCTION HERE IS PURE ARITHMETIC. No I/O, no database, no model
call — spec §33 forbids an LLM anywhere near a return, a volatility or
a z-score, and this module is where that prohibition is structurally
guaranteed: there is nothing here that could make a network call.

CONSISTENCY DISCIPLINE (spec §7): one return function, used
everywhere, with the method recorded on the result. The failure this
prevents is subtle and common — simple returns in one component, log
returns in another, silently non-comparable.

Every function returns None rather than raising or substituting a
default when its inputs are unusable. A None propagates into a
DataQuality issue upstream; a fabricated 0.0 would propagate into a
statistic and never be noticed.
"""

import math
import statistics
from decimal import Decimal
from typing import List, Optional, Sequence, Tuple

from src.domain.impact_models import ReturnMethod


def _to_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compute_return(
    price_before,
    price_after,
    method: ReturnMethod = ReturnMethod.SIMPLE,
) -> Optional[float]:
    """
    Return between two prices.

    SIMPLE: (after - before) / before
    LOG:    ln(after / before)

    Returns None for a missing, non-numeric, or non-positive starting
    price — a zero or negative price is a data error, and dividing by
    it would produce an infinity that poisons every aggregate it
    reaches.
    """
    before, after = _to_float(price_before), _to_float(price_after)
    if before is None or after is None or before <= 0:
        return None
    if method == ReturnMethod.LOG:
        if after <= 0:
            return None
        return round(math.log(after / before), 6)
    return round((after - before) / before, 6)


def compute_abnormal_return(
    asset_return: Optional[float],
    expected_return: Optional[float],
) -> Optional[float]:
    """
    ABNORMAL = ASSET - EXPECTED (spec §8).

    The name is precise and deliberately unglamorous: it says the move
    differed from the benchmark's, and NOTHING about why. Calling this
    an "event effect" would assert a causal claim the arithmetic does
    not support.
    """
    if asset_return is None or expected_return is None:
        return None
    return round(asset_return - expected_return, 6)


def expected_return_market_adjusted(benchmark_return: Optional[float]) -> Optional[float]:
    """
    Market-adjusted model: expected return IS the benchmark return
    (beta implicitly 1.0).

    That implicit beta is a real methodological assumption, stated here
    rather than buried: for a high-beta instrument it understates the
    expected move and therefore OVERSTATES the abnormal return. A
    market-model estimate (beta fitted over an estimation window) is
    the correct refinement and is deliberately not faked here.
    """
    return benchmark_return


def expected_return_mean_adjusted(estimation_returns: Sequence[float]) -> Optional[float]:
    """
    Mean-adjusted model: expected return is the instrument's own mean
    return over an estimation window that ENDS BEFORE the event.

    The caller is responsible for supplying only pre-event returns —
    and PointInTimeView is the tool that makes that enforceable rather
    than a matter of discipline.
    """
    values = [v for v in estimation_returns if v is not None]
    if len(values) < 2:
        return None
    return round(statistics.fmean(values), 6)


def cumulative_return(returns: Sequence[Optional[float]], method: ReturnMethod = ReturnMethod.SIMPLE) -> Optional[float]:
    """
    Compound a series of period returns.

    LOG returns sum; SIMPLE returns compound multiplicatively. Getting
    this backwards is a classic silent error — summing simple returns
    understates compounding — so the method is explicit, never assumed.
    """
    values = [v for v in returns if v is not None]
    if not values:
        return None
    if method == ReturnMethod.LOG:
        return round(sum(values), 6)
    total = 1.0
    for value in values:
        total *= (1.0 + value)
    return round(total - 1.0, 6)


def realized_volatility(returns: Sequence[Optional[float]], annualize: bool = False,
                         periods_per_year: int = 252) -> Optional[float]:
    """
    Standard deviation of returns (sample, not population).

    Needs at least 2 observations. Annualization is OPT-IN because
    scaling an intraday volatility by sqrt(252) produces a number that
    looks authoritative and means very little.
    """
    values = [v for v in returns if v is not None]
    if len(values) < 2:
        return None
    volatility = statistics.stdev(values)
    if annualize:
        volatility *= math.sqrt(periods_per_year)
    return round(volatility, 6)


def volatility_change_pct(pre: Optional[float], post: Optional[float]) -> Optional[float]:
    """Percentage change in volatility. None when pre-volatility is zero — the change would be undefined, not infinite."""
    if pre is None or post is None or pre == 0:
        return None
    return round((post - pre) / pre, 6)


def relative_volume(event_volume: Optional[float], baseline_mean: Optional[float]) -> Optional[float]:
    """
    Event volume as a MULTIPLE of its own baseline (spec §11).

    Normalizing against the instrument's own history is what makes the
    figure comparable across securities — 35M shares means nothing
    until you know whether the usual is 10M or 300M.
    """
    volume, baseline = _to_float(event_volume), _to_float(baseline_mean)
    if volume is None or baseline is None or baseline <= 0:
        return None
    return round(volume / baseline, 4)


def volume_zscore(event_volume: Optional[float], baseline_mean: Optional[float],
                   baseline_std: Optional[float]) -> Optional[float]:
    """
    How many standard deviations the event volume sits from its
    baseline. None when the baseline has no dispersion — a z-score
    against zero variance is undefined, not infinite.
    """
    volume, mean, std = _to_float(event_volume), _to_float(baseline_mean), _to_float(baseline_std)
    if volume is None or mean is None or std is None or std <= 0:
        return None
    return round((volume - mean) / std, 4)


def baseline_statistics(values: Sequence[Optional[float]]) -> Tuple[Optional[float], Optional[float]]:
    """(mean, sample standard deviation) of a baseline series, or (None, None) when too short."""
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return (statistics.fmean(clean), None) if clean else (None, None)
    return round(statistics.fmean(clean), 6), round(statistics.stdev(clean), 6)


def percentile(values: Sequence[float], p: float) -> Optional[float]:
    """
    The p-th percentile (0-100) by linear interpolation.

    Implemented here rather than pulled from numpy so the whole impact
    layer stays dependency-free and its arithmetic stays inspectable.
    """
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    if len(clean) == 1:
        return round(clean[0], 6)
    rank = (p / 100.0) * (len(clean) - 1)
    lower = int(math.floor(rank))
    upper = int(math.ceil(rank))
    if lower == upper:
        return round(clean[lower], 6)
    weight = rank - lower
    return round(clean[lower] * (1 - weight) + clean[upper] * weight, 6)


def gap_return(previous_close, next_open, method: ReturnMethod = ReturnMethod.SIMPLE) -> Optional[float]:
    """Overnight gap: previous close -> next open (spec §13)."""
    return compute_return(previous_close, next_open, method)


def t_statistic(values: Sequence[float]) -> Optional[float]:
    """
    One-sample t-statistic against a null of zero mean.

    Deliberately returns the STATISTIC only, never a p-value or a
    "significant: yes/no" verdict. Event-study returns are not
    independent and rarely normal; producing a p-value from this would
    dress up an assumption-violating test as a result (spec §21).
    """
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return None
    mean = statistics.fmean(clean)
    std = statistics.stdev(clean)
    if std == 0:
        return None
    return round(mean / (std / math.sqrt(len(clean))), 4)
