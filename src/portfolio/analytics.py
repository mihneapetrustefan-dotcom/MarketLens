"""
src/portfolio/analytics.py
-------------------------------
Portfolio risk measurement (Phase 11, spec §9–§15).

EVERY NUMBER HERE IS A MEASUREMENT OF THE PAST
--------------------------------------------------
Nothing in this module forecasts. Volatility, VaR and Expected
Shortfall are computed from realized returns already stored in the
candle cache; they describe how this portfolio WOULD HAVE behaved over
a past window, which is a different claim from how it will behave.
Spec §11 and §58 both insist on that distinction, and every estimate
carries its method and its observation count so a reader can judge how
much weight it deserves.

THE CURRENT-WEIGHTS APPROXIMATION, STATED PLAINLY
-----------------------------------------------------
This system does not have a history of past positions — the portfolio
tables are new and start empty. So portfolio-level volatility and VaR
are computed by applying TODAY's weights to each instrument's PAST
returns ("historical simulation on current weights"), the standard
method when position history is unavailable.

What that answers: "how volatile would this book have been, had it
been held unchanged through that window?"
What it does NOT answer: "how volatile was this book?" — nobody held
it then.

The method string on every estimate says which one it is, so the
weaker claim cannot be quoted as the stronger one.

DRAWDOWN IS THE EXCEPTION, AND DELIBERATELY SO
--------------------------------------------------
Spec §12 forbids fabricating historical equity curves. A drawdown
computed from a simulated curve would be exactly that fabrication, so
this module refuses: drawdown comes ONLY from real stored snapshots of
this portfolio's own equity. With no snapshot history it reports
insufficient_data rather than a synthesized number. That is why
drawdown will be empty on a new portfolio while volatility is not —
the two rest on different evidence.

ASYNCHRONOUS CALENDARS ARE INTERSECTED, NOT FILLED
------------------------------------------------------
Crypto trades every day; equities do not. Combining them requires a
common calendar, and there are two ways to get one: forward-fill the
gaps, or intersect the dates. Forward-filling invents observations
(a "0% return" on a day the market was shut is not something that
happened) and biases volatility downward. So this module intersects,
and reports the resulting observation count — a thin intersection is
then visible as thin, instead of being padded up to a comfortable-
looking sample size.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from src.domain.portfolio_models import (
    ConcentrationMetrics, CorrelationSummary, DrawdownMetrics, PortfolioSnapshot,
    ValueAtRisk, VolatilityEstimate, finite_or_none, safe_ratio,
)

#: Trading days per year — the standard annualization convention, the
#: same one the legacy RiskScoreCalculator already uses.
TRADING_DAYS_PER_YEAR = 252

#: Below this many return observations, a volatility figure is noise
#: dressed as a measurement. Reported as insufficient rather than
#: computed anyway.
MIN_VOLATILITY_OBSERVATIONS = 20

#: Correlation needs more data than volatility: a Pearson coefficient
#: over a handful of points swings wildly and reads as authoritative.
MIN_CORRELATION_OBSERVATIONS = 30

#: Historical VaR reads a tail quantile. With fewer than this many
#: observations the 5th percentile is one or two data points, which is
#: not a distribution.
MIN_VAR_OBSERVATIONS = 60

#: Pairs at or above this are reported as a concentration warning.
HIGH_CORRELATION_THRESHOLD = 0.80


# ============================================================
# Small statistics helpers (pure Python, matching house style)
# ============================================================

def mean(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return finite_or_none(sum(values) / len(values))


def sample_stdev(values: Sequence[float]) -> Optional[float]:
    """
    Sample standard deviation (n-1).

    n-1 rather than n because these are samples of an unknown process,
    not a complete population — the same convention risk_score.py
    already applies.
    """
    if len(values) < 2:
        return None
    average = mean(values)
    if average is None:
        return None
    variance = sum((v - average) ** 2 for v in values) / (len(values) - 1)
    return finite_or_none(math.sqrt(variance))


def pearson_correlation(left: Sequence[float], right: Sequence[float]) -> Optional[float]:
    """
    Pearson correlation, or None when it is undefined.

    A series with zero variance (a constant) has no correlation with
    anything — the denominator is zero. Returning None rather than 0.0
    matters: 0.0 would read as "independent", which is a claim, while
    None reads as "not measurable", which is the truth.
    """
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean, right_mean = mean(left), mean(right)
    if left_mean is None or right_mean is None:
        return None
    covariance = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_ss = sum((a - left_mean) ** 2 for a in left)
    right_ss = sum((b - right_mean) ** 2 for b in right)
    if left_ss <= 0 or right_ss <= 0:
        return None
    return finite_or_none(covariance / math.sqrt(left_ss * right_ss))


def percentile(sorted_values: Sequence[float], fraction: float) -> Optional[float]:
    """
    Linear-interpolated percentile over an ASCENDING sequence.

    Interpolated rather than nearest-rank so the estimate moves
    smoothly as one observation is added, instead of stepping.
    """
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return finite_or_none(sorted_values[0])
    position = max(0.0, min(1.0, fraction)) * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return finite_or_none(sorted_values[int(position)])
    weight = position - lower
    return finite_or_none(
        sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight)


# ============================================================
# Aligning asynchronous series
# ============================================================

def align_return_series(
    series_by_instrument: Dict[str, List[Tuple[datetime, float]]],
) -> Tuple[List[datetime], Dict[str, List[float]]]:
    """
    Reduce per-instrument return series to a common set of dates.

    Alignment is on the calendar DATE, not the exact timestamp: cached
    candles for different asset classes carry different intraday
    stamps (equities at 04:00Z, crypto at 00:00Z), so comparing raw
    timestamps would find almost no overlap between a stock and a coin
    that both traded the same day.

    Returns the shared dates (ascending) and one aligned value list per
    instrument. An empty intersection returns empty structures — the
    caller reports insufficient data rather than receiving a fabricated
    overlap.
    """
    if not series_by_instrument:
        return [], {}

    by_date: Dict[str, Dict[str, float]] = {}
    for instrument_id, points in series_by_instrument.items():
        for timestamp, value in points:
            by_date.setdefault(instrument_id, {})[timestamp.date().isoformat()] = value

    if not by_date:
        return [], {}

    common: Optional[set] = None
    for dates in by_date.values():
        keys = set(dates.keys())
        common = keys if common is None else (common & keys)
    if not common:
        return [], {}

    ordered = sorted(common)
    aligned = {instrument_id: [by_date[instrument_id][d] for d in ordered]
               for instrument_id in by_date}
    return [datetime.fromisoformat(d) for d in ordered], aligned


def portfolio_return_series(
    weights: Dict[str, float],
    series_by_instrument: Dict[str, List[Tuple[datetime, float]]],
) -> Tuple[List[float], int]:
    """
    Combine instrument returns into a portfolio return series using
    SIGNED weights, so a short contributes with the opposite sign.

    Weights are normalized across the instruments that actually have
    return data. Without that, a book where half the positions lack
    history would look artificially calm — the missing half would
    silently contribute zero variance while still occupying weight.
    Normalizing states the alternative honestly: this is the volatility
    of the part that could be measured.

    Returns (returns, observation_count).
    """
    if not weights or not series_by_instrument:
        return [], 0

    usable = {i: w for i, w in weights.items()
              if i in series_by_instrument and w is not None and finite_or_none(w) is not None}
    if not usable:
        return [], 0

    _, aligned = align_return_series(
        {i: series_by_instrument[i] for i in usable})
    if not aligned:
        return [], 0

    total = sum(abs(w) for w in usable.values())
    if total <= 0:
        return [], 0
    normalized = {i: w / total for i, w in usable.items()}

    length = min(len(v) for v in aligned.values())
    returns: List[float] = []
    for index in range(length):
        value = sum(normalized[i] * aligned[i][index] for i in aligned)
        finite = finite_or_none(value)
        if finite is not None:
            returns.append(finite)
    return returns, len(returns)


# ============================================================
# Metrics
# ============================================================

def compute_volatility(returns: Sequence[float], lookback_days: int,
                       method: str = "historical_current_weights") -> VolatilityEstimate:
    """Annualized standard deviation of a return series."""
    estimate = VolatilityEstimate(
        method=method, lookback_days=lookback_days,
        observations=len(returns), return_frequency="daily",
        annualization_factor=math.sqrt(TRADING_DAYS_PER_YEAR))

    if len(returns) < MIN_VOLATILITY_OBSERVATIONS:
        estimate.insufficient_data = True
        estimate.note = (f"{len(returns)} observations, "
                         f"minimum {MIN_VOLATILITY_OBSERVATIONS}")
        return estimate

    daily = sample_stdev(returns)
    if daily is None:
        estimate.insufficient_data = True
        estimate.note = "return series has no measurable dispersion"
        return estimate

    estimate.value = finite_or_none(daily * math.sqrt(TRADING_DAYS_PER_YEAR))
    if estimate.value is None:
        estimate.insufficient_data = True
        estimate.note = "volatility was not finite"
    return estimate


def compute_value_at_risk(returns: Sequence[float], confidence_level: float = 0.95,
                          horizon_days: int = 1) -> ValueAtRisk:
    """
    Historical VaR and Expected Shortfall.

    VaR is the loss the portfolio would have exceeded on
    (1 - confidence) of past days; ES is the AVERAGE loss on those
    days. Both are returned as positive fractions of equity, so 0.031
    means "3.1% of equity".

    Reporting ES alongside VaR is not decoration: VaR says nothing
    whatsoever about how bad the tail gets beyond the threshold, and
    two portfolios with identical VaR can have very different worst
    days. Spec §14 asks for the distinction to be explicit.
    """
    result = ValueAtRisk(confidence_level=confidence_level,
                         horizon_days=horizon_days, method="historical",
                         observations=len(returns))

    if len(returns) < MIN_VAR_OBSERVATIONS:
        result.insufficient_data = True
        result.note = (f"{len(returns)} observations, "
                       f"minimum {MIN_VAR_OBSERVATIONS} for a tail estimate")
        return result

    ordered = sorted(returns)
    threshold = percentile(ordered, 1.0 - confidence_level)
    if threshold is None:
        result.insufficient_data = True
        result.note = "quantile could not be computed"
        return result

    # Scaled by sqrt(horizon) — the standard convention, and an
    # approximation that assumes returns are serially independent.
    # Stated here rather than buried, since it is exactly the kind of
    # assumption that silently inflates confidence.
    scale = math.sqrt(horizon_days) if horizon_days > 1 else 1.0

    # A positive threshold would mean even the tail day was a gain;
    # reporting negative "risk" would be nonsense, so it floors at 0.
    result.value = finite_or_none(max(0.0, -threshold) * scale)

    tail = [r for r in ordered if r <= threshold]
    tail_mean = mean(tail)
    if tail_mean is not None:
        result.expected_shortfall = finite_or_none(max(0.0, -tail_mean) * scale)

    if horizon_days > 1:
        result.note = (f"scaled from 1 day by sqrt({horizon_days}); "
                       f"assumes serially independent returns")
    return result


def compute_drawdown(equity_curve: Sequence[Tuple[datetime, float]]) -> DrawdownMetrics:
    """
    Peak-to-trough decline of a REAL equity curve.

    The caller must pass observed equity values from stored snapshots.
    This function does not synthesize a curve and must never be given a
    simulated one (spec §12) — with fewer than two real observations it
    reports insufficient_data.
    """
    points = [(t, v) for t, v in equity_curve
              if v is not None and finite_or_none(v) is not None]
    metrics = DrawdownMetrics(observations=len(points))

    if len(points) < 2:
        metrics.insufficient_data = True
        return metrics

    points = sorted(points, key=lambda p: p[0])
    peak_value, peak_at = points[0][1], points[0][0]
    max_drawdown = 0.0
    trough_value: Optional[float] = None
    trough_at: Optional[datetime] = None
    best_peak_value, best_peak_at = peak_value, peak_at

    for timestamp, value in points:
        if value > peak_value:
            peak_value, peak_at = value, timestamp
        if peak_value > 0:
            decline = (value - peak_value) / peak_value
            if decline < max_drawdown:
                max_drawdown = decline
                trough_value, trough_at = value, timestamp
                best_peak_value, best_peak_at = peak_value, peak_at

    final_value = points[-1][1]
    current = ((final_value - peak_value) / peak_value) if peak_value > 0 else None

    metrics.max_drawdown = finite_or_none(max_drawdown)
    metrics.current_drawdown = finite_or_none(current)
    metrics.peak_equity = finite_or_none(best_peak_value)
    metrics.peak_at = best_peak_at
    metrics.trough_equity = finite_or_none(trough_value)
    metrics.trough_at = trough_at
    return metrics


def compute_concentration(snapshot: PortfolioSnapshot) -> ConcentrationMetrics:
    """
    How few places the portfolio's risk sits in.

    Weights are absolute-exposure weights over equity, so a short
    counts as concentration rather than offsetting a long. HHI is
    reported on the 0..1 scale together with its reciprocal, the
    "effective number of positions" — 1/HHI is far easier to read than
    the index itself (0.25 means "this is really a 4-position book").
    """
    metrics = ConcentrationMetrics(position_count=len(snapshot.valuations))
    equity = snapshot.equity
    if not snapshot.valuations or equity <= 0:
        return metrics

    by_instrument: Dict[str, float] = {}
    for valuation in snapshot.valuations:
        exposure = valuation.exposure
        if exposure is None:
            continue
        key = valuation.position.instrument_id
        by_instrument[key] = by_instrument.get(key, 0.0) + exposure

    weights = []
    for instrument_id, exposure in by_instrument.items():
        weight = safe_ratio(exposure, equity)
        if weight is not None:
            weights.append((instrument_id, weight))
    if not weights:
        return metrics

    weights.sort(key=lambda pair: pair[1], reverse=True)
    metrics.position_count = len(weights)
    metrics.largest_instrument_id, metrics.largest_weight = weights[0]
    metrics.top_5_weight = finite_or_none(sum(w for _, w in weights[:5]))
    metrics.top_10_weight = finite_or_none(sum(w for _, w in weights[:10]))

    # Against equity: cash lowers it, which is what a risk limit wants.
    metrics.hhi = finite_or_none(sum(w * w for _, w in weights))

    invested = sum(w for _, w in weights)
    metrics.invested_weight = finite_or_none(invested)

    # Against the invested portion only, so 1/HHI keeps meaning "how
    # many positions this really is" rather than counting cash as
    # additional breadth. See ConcentrationMetrics for why these are
    # deliberately two different denominators.
    if invested > 0:
        normalized_hhi = finite_or_none(
            sum((w / invested) ** 2 for _, w in weights))
        if normalized_hhi is not None and normalized_hhi > 0:
            metrics.effective_positions = finite_or_none(1.0 / normalized_hhi)
    return metrics


def compute_correlation_summary(
    series_by_instrument: Dict[str, List[Tuple[datetime, float]]],
    min_observations: int = MIN_CORRELATION_OBSERVATIONS,
    high_threshold: float = HIGH_CORRELATION_THRESHOLD,
) -> CorrelationSummary:
    """
    Pairwise correlation across held instruments, summarized.

    Each pair is aligned on its OWN shared dates rather than on a
    single portfolio-wide intersection: two instruments with long
    histories should not lose their comparability because a third
    holding was added last week.
    """
    summary = CorrelationSummary()
    instruments = sorted(series_by_instrument)
    if len(instruments) < 2:
        return summary

    coefficients: List[float] = []
    observation_counts: List[int] = []

    for index, left in enumerate(instruments):
        for right in instruments[index + 1:]:
            _, aligned = align_return_series({
                left: series_by_instrument[left],
                right: series_by_instrument[right],
            })
            if len(aligned) < 2:
                summary.insufficient_pairs += 1
                continue
            left_values, right_values = aligned[left], aligned[right]
            if len(left_values) < min_observations:
                summary.insufficient_pairs += 1
                continue

            coefficient = pearson_correlation(left_values, right_values)
            if coefficient is None:
                summary.insufficient_pairs += 1
                continue

            coefficients.append(coefficient)
            observation_counts.append(len(left_values))
            summary.computed_pairs += 1
            if coefficient >= high_threshold:
                summary.highly_correlated_pairs.append(
                    (left, right, round(coefficient, 4)))

    if coefficients:
        summary.average_correlation = finite_or_none(mean(coefficients))
        peak = max(coefficients)
        summary.max_correlation = finite_or_none(peak)
        summary.max_pair = None
        for (left, right, value) in summary.highly_correlated_pairs:
            if value == round(peak, 4):
                summary.max_pair = (left, right)
                break
        summary.min_observations_used = min(observation_counts)

    summary.highly_correlated_pairs.sort(key=lambda triple: triple[2], reverse=True)
    return summary


def compute_liquidity_participation(
    quantity: float,
    price_points: Sequence,
    lookback: int = 20,
) -> Optional[float]:
    """
    Position size as a fraction of average daily VOLUME (spec §15).

    Answers "how much of a normal day's trading is this position?" —
    0.25 means a quarter of a typical day's volume, which is a lot.
    Returns None when volume is missing rather than assuming the
    instrument is liquid, because assuming liquidity is precisely the
    assumption that hurts when it is wrong.
    """
    volumes = [p.volume for p in price_points[-lookback:]
               if getattr(p, "volume", None) is not None and p.volume > 0]
    if not volumes:
        return None
    average_volume = mean(volumes)
    if not average_volume or average_volume <= 0:
        return None
    return safe_ratio(abs(quantity), average_volume)
