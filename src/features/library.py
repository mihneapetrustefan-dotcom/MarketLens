"""
src/features/library.py
----------------------------
The concrete feature catalog and point-in-time transformations
(Phase 8, spec §5-§29, §32).

NO DUPLICATED MATH (spec §2, §63): every numerical primitive already
built in Phase 6 — returns, realized volatility, relative volume,
z-score, percentile — is IMPORTED from src/impact/calculations.py, not
reimplemented. A second, subtly-different implementation of "return"
is exactly the inconsistency spec §2 asks us to find and avoid.

TRANSFORMATIONS ARE POINT-IN-TIME BY CONSTRUCTION (spec §32): the
classic leak is fitting a z-score's mean and standard deviation on the
whole dataset — including the future — and applying it historically.
Every transformation here takes its parameters from a HISTORY sequence
the caller drew through a cutoff, and there is no function that accepts
a whole column and standardizes it globally.
"""

import math
import statistics
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from src.domain.feature_models import (
    FeatureDefinition, FeatureStatus, MissingPolicy, ComputationCost, TimestampSemantics,
)
from src.domain.research_models import FeatureNamespace
from src.features.engine import FeatureContext, FeatureRegistry
# Reuse, never reimplement — see the module docstring.
from src.impact.calculations import (
    compute_return, realized_volatility, relative_volume, volume_zscore,
    baseline_statistics, percentile, cumulative_return,
)
from src.domain.impact_models import ReturnMethod


# ============================================================
# Point-in-time transformations (spec §28, §32)
# ============================================================

def zscore_point_in_time(value: Optional[float], history: Sequence[Optional[float]]) -> Optional[float]:
    """
    Standardize using ONLY the supplied history.

    There is deliberately no variant that takes a full column and
    standardizes it in place: fitting mean/std across the entire
    dataset leaks future information into every historical row, and it
    is the most common way a research pipeline quietly cheats.
    """
    if value is None:
        return None
    clean = [v for v in history if v is not None]
    if len(clean) < 2:
        return None
    mean = statistics.fmean(clean)
    std = statistics.stdev(clean)
    if std == 0:
        return None
    return round((value - mean) / std, 6)


def percentile_rank_point_in_time(value: Optional[float], history: Sequence[Optional[float]]) -> Optional[float]:
    """Where `value` sits within its own past distribution, in [0,1]. History must already be cutoff-bounded."""
    if value is None:
        return None
    clean = [v for v in history if v is not None]
    if not clean:
        return None
    below = sum(1 for v in clean if v < value)
    return round(below / len(clean), 6)


def winsorize(value: Optional[float], history: Sequence[Optional[float]],
              lower_pct: float = 1.0, upper_pct: float = 99.0) -> Optional[float]:
    """
    Clip to historical percentile bounds (spec §30).

    Winsorizing rather than DROPPING is deliberate: a 20% move on
    earnings day is the observation a study most wants, and removing it
    would delete the signal while keeping the noise. Clipping limits
    its leverage without pretending it did not happen.
    """
    if value is None:
        return None
    clean = [v for v in history if v is not None]
    if len(clean) < 2:
        return value
    low = percentile(clean, lower_pct)
    high = percentile(clean, upper_pct)
    if low is None or high is None:
        return value
    return round(min(max(value, low), high), 6)


def cross_sectional_rank(value: Optional[float], universe_values: Dict[str, Optional[float]],
                          instrument_id: str) -> Optional[float]:
    """
    Percentile rank of one instrument within its universe at a point in
    time (spec §22, §23).

    THE UNIVERSE IS THE CALLER'S RESPONSIBILITY, and must reflect
    HISTORICAL membership. Ranking a 2019 observation against today's
    index constituents injects survivorship bias — only the companies
    that survived are present, so the historical rank is wrong in a
    direction that flatters the result.

    Ties share the average rank; instruments with no value are excluded
    from the denominator rather than treated as zero.
    """
    if value is None:
        return None
    present = [v for v in universe_values.values() if v is not None]
    if len(present) < 2:
        return None
    below = sum(1 for v in present if v < value)
    equal = sum(1 for v in present if v == value)
    return round((below + 0.5 * max(0, equal - 1)) / (len(present) - 1 if equal <= 1 else len(present)), 6)


# ============================================================
# Feature computation callables
# ============================================================

