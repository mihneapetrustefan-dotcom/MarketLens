"""
src/features/intraday.py
-------------------------------------------
Intraday feature definitions (Phase 25.9F).

WHAT CHANGED, AND WHAT DELIBERATELY DID NOT
-----------------------------------------------
Phase 8 already built the right machine: a `FeatureRegistry` with
versioned, namespaced definitions and a `FeatureContext` that is a
point-in-time lens a feature author cannot reach around. Phase 25.9E's
audit found the session runner's "features" stage only counted bars,
and the registry held 24 features -- every one of them daily-session,
event or news frequency. **Zero intraday features existed.**

So this module adds DEFINITIONS to the existing registry. It does not
add a second engine, a second registry, or a second notion of what a
feature is (§45).

WHY NOT REUSE `market.return_5d` ON MINUTE BARS
--------------------------------------------------
Because it would silently lie. Those definitions index by POSITION in
the candle list, so feeding them one-minute bars computes a five-MINUTE
return under a name that says five days, and every stored value from
before the change would be reinterpreted. Intraday features get their
own names, their own namespace prefix and their own version.

THE THREE RULES EVERY FEATURE HERE OBEYS
--------------------------------------------
1. CLOSED BARS ONLY. The context is built from `trailing_run`, which
   returns bars whose `bar_end <= cutoff`. The minute in progress is
   not merely discouraged, it is absent.

2. CONTIGUOUS MINUTES ONLY. A window is computed inside one unbroken
   run. On this project's real data the median run is two minutes long,
   so a rolling window that spanned a gap would be computing an
   overnight or cross-event jump and reporting it as momentum.

3. MISSING IS MISSING. Too little contiguous history returns None with
   `INSUFFICIENT_HISTORY`, never a shorter window quietly substituted
   and never zero.

WHAT IS NOT IMPLEMENTED, AND WHY
------------------------------------
No spread, relative spread or quote-imbalance feature: the research
corpus (`price_candle_cache`) stores OHLCV and carries no bid or ask.
§12 asks for microstructure features "where quote information
genuinely exists" -- here it does not, so they are absent rather than
approximated from a high-low range and called a spread.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Sequence

from src.domain.feature_models import (
    ComputationCost, FeatureDefinition, MissingPolicy, TimestampSemantics,
)
from src.domain.research_models import FeatureNamespace
from src.features.engine import FeatureContext, FeatureRegistry
from src.marketdata.calendar import NEW_YORK, USEquityCalendar

#: Every intraday feature carries this version. A change to any formula
#: here requires bumping it, because stored values must never be
#: reinterpreted by a newer definition (§11).
INTRADAY_FEATURE_VERSION = "v1"

#: The namespace prefix. `intraday.` is not a FeatureNamespace member --
#: the namespace is the analytical family (MARKET, VOLATILITY, ...) and
#: the NAME carries the frequency, so `market.return_5m@v1` sits beside
#: `market.return_5d@v1` without either being mistakable for the other.
INTRADAY_SOURCE = "intraday_1m_bars"


# ======================================================================
# Helpers. Each takes the contiguous closed run the context carries.
# ======================================================================

#: Attribute the per-context filtered run is memoized under.
_RUN_CACHE = "_intraday_known_run"


def _run(context: FeatureContext) -> List:
    """
    The bars this feature may see.

    `known_candles()` re-filters on `.timestamp`, which for an
    `IntradayBar` is `bar_end`. So even a caller who built the context
    carelessly cannot get the minute in progress through this.

    MEMOIZED PER CONTEXT, NOT SKIPPED. The filter still runs -- once per
    observation instead of once per feature. Profiling a real build
    showed the nineteen features re-filtering and re-sorting the same
    list nineteen times, which was 31 of 40 seconds. The cache is keyed
    on the context's own cutoff, so it cannot outlive the observation it
    belongs to or serve a different moment.
    """
    cached = getattr(context, _RUN_CACHE, None)
    if cached is not None and cached[0] == context.cutoff:
        return cached[1]
    known = context.known_candles()
    try:
        setattr(context, _RUN_CACHE, (context.cutoff, known))
    except AttributeError:            # a context that forbids attributes
        pass
    return known


def _closes(context: FeatureContext, needed: int) -> Optional[List[float]]:
    bars = _run(context)
    if len(bars) < needed:
        return None
    closes = [b.close for b in bars[-needed:]]
    return None if any(c is None for c in closes) else closes


def _return_over(context: FeatureContext, minutes: int) -> Optional[float]:
    """Simple return across `minutes` closed, contiguous bars."""
    closes = _closes(context, minutes + 1)
    if closes is None or closes[0] in (None, 0):
        return None
    return closes[-1] / closes[0] - 1.0


def _realized_volatility(context: FeatureContext, minutes: int) -> Optional[float]:
    """Stdev of one-minute returns inside the window. Not annualised."""
    closes = _closes(context, minutes + 1)
    if closes is None:
        return None
    returns = [b / a - 1.0 for a, b in zip(closes, closes[1:]) if a]
    if len(returns) < 2:
        return None
    return statistics.pstdev(returns)


def _mean_absolute_return(context: FeatureContext, minutes: int) -> Optional[float]:
    closes = _closes(context, minutes + 1)
    if closes is None:
        return None
    returns = [abs(b / a - 1.0) for a, b in zip(closes, closes[1:]) if a]
    if not returns:
        return None
    return statistics.fmean(returns)


def _range_over(context: FeatureContext, minutes: int) -> Optional[float]:
    """(high - low) / close across the window, from the bars' own extremes."""
    bars = _run(context)
    if len(bars) < minutes:
        return None
    window = bars[-minutes:]
    highs = [b.high for b in window if b.high is not None]
    lows = [b.low for b in window if b.low is not None]
    last = window[-1].close
    if not highs or not lows or not last:
        return None
    return (max(highs) - min(lows)) / last


