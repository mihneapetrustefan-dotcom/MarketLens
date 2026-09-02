"""
src/paper/comparison.py
----------------------------
Did paper behave like the backtest said it would?
(Phase 13, spec §65, §66, §67, §83)

THE QUESTION WORTH ASKING
-----------------------------
A backtest is a claim about how a strategy behaves. Paper trading is the
first cheap opportunity to check that claim against something the
backtest did not control: real elapsed time, real data arrival, real
gaps. Where the two diverge, the backtest was wrong about something —
and knowing WHICH something is more useful than either number alone.

WHAT IS COMPARED, AND WHAT DELIBERATELY IS NOT
--------------------------------------------------
Comparable: signal frequency, fill rate, slippage, turnover, cost per
trade, order rejection rate. These are mechanical properties of the
pipeline, and a divergence points at a specific cause — data gaps,
liquidity, latency, a risk constraint binding differently.

Reported but NOT treated as evidence: return, and any ranking derived
from it. Spec §84 forbids concluding a strategy works from a short paper
period, and a few weeks of paper P&L cannot distinguish skill from
noise. `ReturnComparison` therefore carries `is_conclusive=False` and
says why, rather than being left for a reader to over-interpret.

DRIFT IS A DIAGNOSTIC, NOT A VERDICT
----------------------------------------
`detect_drift` reports where the two disagree and by how much. It does
not decide the strategy is broken — a paper fill rate below the
backtest's could mean the liquidity model was optimistic, or simply
that this fortnight was thin. It names the divergence and leaves the
judgement to someone who can look at the cause.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from src.domain.paper_models import finite_or_none, safe_ratio

#: Below this many paper trades, every comparison is anecdote. Matches
#: Phase 12's attribution floor so the two phases agree about what
#: counts as a sample.
MIN_TRADES_FOR_COMPARISON = 5

#: Relative divergence past which a metric is flagged. Generous on
#: purpose: a tight threshold on a small sample fires constantly, and
#: an alert that always fires is one nobody reads.
DRIFT_THRESHOLD = 0.50


@dataclass
class MetricComparison:
    """One metric measured in both worlds."""
    metric: str
    backtest: Optional[float] = None
    paper: Optional[float] = None
    unit: str = ""
    #: True when a difference here points at a mechanical cause worth
    #: investigating, rather than at luck.
    is_diagnostic: bool = True
    note: str = ""

    @property
    def absolute_difference(self) -> Optional[float]:
        if self.backtest is None or self.paper is None:
            return None
        return finite_or_none(self.paper - self.backtest)

    @property
    def relative_difference(self) -> Optional[float]:
        """Paper relative to backtest. None when the baseline is zero."""
        if self.backtest is None or self.paper is None or self.backtest == 0:
            return None
        return finite_or_none((self.paper - self.backtest) / abs(self.backtest))

    @property
    def has_drifted(self) -> bool:
        relative = self.relative_difference
        return relative is not None and abs(relative) > DRIFT_THRESHOLD

    @property
    def is_measurable(self) -> bool:
        return self.backtest is not None and self.paper is not None


@dataclass
class ComparisonReport:
    """
    Paper against backtest, with the caveats attached.

    `is_conclusive` is False whenever the paper sample is too small,
    which on any realistic paper period it will be. The field exists so
    the caveat travels with the numbers rather than sitting in a
    docstring nobody reads.
    """
    session_id: str
    backtest_run_id: Optional[str] = None
    at: Optional[datetime] = None
    paper_trades: int = 0
    paper_days: int = 0
    metrics: List[MetricComparison] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def is_conclusive(self) -> bool:
        """
        Deliberately almost always False.

        Spec §84 forbids concluding a strategy works from a short paper
        period. A comparison over fewer than the minimum trades, or a
        handful of days, describes what happened — it does not establish
        anything about what will.
        """
        return self.paper_trades >= MIN_TRADES_FOR_COMPARISON and self.paper_days >= 60

    @property
    def drifted(self) -> List[MetricComparison]:
        return [m for m in self.metrics if m.is_diagnostic and m.has_drifted]

    @property
    def measurable(self) -> List[MetricComparison]:
        return [m for m in self.metrics if m.is_measurable]

    def summary(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "backtest_run_id": self.backtest_run_id,
            "paper_trades": self.paper_trades,
            "paper_days": self.paper_days,
            "conclusive": self.is_conclusive,
            "metrics_compared": len(self.measurable),
            "drifted": [m.metric for m in self.drifted],
            "notes": self.notes,
            "caveat": ("a short paper period cannot establish whether a strategy "
                       "works; mechanical divergences are diagnostic, return "
                       "differences are not"),
        }


def compare(session_id: str, paper: Dict[str, Any],
            backtest: Optional[Dict[str, Any]] = None,
            backtest_run_id: Optional[str] = None,
            at: Optional[datetime] = None) -> ComparisonReport:
    """
    Build the comparison from two metric dictionaries.

    Takes plain dicts rather than the two phases' result objects so the
    function stays testable without a database and without constructing
    a full backtest — and so a caller can compare against a stored run's
    metrics exactly as the repository returns them.
    """
    report = ComparisonReport(
        session_id=session_id, backtest_run_id=backtest_run_id,
        at=at or datetime.now(timezone.utc),
        paper_trades=int(paper.get("trades") or 0),
        paper_days=int(paper.get("days") or 0))

    if backtest is None:
        report.notes.append(
            "no backtest run was supplied; paper metrics are reported alone")
        for metric, unit in (("signals_per_day", "count"), ("trades", "count"),
                             ("fill_rate", "fraction"), ("slippage_bps", "bps"),
                             ("turnover", "x"), ("cost_per_trade", "currency")):
            report.metrics.append(MetricComparison(
                metric=metric, backtest=None, paper=paper.get(metric), unit=unit))
        return report

    # --- mechanical metrics: a divergence points at a cause ---
    for metric, unit, note in (
        ("signals_per_day", "count",
         "differs when data availability or signal lifetimes differ"),
        ("fill_rate", "fraction",
         "differs when liquidity, market hours or freshness gating differ"),
        ("rejection_rate", "fraction",
         "differs when risk constraints or controls bind differently"),
        ("slippage_bps", "bps",
         "should match closely — both use the Phase 12 slippage model"),
        ("cost_per_trade", "currency",
         "should match closely — both use the Phase 12 cost model"),
        ("turnover", "x", "differs when signal frequency or sizing differ"),
        ("avg_holding_days", "days", "differs when exit timing differs"),
    ):
        report.metrics.append(MetricComparison(
            metric=metric, backtest=backtest.get(metric), paper=paper.get(metric),
            unit=unit, is_diagnostic=True, note=note))

    # --- outcome metrics: reported, never diagnostic ---
    for metric, unit in (("total_return", "fraction"), ("win_rate", "fraction"),
                         ("max_drawdown", "fraction")):
        report.metrics.append(MetricComparison(
            metric=metric, backtest=backtest.get(metric), paper=paper.get(metric),
            unit=unit, is_diagnostic=False,
            note="reported for context; a short paper sample cannot "
                 "establish a difference in outcome"))

    if not report.is_conclusive:
        report.notes.append(
            f"{report.paper_trades} paper trade(s) over {report.paper_days} day(s) — "
            f"below the {MIN_TRADES_FOR_COMPARISON} trades and 60 days this "
            f"comparison would need to mean anything")

    return report


def detect_drift(report: ComparisonReport) -> List[Dict[str, Any]]:
    """
    Where paper and backtest disagree mechanically (spec §67).

    Each finding names the metric, both values, the relative gap and the
    likely causes. It stops short of asserting which cause applies —
    that requires looking at the data, and a diagnostic that guessed
    would be worse than one that pointed.
    """
    findings: List[Dict[str, Any]] = []
    for metric in report.drifted:
        findings.append({
            "metric": metric.metric,
            "backtest": metric.backtest,
            "paper": metric.paper,
            "relative_difference": metric.relative_difference,
            "direction": ("paper is higher" if (metric.absolute_difference or 0) > 0
                          else "paper is lower"),
            "likely_causes": metric.note,
            "conclusive": False,
        })

    # Slippage and cost SHOULD agree — both phases use the same models,
    # so a divergence there means the models were configured
    # differently, which is a configuration bug rather than a market
    # observation.
    for metric in report.metrics:
        if metric.metric in ("slippage_bps", "cost_per_trade") and metric.has_drifted:
            findings.append({
                "metric": metric.metric,
                "backtest": metric.backtest,
                "paper": metric.paper,
                "relative_difference": metric.relative_difference,
                "direction": "configuration mismatch",
                "likely_causes": (
                    "both phases share the Phase 12 model, so a gap here points "
                    "at differing model VERSIONS or parameters between the runs, "
                    "not at market behaviour"),
                "conclusive": False,
            })
    return findings


def paper_metrics_from(snapshots: Sequence[Dict[str, Any]],
                       orders: Sequence[Dict[str, Any]],
                       fills: Sequence[Dict[str, Any]],
                       initial_capital: float) -> Dict[str, Any]:
    """
    Reduce a stored session to the metrics `compare` consumes.

    Computed from persisted records rather than from a live session
    object, so a finished session can be compared long after the process
    that ran it exited.
    """
    equities = [s["equity"] for s in snapshots
                if s.get("equity") is not None]
    days = 0
    if len(snapshots) >= 2:
        first = datetime.fromisoformat(snapshots[0]["at"])
        last = datetime.fromisoformat(snapshots[-1]["at"])
        days = max(0, int((last - first).total_seconds() // 86400))

    filled = [o for o in orders if o.get("state") == "filled"]
    rejected = [o for o in orders if o.get("state") == "rejected"]

    slippage_bps: List[float] = []
    for fill in fills:
        reference = fill.get("reference_price")
        if reference:
            slippage_bps.append(
                abs(fill["price"] - reference) / reference * 10_000.0)

    total_commission = sum(f.get("commission", 0.0) for f in fills)
    traded_notional = sum(abs(f["quantity"]) * f["price"] for f in fills)
    average_equity = (sum(equities) / len(equities)) if equities else None

    return {
        "trades": len(fills),
        "days": days,
        "signals_per_day": None,      # filled in by the caller from the event log
        "fill_rate": safe_ratio(len(filled), len(orders)) if orders else None,
        "rejection_rate": safe_ratio(len(rejected), len(orders)) if orders else None,
        "slippage_bps": (sum(slippage_bps) / len(slippage_bps)
                         if slippage_bps else None),
        "cost_per_trade": safe_ratio(total_commission, len(fills)) if fills else None,
        "turnover": safe_ratio(traded_notional, average_equity),
        "total_return": (safe_ratio(equities[-1] - initial_capital, initial_capital)
                         if equities else None),
        "max_drawdown": min((s["drawdown"] for s in snapshots
                             if s.get("drawdown") is not None), default=None),
    }