def _return_over(context: FeatureContext, periods: int) -> Optional[float]:
    prices = context.prices()
    if len(prices) < periods + 1:
        return None   # insufficient lookback — never padded or guessed
    return compute_return(prices[-(periods + 1)], prices[-1], ReturnMethod.SIMPLE)


def _volatility_over(context: FeatureContext, periods: int) -> Optional[float]:
    prices = context.prices(periods + 1)
    if len(prices) < periods + 1:
        return None
    returns = [compute_return(a, b, ReturnMethod.SIMPLE) for a, b in zip(prices, prices[1:])]
    return realized_volatility(returns)


def _relative_volume(context: FeatureContext, periods: int = 20) -> Optional[float]:
    volumes = context.volumes()
    if len(volumes) < periods + 1:
        return None
    mean, _ = baseline_statistics(volumes[-(periods + 1):-1])
    return relative_volume(volumes[-1], mean)


def _volume_zscore(context: FeatureContext, periods: int = 20) -> Optional[float]:
    volumes = context.volumes()
    if len(volumes) < periods + 1:
        return None
    mean, std = baseline_statistics(volumes[-(periods + 1):-1])
    return volume_zscore(volumes[-1], mean, std)


def _distance_from_moving_average(context: FeatureContext, periods: int = 20) -> Optional[float]:
    prices = context.prices()
    if len(prices) < periods:
        return None
    average = statistics.fmean(prices[-periods:])
    if average == 0:
        return None
    return round((prices[-1] - average) / average, 6)


def _drawdown(context: FeatureContext, periods: int = 60) -> Optional[float]:
    prices = context.prices(periods)
    if len(prices) < 2:
        return None
    peak = max(prices)
    if peak == 0:
        return None
    return round((prices[-1] - peak) / peak, 6)


def _rsi(context: FeatureContext, periods: int = 14) -> Optional[float]:
    """
    Relative Strength Index — Wilder's definition, computed
    deterministically (spec §8: never an LLM, never approximated).

        RSI = 100 - 100 / (1 + avg_gain / avg_loss)
    """
    prices = context.prices(periods + 1)
    if len(prices) < periods + 1:
        return None
    gains, losses = [], []
    for previous, current in zip(prices, prices[1:]):
        change = current - previous
        gains.append(max(0.0, change))
        losses.append(max(0.0, -change))
    average_gain = statistics.fmean(gains)
    average_loss = statistics.fmean(losses)
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0   # no losses: maximal RSI, or neutral if perfectly flat
    rs = average_gain / average_loss
    return round(100 - (100 / (1 + rs)), 4)


def _days_since_last_event(context: FeatureContext) -> Optional[float]:
    """Recency (spec §10). Uses only events knowable at the cutoff."""
    events = context.known_events()
    if not events:
        return None
    return round((context.cutoff - events[-1].publication_time).total_seconds() / 86400, 4)


def _event_count_window(context: FeatureContext, days: int) -> int:
    """Frequency (spec §11). Returns 0 legitimately — zero events IS zero, not missing."""
    since = context.cutoff - timedelta(days=days)
    return sum(1 for e in context.known_events() if e.publication_time >= since)


def _event_novelty(context: FeatureContext, lookback_days: int = 365) -> Optional[float]:
    """
    How unusual this event type is for this entity (spec §12).

        novelty = 1 / (1 + count of same-type events in the lookback)

    Documented and bounded rather than a vague subjective score: 1.0
    means never seen before in the window, and it decays as the event
    type becomes routine for that company.
    """
    event_type = context.metadata.get("event_type")
    if not event_type:
        return None
    since = context.cutoff - timedelta(days=lookback_days)
    same_type = sum(1 for e in context.known_events()
                     if getattr(e, "event_type", None) == event_type and e.publication_time >= since)
    return round(1.0 / (1.0 + same_type), 6)


def _article_count_window(context: FeatureContext, days: int) -> int:
    since = context.cutoff - timedelta(days=days)
    return sum(1 for a in context.known_articles() if a.published_at >= since)


def _source_diversity(context: FeatureContext, days: int = 7) -> Optional[float]:
    """
    Distinct sources divided by article count (spec §15).

    1.0 means every article came from a different outlet; a low value
    means one story echoed repeatedly. Article COUNT alone would
    mistake echo for corroboration (spec §13's explicit warning).
    """
    since = context.cutoff - timedelta(days=days)
    recent = [a for a in context.known_articles() if a.published_at >= since]
    if not recent:
        return None
    sources = {getattr(a, "source_name", None) for a in recent if getattr(a, "source_name", None)}
    return round(len(sources) / len(recent), 6)