def _momentum_ratio(context: FeatureContext, short: int, long: int
                    ) -> Optional[float]:
    """Short-window return minus long-window return: acceleration, signed."""
    fast = _return_over(context, short)
    slow = _return_over(context, long)
    if fast is None or slow is None:
        return None
    return fast - slow


def _relative_volume(context: FeatureContext, minutes: int) -> Optional[float]:
    """Latest minute's volume against the mean of the window before it."""
    bars = _run(context)
    if len(bars) < minutes + 1:
        return None
    window = bars[-(minutes + 1):]
    history = [b.volume for b in window[:-1]]
    latest = window[-1].volume
    if latest is None or any(v is None for v in history):
        return None
    mean = statistics.fmean(history)
    if not mean:
        return None
    return latest / mean


def _session_vwap_distance(context: FeatureContext) -> Optional[float]:
    """
    Distance from the volume-weighted average price of the run so far.

    Computed over the contiguous run only, which on fragmentary data is
    an event window rather than a full session -- the name says VWAP of
    what is actually observed, and `run_minutes` travels beside it so a
    consumer can tell a 400-minute session from a 30-minute fragment.
    """
    bars = _run(context)
    priced = [(b.close, b.volume) for b in bars
              if b.close is not None and b.volume not in (None, 0)]
    if len(priced) < 2:
        return None
    total_volume = sum(v for _c, v in priced)
    if not total_volume:
        return None
    vwap = sum(c * v for c, v in priced) / total_volume
    last = bars[-1].close
    if not vwap or last is None:
        return None
    return last / vwap - 1.0


def _run_minutes(context: FeatureContext) -> Optional[float]:
    """
    How many contiguous closed minutes back this feature row can see.

    Not decoration: on this corpus it is the difference between a
    genuine window and three minutes of an event fragment, and a
    consumer that cannot tell them apart will average them.
    """
    bars = _run(context)
    return float(len(bars)) if bars else None


def _minutes_since_open(context: FeatureContext,
                        calendar: Optional[USEquityCalendar] = None
                        ) -> Optional[float]:
    calendar = calendar or USEquityCalendar()
    cutoff = context.cutoff.astimezone(timezone.utc)
    window = calendar.session(cutoff.astimezone(NEW_YORK).date())
    if not window.is_trading_day or window.opens_at is None:
        return None
    delta = (cutoff - window.opens_at).total_seconds() / 60.0
    return delta if delta >= 0 else None


