"""
src/backtest/attribution.py
--------------------------------
Where the results came from, and how much they can be trusted
(Phase 12, spec §39-§44, §99, §100, §108).

ATTRIBUTION DESCRIBES; IT DOES NOT EXPLAIN
----------------------------------------------
Spec §108 draws a line this module is built around. Saying "trades
tagged EARNINGS produced +4.2%" is a description of where P&L
accumulated. It is NOT the claim "earnings events caused that profit" —
the trades might have been long tech through a tech rally, and the
event tag merely rode along.

So every bucket here reports sums and counts, and nothing in this file
computes a "contribution to alpha" or ranks causes. The naming stays
descriptive (`net_pnl`, `trades`, `win_rate`) precisely so a reader
cannot mistake a grouping for a finding. Where a caller wants causal
language, the data does not support it and the report says so.

THE QUALITY SCORE IS NOT A PROFITABILITY SCORE
--------------------------------------------------
`assess_quality` grades how much a run's numbers can be believed:
sample size, execution realism, cost realism, point-in-time integrity,
history length. A wildly profitable run built on same-bar execution
with zero costs scores near the bottom, which is the entire point.
Spec §100 is explicit that confusing the two would be worse than not
scoring at all, so the returned object carries its own disclaimer.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence

from src.domain.backtest_models import (
    AttributionBucket, BacktestConfiguration, ExecutionTiming, PerformanceMetrics,
    QualityAssessment, SlippageMethod, Trade, WarningCode, finite_or_none, safe_ratio,
)
from src.domain.signal_models import Signal

#: Below this many closed trades, any per-bucket breakdown is anecdote.
MIN_TRADES_FOR_ATTRIBUTION = 5


def _bucket_key(value: Optional[str], fallback: str = "unattributed") -> str:
    """
    Missing dimensions get an explicit bucket, never silent exclusion.

    A sector breakdown that quietly dropped every unclassified trade
    would sum to less than the portfolio and still look complete —
    the same failure Phase 11's exposure engine guards against.
    """
    return value if value else fallback


class AttributionEngine:
    """Groups closed trades along the dimensions the data actually supports."""

    def __init__(self, sector_by_instrument: Optional[Dict[str, Optional[str]]] = None,
                 signals_by_id: Optional[Dict[str, Signal]] = None):
        self.sector_by_instrument = sector_by_instrument or {}
        self.signals_by_id = signals_by_id or {}

    # ---------------- grouping ----------------

    def _group(self, trades: Sequence[Trade], dimension: str,
               key_of, label_of=None) -> List[AttributionBucket]:
        buckets: Dict[str, AttributionBucket] = {}
        for trade in trades:
            key = _bucket_key(key_of(trade))
            bucket = buckets.get(key)
            if bucket is None:
                bucket = AttributionBucket(
                    dimension=dimension, key=key,
                    label=(label_of(key) if label_of else key))
                buckets[key] = bucket
            bucket.trades += 1
            bucket.net_pnl += trade.net_pnl
            bucket.gross_pnl += trade.gross_pnl
            bucket.costs += trade.costs
            if trade.is_win:
                bucket.wins += 1
        return sorted(buckets.values(), key=lambda b: b.net_pnl, reverse=True)

    # ---------------- dimensions ----------------

    def by_instrument(self, trades: Sequence[Trade]) -> List[AttributionBucket]:
        return self._group(trades, "instrument", lambda t: t.instrument_id)

    def by_sector(self, trades: Sequence[Trade]) -> List[AttributionBucket]:
        return self._group(
            trades, "sector",
            lambda t: t.sector_id or self.sector_by_instrument.get(t.instrument_id))

    def by_strategy(self, trades: Sequence[Trade]) -> List[AttributionBucket]:
        return self._group(trades, "strategy", lambda t: t.strategy_id)

    def by_event_type(self, trades: Sequence[Trade]) -> List[AttributionBucket]:
        """
        Event attribution (spec §43), resolved through the signal that
        opened the trade — the event chain is
        event -> signal -> allocation -> trade, and the trade only
        remembers its signal.
        """
        def event_of(trade: Trade) -> Optional[str]:
            signal = self.signals_by_id.get(trade.entry_signal_id or "")
            return signal.context.event_type if signal else None
        return self._group(trades, "event_type", event_of)

    def by_confidence_bucket(self, trades: Sequence[Trade]) -> List[AttributionBucket]:
        """
        Performance by the confidence the system claimed at entry
        (spec §39).

        This is the calibration question asked in money rather than hit
        rate: did the trades it was surest about actually pay more?
        """
        def confidence_of(trade: Trade) -> Optional[str]:
            signal = self.signals_by_id.get(trade.entry_signal_id or "")
            if signal is None or signal.confidence is None:
                return None
            lower = int(signal.confidence * 10) / 10.0
            return f"{lower:.1f}-{lower + 0.1:.1f}"
        return self._group(trades, "confidence_bucket", confidence_of)

    def by_model(self, trades: Sequence[Trade]) -> List[AttributionBucket]:
        """
        Model attribution (spec §40).

        A trade's signal may carry several model contributions; this
        assigns the trade to each contributing model, so buckets can sum
        to more than the portfolio total. That overlap is deliberate and
        stated: splitting P&L between models by weight would imply a
        causal decomposition the evidence does not support.
        """
        buckets: Dict[str, AttributionBucket] = {}
        for trade in trades:
            signal = self.signals_by_id.get(trade.entry_signal_id or "")
            models = ([c.model_qualified_id for c in signal.contributions]
                      if signal and signal.contributions else [])
            for model in (models or [None]):
                key = _bucket_key(model, "unattributed")
                bucket = buckets.setdefault(
                    key, AttributionBucket(dimension="model", key=key, label=key))
                bucket.trades += 1
                bucket.net_pnl += trade.net_pnl
                bucket.gross_pnl += trade.gross_pnl
                bucket.costs += trade.costs
                if trade.is_win:
                    bucket.wins += 1
        return sorted(buckets.values(), key=lambda b: b.net_pnl, reverse=True)

    def by_regime(self, trades: Sequence[Trade]) -> List[AttributionBucket]:
        """
        Regime attribution (spec §44).

        Uses the project's own regime field rather than inventing one.
        That field is NULL on every record in this database, so this
        returns a single `unattributed` bucket — reported honestly
        rather than filled with a regime classifier invented here.
        """
        def regime_of(trade: Trade) -> Optional[str]:
            signal = self.signals_by_id.get(trade.entry_signal_id or "")
            return signal.context.market_regime if signal else None
        return self._group(trades, "regime", regime_of)

    def all_dimensions(self, trades: Sequence[Trade]) -> List[AttributionBucket]:
        """Every supported breakdown, flattened."""
        if not trades:
            return []
        out: List[AttributionBucket] = []
        out.extend(self.by_instrument(trades))
        out.extend(self.by_sector(trades))
        out.extend(self.by_strategy(trades))
        out.extend(self.by_event_type(trades))
        out.extend(self.by_confidence_bucket(trades))
        out.extend(self.by_model(trades))
        out.extend(self.by_regime(trades))
        return out

    def is_meaningful(self, trades: Sequence[Trade]) -> bool:
        return len(trades) >= MIN_TRADES_FOR_ATTRIBUTION


# ============================================================
# Research quality
# ============================================================

def assess_quality(configuration: BacktestConfiguration,
                   metrics: PerformanceMetrics,
                   warning_codes: Iterable[WarningCode],
                   trading_days: int,
                   instruments_with_data: int,
                   instruments_requested: int) -> QualityAssessment:
    """
    Grade how much this run's numbers can be trusted (spec §100).

    NOT a profitability score. Each factor is scored 0..1 and combined
    as a WEIGHTED mean, with evidence weighted double against realism.

    The weighting is the one judgement call here, and it is deliberate:
    an unweighted mean lets a carefully-configured run over 30 trades
    score as highly as a well-evidenced one, because four configuration
    factors outvote two data factors. But good configuration cannot
    manufacture evidence — a realistic cost model applied to 30 trades
    still tells you almost nothing, while thin data cannot be fixed by
    any setting. So sample size, history length and point-in-time
    integrity count twice.
    """
    #: Evidence factors weigh double; configuration factors weigh once.
    weights = {
        "sample_size": 2.0,
        "history_length": 2.0,
        "point_in_time_integrity": 2.0,
        "execution_realism": 1.0,
        "cost_realism": 1.0,
        "data_coverage": 1.0,
        "benchmark": 1.0,
    }
    codes = set(warning_codes)
    factors: Dict[str, float] = {}
    notes: List[str] = []

    # --- sample size ---
    if metrics.total_trades >= 100:
        factors["sample_size"] = 1.0
    elif metrics.total_trades >= 30:
        factors["sample_size"] = 0.6
    elif metrics.total_trades >= 5:
        factors["sample_size"] = 0.3
    else:
        factors["sample_size"] = 0.0
        notes.append(f"{metrics.total_trades} closed trade(s) — far too few to "
                     f"distinguish skill from noise")

    # --- history length ---
    if trading_days >= 750:
        factors["history_length"] = 1.0
    elif trading_days >= 250:
        factors["history_length"] = 0.6
    elif trading_days >= 60:
        factors["history_length"] = 0.3
    else:
        factors["history_length"] = 0.0
        notes.append(f"{trading_days} trading day(s) of history")

    # --- execution realism ---
    if configuration.execution.timing == ExecutionTiming.SAME_BAR_CLOSE:
        factors["execution_realism"] = 0.0
        notes.append("same-bar-close execution: decisions fill at a price that "
                     "was part of the information used to make them")
    elif configuration.execution.max_participation is None:
        factors["execution_realism"] = 0.5
        notes.append("no participation cap — fills assume unlimited liquidity")
    else:
        factors["execution_realism"] = 1.0

    # --- cost realism ---
    if configuration.costs.is_zero and configuration.slippage.method == SlippageMethod.NONE:
        factors["cost_realism"] = 0.0
        notes.append("zero costs and zero slippage — results are an upper bound "
                     "that no real account could achieve")
    elif configuration.costs.is_zero or configuration.slippage.method == SlippageMethod.NONE:
        factors["cost_realism"] = 0.5
        notes.append("only one of commission or slippage is modelled")
    else:
        factors["cost_realism"] = 1.0

    # --- data coverage ---
    coverage = safe_ratio(instruments_with_data, instruments_requested)
    if coverage is None:
        factors["data_coverage"] = 0.0
    else:
        factors["data_coverage"] = max(0.0, min(1.0, coverage))
        if coverage < 0.9:
            notes.append(f"{instruments_with_data}/{instruments_requested} instruments "
                         f"had cached price history")

    # --- point-in-time integrity ---
    integrity = 1.0
    if WarningCode.RETROACTIVE_ADJUSTMENT in codes:
        integrity -= 0.4
        notes.append("prices are retroactively split/dividend-adjusted, so a "
                     "historical bar is not exactly what was quoted then")
    if WarningCode.SURVIVORSHIP_RISK in codes:
        integrity -= 0.3
        notes.append("universe membership is current, not point-in-time")
    if WarningCode.IN_SAMPLE_MODEL in codes:
        integrity -= 0.5
        notes.append("a model generated predictions for periods inside its own "
                     "training window")
    factors["point_in_time_integrity"] = max(0.0, integrity)

    # --- benchmark ---
    factors["benchmark"] = 0.0 if WarningCode.NO_BENCHMARK in codes else 1.0

    total_weight = sum(weights.get(name, 1.0) for name in factors)
    weighted = sum(value * weights.get(name, 1.0) for name, value in factors.items())
    score = finite_or_none(weighted / total_weight) if total_weight else None
    return QualityAssessment(score=score, factors=factors, notes=notes)