def _mean_sentiment(context: FeatureContext, days: int = 7) -> Optional[float]:
    since = context.cutoff - timedelta(days=days)
    scores = [getattr(a, "sentiment_score", None) for a in context.known_articles()
               if a.published_at >= since]
    clean = [float(s) for s in scores if s is not None]
    return round(statistics.fmean(clean), 6) if clean else None


def _sentiment_dispersion(context: FeatureContext, days: int = 7) -> Optional[float]:
    """
    Disagreement among sources (spec §14). High dispersion means the
    news flow is genuinely mixed — materially different from a
    confident neutral average, which a mean alone cannot distinguish.
    """
    since = context.cutoff - timedelta(days=days)
    scores = [getattr(a, "sentiment_score", None) for a in context.known_articles()
               if a.published_at >= since]
    clean = [float(s) for s in scores if s is not None]
    return round(statistics.stdev(clean), 6) if len(clean) >= 2 else None


def _peer_relative_return(context: FeatureContext, periods: int = 5) -> Optional[float]:
    """
    Return minus the peer median (spec §17).

    Peers come from `peer_candles`, which the caller populates from
    DEFINED peer relationships — not from "everything in the sector",
    which spec §17 explicitly warns against.
    """
    own = _return_over(context, periods)
    if own is None or not context.peer_candles:
        return None
    peer_returns = []
    for peer_id in context.peer_candles:
        prices = [c.price for c in context.known_peer_candles(peer_id) if getattr(c, "price", None) is not None]
        if len(prices) >= periods + 1:
            value = compute_return(prices[-(periods + 1)], prices[-1], ReturnMethod.SIMPLE)
            if value is not None:
                peer_returns.append(value)
    if not peer_returns:
        return None
    return round(own - statistics.median(peer_returns), 6)


def _peer_dispersion(context: FeatureContext, periods: int = 5) -> Optional[float]:
    peer_returns = []
    for peer_id in context.peer_candles:
        prices = [c.price for c in context.known_peer_candles(peer_id) if getattr(c, "price", None) is not None]
        if len(prices) >= periods + 1:
            value = compute_return(prices[-(periods + 1)], prices[-1], ReturnMethod.SIMPLE)
            if value is not None:
                peer_returns.append(value)
    return round(statistics.stdev(peer_returns), 6) if len(peer_returns) >= 2 else None


def _volatility_percentile(context: FeatureContext, periods: int = 20, lookback: int = 120) -> Optional[float]:
    """Where current volatility sits in its OWN history — the regime input (spec §24)."""
    prices = context.prices()
    if len(prices) < lookback:
        return None
    returns = [compute_return(a, b, ReturnMethod.SIMPLE) for a, b in zip(prices, prices[1:])]
    current = realized_volatility(returns[-periods:])
    if current is None:
        return None
    rolling = [realized_volatility(returns[i:i + periods]) for i in range(len(returns) - periods)]
    rolling = [r for r in rolling if r is not None]
    if not rolling:
        return None
    return round(sum(1 for r in rolling if r < current) / len(rolling), 6)


def _surprise_pct(actual: Optional[float], consensus: Optional[float]) -> Optional[float]:
    """
    (actual - consensus) / |consensus| (spec §21).

    Returns None on a zero or missing consensus — dividing by zero
    would produce an infinity that poisons every aggregate downstream,
    and fabricating a consensus is explicitly forbidden.
    """
    if actual is None or consensus is None or consensus == 0:
        return None
    return round((actual - consensus) / abs(consensus), 6)


def _event_confidence(context: FeatureContext) -> Optional[float]:
    """Carried from Phase 5's fused event — a contemporaneous event attribute, not a market observation."""
    return context.metadata.get("event_confidence")


def _independent_source_count(context: FeatureContext) -> Optional[int]:
    return context.metadata.get("independent_source_count")


# ============================================================
# Catalog
# ============================================================