def _minutes_to_close(context: FeatureContext,
                      calendar: Optional[USEquityCalendar] = None
                      ) -> Optional[float]:
    calendar = calendar or USEquityCalendar()
    cutoff = context.cutoff.astimezone(timezone.utc)
    window = calendar.session(cutoff.astimezone(NEW_YORK).date())
    if not window.is_trading_day or window.closes_at is None:
        return None
    # Only inside the session, exactly like `minutes_since_open`. A
    # pre-market cutoff is not "424 minutes into the session"; the two
    # session-position features must agree on when a session exists.
    if not (window.opens_at <= cutoff < window.closes_at):
        return None
    return (window.closes_at - cutoff).total_seconds() / 60.0


def _overnight_gap(context: FeatureContext) -> Optional[float]:
    """
    This session's first observed price against the previous session's
    last observed price.

    Deliberately a SEPARATE feature rather than something a one-minute
    return is ever allowed to absorb (§9). `metadata["previous_close"]`
    is supplied by the dataset builder, which is the only component
    that can see across the session break.
    """
    previous = context.metadata.get("previous_session_close")
    bars = _run(context)
    if previous in (None, 0) or not bars:
        return None
    first = next((b.close for b in bars if b.close is not None), None)
    if first is None:
        return None
    return first / previous - 1.0


# ---- market context, computed on the benchmark, never on self -------

def _market_return(context: FeatureContext, minutes: int) -> Optional[float]:
    """
    The benchmark's return over the same window.

    Read from `metadata["market_returns"]`, which the dataset builder
    computes from the BENCHMARK's own bars. A stock's market-context
    feature must not be derived from its own future contribution to an
    aggregate (§31).
    """
    returns = context.metadata.get("market_returns") or {}
    value = returns.get(minutes)
    return float(value) if value is not None else None


def _cross_sectional_dispersion(context: FeatureContext) -> Optional[float]:
    """
    Spread of peer returns at this same minute.

    Supplied by the builder from the other instruments observed at the
    SAME cutoff -- point-in-time by construction, because an instrument
    with no closed bar at the cutoff is simply not in the set (§37).
    """
    value = context.metadata.get("cross_sectional_dispersion")
    return float(value) if value is not None else None


# ======================================================================
# Registration
# ======================================================================

