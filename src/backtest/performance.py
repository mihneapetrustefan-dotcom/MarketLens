"""
src/backtest/performance.py
--------------------------------
Performance measurement for a completed run
(Phase 12, spec §34-§38, §41, §42, §45, §46, §68).

FORMULAS ARE STATED, NOT IMPLIED
------------------------------------
Every metric below documents its definition, because the same word
means different things in different systems and a Sharpe ratio whose
convention is unknown cannot be compared to anything:

  simple return      (V1 - V0) / V0
  cumulative return  V_final / V_initial - 1
  CAGR               (V_final / V_initial)^(365.25 / days) - 1
  volatility         sample stdev of daily returns * sqrt(252)
  Sharpe             (annualized return - rf) / volatility
  Sortino            (annualized return - rf) / downside deviation,
                     where downside deviation counts only returns below
                     the daily risk-free rate
  Calmar             annualized return / |max drawdown|
  turnover           traded notional / average equity, annualized by
                     365.25 / days
  profit factor      gross profit / gross loss
  expectancy         mean net P&L per trade

THE RISK-FREE RATE IS NOT ASSUMED TO BE ZERO
------------------------------------------------
Spec §36 forbids blindly assuming zero. It comes from the
configuration, and the configuration records where it came from. This
database contains no risk-free series, so the shipped default IS zero —
but it is a stated, sourced default rather than a silent one, and a run
that supplies a real rate uses it throughout.

METRICS ARE ABSENT WHEN THE SAMPLE CANNOT SUPPORT THEM
----------------------------------------------------------
Spec §34: do not calculate metrics when insufficient data exists. A
Sharpe ratio from four observations is a number, not a measurement.
Each such metric is left as None with its reason recorded in
`unavailable`, exactly as Phase 11 handles unmeasurable risk.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from src.domain.backtest_models import (
    DrawdownEpisode, EquityPoint, PerformanceMetrics, Trade, finite_or_none,
    safe_ratio,
)
from src.portfolio.analytics import TRADING_DAYS_PER_YEAR, mean, sample_stdev

#: Below this many equity observations, return-series metrics are noise.
#: Matches Phase 11's volatility floor so the two layers agree about
#: what counts as measurable.
MIN_RETURN_OBSERVATIONS = 20

#: Ratios need a longer series still: a Sharpe over 20 points swings
#: wildly with one observation.
MIN_RATIO_OBSERVATIONS = 30

DAYS_PER_YEAR = 365.25


def simple_returns(points: Sequence[EquityPoint]) -> List[float]:
    """Period-over-period returns of the equity curve, ascending."""
    out: List[float] = []
    for previous, current in zip(points, points[1:]):
        if previous.equity is None or previous.equity <= 0:
            continue
        value = finite_or_none((current.equity - previous.equity) / previous.equity)
        if value is not None:
            out.append(value)
    return out


def compute_drawdown_episodes(points: Sequence[EquityPoint]) -> List[DrawdownEpisode]:
    """
    Every peak-to-trough-to-recovery cycle (spec §37, §57).

    A list rather than one number: "max drawdown 18%" says nothing
    about whether that was one bad year or twelve bad weeks, and the
    difference matters more than the depth.
    """
    usable = [p for p in points if p.equity is not None and math.isfinite(p.equity)]
    if len(usable) < 2:
        return []

    episodes: List[DrawdownEpisode] = []
    peak = usable[0]
    trough: Optional[EquityPoint] = None

    for point in usable[1:]:
        if point.equity >= peak.equity:
            if trough is not None:
                depth = (trough.equity - peak.equity) / peak.equity
                episodes.append(DrawdownEpisode(
                    peak_at=peak.timestamp, peak_equity=peak.equity,
                    trough_at=trough.timestamp, trough_equity=trough.equity,
                    depth=finite_or_none(depth) or 0.0,
                    recovered_at=point.timestamp))
                trough = None
            peak = point
            continue
        if trough is None or point.equity < trough.equity:
            trough = point

    if trough is not None and peak.equity > 0:
        depth = (trough.equity - peak.equity) / peak.equity
        episodes.append(DrawdownEpisode(
            peak_at=peak.timestamp, peak_equity=peak.equity,
            trough_at=trough.timestamp, trough_equity=trough.equity,
            depth=finite_or_none(depth) or 0.0, recovered_at=None))

    return episodes


def annotate_drawdown(points: List[EquityPoint]) -> None:
    """Stamp each point with its drawdown from the running peak (the underwater curve)."""
    peak: Optional[float] = None
    for point in points:
        if point.equity is None:
            continue
        peak = point.equity if peak is None else max(peak, point.equity)
        point.drawdown = (finite_or_none((point.equity - peak) / peak)
                          if peak and peak > 0 else None)


def period_returns(points: Sequence[EquityPoint], granularity: str = "month"
                   ) -> List[Tuple[str, float]]:
    """
    Returns bucketed by calendar period (spec §45).

    Aggregate metrics hide a strategy that worked for six months and
    then stopped; this is what makes that visible.
    """
    if len(points) < 2:
        return []

    def key_for(moment: datetime) -> str:
        if granularity == "year":
            return f"{moment.year}"
        if granularity == "quarter":
            return f"{moment.year}-Q{(moment.month - 1) // 3 + 1}"
        return f"{moment.year}-{moment.month:02d}"

    buckets: Dict[str, List[EquityPoint]] = defaultdict(list)
    for point in points:
        buckets[key_for(point.timestamp)].append(point)

    out: List[Tuple[str, float]] = []
    for key in sorted(buckets):
        bucket = buckets[key]
        first, last = bucket[0], bucket[-1]
        if first.equity and first.equity > 0:
            value = finite_or_none((last.equity - first.equity) / first.equity)
            if value is not None:
                out.append((key, value))
    return out


def rolling_metric(points: Sequence[EquityPoint], window: int,
                   metric: str = "return") -> List[Tuple[datetime, float]]:
    """
    Rolling stability series (spec §46).

    Supported metrics: "return", "volatility", "sharpe", "drawdown".
    A window longer than the data returns nothing rather than a
    degenerate single value.
    """
    if window < 2 or len(points) <= window:
        return []

    out: List[Tuple[datetime, float]] = []
    for end in range(window, len(points)):
        chunk = points[end - window:end + 1]
        if metric == "return":
            first, last = chunk[0], chunk[-1]
            if first.equity and first.equity > 0:
                value = finite_or_none((last.equity - first.equity) / first.equity)
                if value is not None:
                    out.append((last.timestamp, value))
        elif metric in ("volatility", "sharpe"):
            returns = simple_returns(chunk)
            deviation = sample_stdev(returns)
            if deviation is None:
                continue
            annualized_vol = deviation * math.sqrt(TRADING_DAYS_PER_YEAR)
            if metric == "volatility":
                value = finite_or_none(annualized_vol)
            else:
                average = mean(returns)
                value = (finite_or_none(average * TRADING_DAYS_PER_YEAR / annualized_vol)
                         if average is not None and annualized_vol > 0 else None)
            if value is not None:
                out.append((chunk[-1].timestamp, value))
        elif metric == "drawdown":
            equities = [p.equity for p in chunk if p.equity is not None]
            if not equities:
                continue
            peak = max(equities)
            value = (finite_or_none((equities[-1] - peak) / peak)
                     if peak > 0 else None)
            if value is not None:
                out.append((chunk[-1].timestamp, value))
    return out


class PerformanceEngine:
    """Turns an equity curve and a trade list into measured performance."""

    def __init__(self, risk_free_rate: float = 0.0,
                 risk_free_source: str = "assumed zero"):
        self.risk_free_rate = risk_free_rate
        self.risk_free_source = risk_free_source

    # ---------------- helpers ----------------

    def _elapsed_days(self, points: Sequence[EquityPoint]) -> Optional[float]:
        if len(points) < 2:
            return None
        span = (points[-1].timestamp - points[0].timestamp).total_seconds() / 86400.0
        return span if span > 0 else None

    # ---------------- the engine ----------------

    def compute(self, points: Sequence[EquityPoint], trades: Sequence[Trade],
                initial_capital: float, traded_notional: float = 0.0,
                total_costs: float = 0.0, total_slippage: float = 0.0,
                benchmark_points: Optional[Sequence[Tuple[datetime, float]]] = None,
                ) -> PerformanceMetrics:
        metrics = PerformanceMetrics(
            observations=len(points), initial_capital=initial_capital,
            total_costs=total_costs, total_slippage=total_slippage)

        metrics.total_trades = len(trades)
        self._trade_metrics(metrics, trades)

        if not points:
            metrics.mark_unavailable("all", "the run produced no equity observations")
            return metrics

        metrics.final_capital = points[-1].equity
        metrics.trading_days = len(points)

        if initial_capital > 0 and metrics.final_capital is not None:
            metrics.total_return = finite_or_none(
                metrics.final_capital / initial_capital - 1.0)

        elapsed = self._elapsed_days(points)
        returns = simple_returns(points)

        # --- annualized return and CAGR ---
        if (elapsed and elapsed > 0 and initial_capital > 0
                and metrics.final_capital and metrics.final_capital > 0):
            growth = metrics.final_capital / initial_capital
            metrics.cagr = finite_or_none(growth ** (DAYS_PER_YEAR / elapsed) - 1.0)
            metrics.annualized_return = metrics.cagr
        else:
            metrics.mark_unavailable(
                "cagr", "needs a positive elapsed period and positive final capital")

        # --- volatility ---
        if len(returns) < MIN_RETURN_OBSERVATIONS:
            metrics.mark_unavailable(
                "volatility",
                f"{len(returns)} return observations, minimum {MIN_RETURN_OBSERVATIONS}")
        else:
            deviation = sample_stdev(returns)
            metrics.volatility = (finite_or_none(deviation * math.sqrt(TRADING_DAYS_PER_YEAR))
                                  if deviation is not None else None)
            daily_rf = self.risk_free_rate / TRADING_DAYS_PER_YEAR
            downside = [r for r in returns if r < daily_rf]
            if len(downside) >= 2:
                downside_dev = sample_stdev(downside)
                metrics.downside_volatility = (
                    finite_or_none(downside_dev * math.sqrt(TRADING_DAYS_PER_YEAR))
                    if downside_dev is not None else None)
            else:
                metrics.mark_unavailable(
                    "downside_volatility",
                    f"only {len(downside)} return(s) fell below the risk-free rate")

        # --- risk-adjusted ratios ---
        if len(returns) < MIN_RATIO_OBSERVATIONS:
            metrics.mark_unavailable(
                "sharpe",
                f"{len(returns)} observations, minimum {MIN_RATIO_OBSERVATIONS} "
                f"for a ratio")
        elif metrics.annualized_return is not None:
            excess = metrics.annualized_return - self.risk_free_rate
            if metrics.volatility and metrics.volatility > 0:
                metrics.sharpe = finite_or_none(excess / metrics.volatility)
            if metrics.downside_volatility and metrics.downside_volatility > 0:
                metrics.sortino = finite_or_none(excess / metrics.downside_volatility)

        # --- drawdown ---
        episodes = compute_drawdown_episodes(points)
        if episodes:
            depths = [e.depth for e in episodes]
            metrics.max_drawdown = finite_or_none(min(depths))
            metrics.average_drawdown = finite_or_none(mean(depths))
            deepest = min(episodes, key=lambda e: e.depth)
            metrics.max_drawdown_duration_days = deepest.duration_days
            if (metrics.annualized_return is not None and metrics.max_drawdown
                    and metrics.max_drawdown < 0):
                metrics.calmar = finite_or_none(
                    metrics.annualized_return / abs(metrics.max_drawdown))
        else:
            metrics.mark_unavailable("max_drawdown", "no drawdown episode occurred")

        # --- exposure and cash ---
        equities = [p.equity for p in points if p.equity is not None]
        exposures = [safe_ratio(p.gross_exposure, p.equity) for p in points
                     if p.equity and p.equity > 0]
        exposures = [e for e in exposures if e is not None]
        if exposures:
            metrics.average_exposure = finite_or_none(mean(exposures))
        cash_values = [p.cash for p in points if p.cash is not None]
        if cash_values:
            metrics.average_cash = finite_or_none(mean(cash_values))

        # --- turnover ---
        average_equity = mean(equities) if equities else None
        if average_equity and average_equity > 0 and traded_notional > 0:
            metrics.turnover = finite_or_none(traded_notional / average_equity)
            if elapsed and elapsed > 0 and metrics.turnover is not None:
                metrics.annualized_turnover = finite_or_none(
                    metrics.turnover * DAYS_PER_YEAR / elapsed)
        elif traded_notional == 0:
            metrics.mark_unavailable("turnover", "no notional was traded")

        # --- benchmark ---
        self._benchmark_metrics(metrics, points, benchmark_points)
        return metrics

    # ---------------- trades ----------------

    def _trade_metrics(self, metrics: PerformanceMetrics,
                       trades: Sequence[Trade]) -> None:
        if not trades:
            metrics.mark_unavailable("trade_metrics", "the run produced no closed trades")
            return

        wins = [t for t in trades if t.net_pnl > 0]
        losses = [t for t in trades if t.net_pnl < 0]
        metrics.winning_trades = len(wins)
        metrics.losing_trades = len(losses)
        metrics.win_rate = safe_ratio(len(wins), len(trades))

        if wins:
            metrics.average_win = finite_or_none(mean([t.net_pnl for t in wins]))
            metrics.largest_win = finite_or_none(max(t.net_pnl for t in wins))
        if losses:
            metrics.average_loss = finite_or_none(mean([t.net_pnl for t in losses]))
            metrics.largest_loss = finite_or_none(min(t.net_pnl for t in losses))

        gross_profit = sum(t.net_pnl for t in wins)
        gross_loss = abs(sum(t.net_pnl for t in losses))
        if gross_loss > 0:
            metrics.profit_factor = finite_or_none(gross_profit / gross_loss)
        elif gross_profit > 0:
            # Every trade won. Reporting infinity would be arithmetically
            # true and practically meaningless, so it is recorded as
            # unmeasurable instead.
            metrics.mark_unavailable(
                "profit_factor", "no losing trades — the ratio is undefined")

        metrics.expectancy = finite_or_none(mean([t.net_pnl for t in trades]))
        metrics.average_holding_days = finite_or_none(
            mean([t.holding_days for t in trades]))

    # ---------------- benchmark ----------------

    def _benchmark_metrics(self, metrics: PerformanceMetrics,
                           points: Sequence[EquityPoint],
                           benchmark_points: Optional[Sequence[Tuple[datetime, float]]]
                           ) -> None:
        if not benchmark_points or len(benchmark_points) < 2:
            metrics.mark_unavailable(
                "benchmark", "no benchmark series was available for this period")
            return
        first, last = benchmark_points[0][1], benchmark_points[-1][1]
        if not first or first <= 0:
            metrics.mark_unavailable("benchmark", "benchmark series starts at zero")
            return
        metrics.benchmark_return = finite_or_none(last / first - 1.0)
        if metrics.total_return is not None and metrics.benchmark_return is not None:
            metrics.excess_return = finite_or_none(
                metrics.total_return - metrics.benchmark_return)

    # ---------------- methodology ----------------

    def methodology(self) -> Dict[str, str]:
        """The formulas and conventions behind every number, for the record."""
        return {
            "return": "simple period-over-period on portfolio equity",
            "cumulative": "final equity / initial capital - 1",
            "cagr": "(final/initial)^(365.25/elapsed_days) - 1",
            "volatility": f"sample stdev of daily returns * sqrt({TRADING_DAYS_PER_YEAR})",
            "sharpe": "(annualized return - risk_free) / annualized volatility",
            "sortino": "(annualized return - risk_free) / downside deviation "
                       "below the daily risk-free rate",
            "calmar": "annualized return / |max drawdown|",
            "turnover": "traded notional / average equity, annualized by 365.25/days",
            "profit_factor": "gross profit / gross loss over closed trades",
            "expectancy": "mean net P&L per closed trade",
            "risk_free_rate": f"{self.risk_free_rate:.6f} ({self.risk_free_source})",
            "minimum_observations": f"{MIN_RETURN_OBSERVATIONS} for dispersion, "
                                    f"{MIN_RATIO_OBSERVATIONS} for ratios",
        }