def build_default_registry() -> FeatureRegistry:
    """
    Register the standard feature catalog.

    DELIBERATELY BOUNDED (spec §5, §8): one feature per genuine
    question, not every window between 1 and 200 days. Feature
    proliferation makes a dataset look rich while multiplying the
    chances that something correlates with the label by accident.
    """
    registry = FeatureRegistry()

    def add(feature_id, name, namespace, formula, compute, description="",
            dependencies=None, lookback=None, cost=ComputationCost.CHEAP,
            missing=MissingPolicy.MISSING, semantics=TimestampSemantics.TRAILING_WINDOW,
            source="market_observations", output_type="float"):
        registry.register(FeatureDefinition(
            feature_id=feature_id, name=name, namespace=namespace, formula=formula,
            description=description, dependencies=dependencies or [], lookback_periods=lookback,
            cost=cost, missing_policy=missing, timestamp_semantics=semantics,
            source=source, output_type=output_type, compute=compute,
        ))

    # --- MARKET (spec §5) ---
    for periods in (1, 5, 20, 60):
        add(f"market.return_{periods}d", f"return_{periods}d", FeatureNamespace.MARKET,
            f"close[t] / close[t-{periods}] - 1",
            lambda ctx, p=periods: _return_over(ctx, p),
            description=f"Simple return over the trailing {periods} sessions.", lookback=periods)

    add("market.distance_from_ma20", "distance_from_ma20", FeatureNamespace.MARKET,
        "(close[t] - mean(close[t-19..t])) / mean(close[t-19..t])",
        lambda ctx: _distance_from_moving_average(ctx, 20),
        description="How far price sits from its own 20-session mean.", lookback=20)

    add("market.drawdown_60d", "drawdown_60d", FeatureNamespace.MARKET,
        "(close[t] - max(close[t-59..t])) / max(close[t-59..t])",
        lambda ctx: _drawdown(ctx, 60),
        description="Current decline from the trailing 60-session peak.", lookback=60)

    # --- VOLATILITY (spec §6) ---
    for periods in (5, 20):
        add(f"volatility.realized_{periods}d", f"realized_{periods}d", FeatureNamespace.VOLATILITY,
            f"stdev(simple returns over trailing {periods} sessions)",
            lambda ctx, p=periods: _volatility_over(ctx, p),
            description=f"Realized volatility over {periods} sessions.", lookback=periods)

    add("volatility.percentile_20d", "percentile_20d", FeatureNamespace.VOLATILITY,
        "rank of current 20d volatility within its own trailing 120-session history",
        lambda ctx: _volatility_percentile(ctx, 20, 120),
        description="Volatility regime input, expressed as a self-relative percentile.",
        lookback=120, cost=ComputationCost.MODERATE)

    # --- LIQUIDITY (spec §7) ---
    add("liquidity.relative_volume_20d", "relative_volume_20d", FeatureNamespace.LIQUIDITY,
        "volume[t] / mean(volume[t-20..t-1])",
        lambda ctx: _relative_volume(ctx, 20),
        description="Volume as a multiple of its own baseline — normalized, so comparable across instruments.",
        lookback=20)

    add("liquidity.volume_zscore_20d", "volume_zscore_20d", FeatureNamespace.LIQUIDITY,
        "(volume[t] - mean(baseline)) / stdev(baseline)",
        lambda ctx: _volume_zscore(ctx, 20),
        description="How many standard deviations volume sits from its baseline.", lookback=20)

    # --- TECHNICAL (spec §8) ---
    add("technical.rsi_14", "rsi_14", FeatureNamespace.TECHNICAL,
        "100 - 100/(1 + avg_gain/avg_loss) over 14 sessions (Wilder)",
        lambda ctx: _rsi(ctx, 14),
        description="Relative Strength Index, deterministic.", lookback=14)

    # --- EVENT (spec §9, §10, §11, §12) ---
    add("event.confidence", "confidence", FeatureNamespace.EVENT,
        "carried from the fused canonical event (Phase 5)",
        _event_confidence, description="How confident we are the event is correctly represented.",
        semantics=TimestampSemantics.CONTEMPORANEOUS_EVENT, source="event_fusion")

    add("event.independent_source_count", "independent_source_count", FeatureNamespace.EVENT,
        "distinct sources with ORIGINAL_REPORT lineage (Phase 5)",
        _independent_source_count, description="Independent confirmations, excluding syndicated copies.",
        semantics=TimestampSemantics.CONTEMPORANEOUS_EVENT, source="event_fusion", output_type="int")

    add("event.days_since_last", "days_since_last", FeatureNamespace.EVENT,
        "(cutoff - publication_time of most recent prior event) in days",
        _days_since_last_event, description="Recency of the previous event for this entity.",
        source="event_store")

    for days in (7, 30):
        add(f"event.count_{days}d", f"count_{days}d", FeatureNamespace.EVENT,
            f"count of events with publication_time in (cutoff-{days}d, cutoff]",
            lambda ctx, d=days: _event_count_window(ctx, d),
            description=f"Event density over the trailing {days} days.",
            missing=MissingPolicy.ZERO_IS_SEMANTIC, source="event_store", output_type="int")

    add("event.novelty_365d", "novelty_365d", FeatureNamespace.EVENT,
        "1 / (1 + count of same-type events in the trailing 365 days)",
        lambda ctx: _event_novelty(ctx, 365),
        description="How unusual this event type is for this entity. 1.0 = first occurrence in the window.",
        source="event_store")

    # --- NEWS & SENTIMENT (spec §13, §14, §15) ---
    add("news.article_count_7d", "article_count_7d", FeatureNamespace.NEWS,
        "count of articles published in (cutoff-7d, cutoff]",
        lambda ctx: _article_count_window(ctx, 7),
        description="Raw article volume. NOT a measure of importance on its own.",
        missing=MissingPolicy.ZERO_IS_SEMANTIC, source="news_store", output_type="int")

    add("news.source_diversity_7d", "source_diversity_7d", FeatureNamespace.NEWS,
        "distinct sources / article count over the trailing 7 days",
        lambda ctx: _source_diversity(ctx, 7),
        description="1.0 = every article from a different outlet; low = one story echoed.",
        source="news_store")

    add("sentiment.mean_7d", "mean_7d", FeatureNamespace.SENTIMENT,
        "mean of article sentiment scores over the trailing 7 days",
        lambda ctx: _mean_sentiment(ctx, 7),
        description="Average sentiment of INFORMATION — deliberately not a market-impact measure.",
        source="sentiment_engine")

    add("sentiment.dispersion_7d", "dispersion_7d", FeatureNamespace.SENTIMENT,
        "stdev of article sentiment scores over the trailing 7 days",
        lambda ctx: _sentiment_dispersion(ctx, 7),
        description="Disagreement among sources; distinguishes 'mixed' from 'confidently neutral'.",
        source="sentiment_engine")

    # --- PEER / CROSS-SECTIONAL (spec §17, §22) ---
    add("peer.relative_return_5d", "relative_return_5d", FeatureNamespace.PEER,
        "own 5d return - median(peer 5d returns)",
        lambda ctx: _peer_relative_return(ctx, 5),
        description="Performance against DEFINED peers, not all sector members.",
        dependencies=["market.return_5d"], lookback=5, cost=ComputationCost.MODERATE,
        source="market_observations")

    add("peer.dispersion_5d", "dispersion_5d", FeatureNamespace.PEER,
        "stdev(peer 5d returns)",
        lambda ctx: _peer_dispersion(ctx, 5),
        description="How much peers disagree — separates idiosyncratic from sector-wide moves.",
        lookback=5, cost=ComputationCost.MODERATE)

    return registry