def register_intraday_features(registry: FeatureRegistry) -> FeatureRegistry:
    """
    Add the intraday definitions to an existing registry.

    DELIBERATELY SMALL (§12, §13). One feature per genuine question.
    No RSI, no MACD, no family of eleven moving averages: on a corpus
    whose median contiguous run is two minutes long, a hundred
    indicators would multiply the chances of an accidental correlation
    without adding one new fact about the market.
    """
    def add(name, namespace, formula, compute, description="", lookback=None,
            missing=MissingPolicy.INSUFFICIENT_HISTORY,
            semantics=TimestampSemantics.TRAILING_WINDOW,
            output_type="float"):
        registry.register(FeatureDefinition(
            feature_id=f"{namespace.value}.{name}",
            name=name, namespace=namespace,
            version=INTRADAY_FEATURE_VERSION,
            formula=formula, description=description,
            lookback_periods=lookback, cost=ComputationCost.CHEAP,
            missing_policy=missing, timestamp_semantics=semantics,
            source=INTRADAY_SOURCE, output_type=output_type, compute=compute))

    # --- returns, the price-action family ---------------------------
    for minutes in (1, 5, 15, 30, 60):
        add(f"return_{minutes}m", FeatureNamespace.MARKET,
            f"close[t] / close[t-{minutes}m] - 1 (contiguous closed bars)",
            lambda ctx, m=minutes: _return_over(ctx, m),
            description=(f"Return over the trailing {minutes} contiguous "
                         f"closed minutes. None if the unbroken run is shorter."),
            lookback=minutes + 1)

    add("momentum_5m_vs_30m", FeatureNamespace.MARKET,
        "return_5m - return_30m",
        lambda ctx: _momentum_ratio(ctx, 5, 30),
        description="Short-horizon acceleration against the medium horizon.",
        lookback=31)

    # --- volatility --------------------------------------------------
    for minutes in (15, 30):
        add(f"realized_{minutes}m", FeatureNamespace.VOLATILITY,
            f"pstdev(one-minute returns over trailing {minutes} minutes)",
            lambda ctx, m=minutes: _realized_volatility(ctx, m),
            description=f"Realized volatility over {minutes} contiguous minutes.",
            lookback=minutes + 1)

    add("mean_abs_return_15m", FeatureNamespace.VOLATILITY,
        "mean(|one-minute return|) over trailing 15 minutes",
        lambda ctx: _mean_absolute_return(ctx, 15),
        description="Average absolute minute move; robust to a single outlier.",
        lookback=16)

    add("range_15m", FeatureNamespace.VOLATILITY,
        "(max(high) - min(low)) / close over trailing 15 minutes",
        lambda ctx: _range_over(ctx, 15),
        description="Intraday range across the window, from the bars' extremes.",
        lookback=15)

    # --- volume ------------------------------------------------------
    add("relative_volume_30m", FeatureNamespace.LIQUIDITY,
        "volume[t] / mean(volume[t-30m..t-1m])",
        lambda ctx: _relative_volume(ctx, 30),
        description="This minute's volume against its own recent mean.",
        lookback=31)

    # --- session context ---------------------------------------------
    add("minutes_since_open", FeatureNamespace.REGIME,
        "cutoff - session open (exchange calendar)",
        _minutes_since_open,
        description="Position within the regular session. None outside it.",
        semantics=TimestampSemantics.AS_OF_CUTOFF,
        missing=MissingPolicy.NOT_APPLICABLE)

    add("minutes_to_close", FeatureNamespace.REGIME,
        "session close - cutoff (exchange calendar)",
        _minutes_to_close,
        description="Time remaining in the regular session. None outside it.",
        semantics=TimestampSemantics.AS_OF_CUTOFF,
        missing=MissingPolicy.NOT_APPLICABLE)

    add("overnight_gap", FeatureNamespace.MARKET,
        "first observed close / previous session's last close - 1",
        _overnight_gap,
        description=("The session break, as its own feature so no minute "
                     "return ever absorbs it."),
        semantics=TimestampSemantics.AS_OF_CUTOFF)

    add("vwap_distance", FeatureNamespace.MARKET,
        "close[t] / VWAP(observed contiguous run) - 1",
        _session_vwap_distance,
        description=("Distance from the volume-weighted mean of the observed "
                     "run. Read with run_minutes: a fragment is not a session."),
        semantics=TimestampSemantics.AS_OF_CUTOFF)

    add("run_minutes", FeatureNamespace.REGIME,
        "count(contiguous closed minutes ending at cutoff)",
        _run_minutes,
        description=("How much unbroken history this row actually had. The "
                     "honesty column for every window above."),
        semantics=TimestampSemantics.AS_OF_CUTOFF, output_type="int")

    # --- market context, from the benchmark --------------------------
    for minutes in (5, 30):
        add(f"market_return_{minutes}m", FeatureNamespace.CROSS_SECTIONAL,
            f"benchmark return over trailing {minutes} contiguous minutes",
            lambda ctx, m=minutes: _market_return(ctx, m),
            description=("Benchmark move over the same window, computed on the "
                         "benchmark's own bars, never on this instrument."),
            lookback=minutes + 1)

    add("dispersion_1m", FeatureNamespace.CROSS_SECTIONAL,
        "pstdev(one-minute returns across instruments closed at this cutoff)",
        _cross_sectional_dispersion,
        description=("Cross-sectional spread at this minute, over exactly the "
                     "instruments whose bar had closed."),
        semantics=TimestampSemantics.AS_OF_CUTOFF)

    return registry


def intraday_feature_ids(registry: FeatureRegistry) -> List[str]:
    """Every feature in the registry sourced from one-minute bars."""
    return sorted(d.feature_id for d in registry.all()
                  if d.source == INTRADAY_SOURCE)


def build_intraday_registry() -> FeatureRegistry:
    """The canonical registry plus the intraday definitions."""
    from src.features.library import build_default_registry
    return register_intraday_features(build_default_registry())