def build_default_feature_sets(created_at: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Standard, versioned feature sets (spec §51).

    Grouped by the QUESTION they answer, so a research run can request
    "market baseline only" and get a reproducible, named bundle rather
    than an ad-hoc list of feature ids.
    """
    from src.domain.feature_models import FeatureSet
    return {
        "market_baseline_v1": FeatureSet(
            feature_set_id="market_baseline", name="Market baseline", version="v1",
            description="Price, volatility and liquidity features with no event dependency.",
            feature_ids=[
                "market.return_1d", "market.return_5d", "market.return_20d",
                "market.distance_from_ma20", "volatility.realized_20d",
                "liquidity.relative_volume_20d",
            ],
            created_at=created_at),
        "event_intelligence_v1": FeatureSet(
            feature_set_id="event_intelligence", name="Event intelligence", version="v1",
            description="Event confidence, recency, density and novelty.",
            feature_ids=[
                "event.confidence", "event.independent_source_count",
                "event.days_since_last", "event.count_30d", "event.novelty_365d",
            ],
            created_at=created_at),
        "news_sentiment_v1": FeatureSet(
            feature_set_id="news_sentiment", name="News and sentiment", version="v1",
            description="Article flow, source diversity and sentiment distribution.",
            feature_ids=[
                "news.article_count_7d", "news.source_diversity_7d",
                "sentiment.mean_7d", "sentiment.dispersion_7d",
            ],
            created_at=created_at),
    }
