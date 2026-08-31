"""
src/backtest/robustness.py
-------------------------------
Is the result fragile? (Phase 12, spec §60-§65, §48, §62, §63)

THE QUESTION THIS MODULE ASKS
---------------------------------
Not "did the strategy make money" but "does it still make money when
one assumption moves". A result that survives 0 to 20 bps of cost is a
different object from one that only works at zero — and the second is
far more common than its authors expect.

Every scenario is a SEPARATE, fully-recorded run (spec §101). Nothing
here rescales an existing result to approximate what a different cost
would have produced: costs change which orders are affordable, which
changes the positions, which changes everything downstream. Scaling the
final number would be a plausible-looking lie.

ON RESAMPLING, AND WHAT IT IS VALID FOR
-------------------------------------------
Spec §64 warns against misusing resampling on dependent time-series
data, and the warning is the important part.

`bootstrap_trades` resamples CLOSED TRADES, not daily returns, and
reports a distribution of terminal outcomes. That is defensible only
under an assumption it states out loud: that trade outcomes are
approximately independent of each other. For a portfolio holding
several correlated positions simultaneously, they are NOT independent,
and the resulting interval will be too narrow.

So the function returns its assumption alongside its numbers, and
refuses to run on fewer than 30 trades. Bootstrapping the daily equity
series is deliberately NOT offered: it would destroy the
autocorrelation and volatility clustering that make drawdowns what they
are, and produce a confidently wrong risk estimate.

WALK-FORWARD REUSES PHASE 9
-------------------------------
`WalkForwardSplitter` already exists, with purging and embargo, built
for exactly this. This module generates windows through it rather than
writing a second splitter that could disagree with the first.
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from src.domain.backtest_models import (
    BacktestConfiguration, BacktestResult, CostModel, SlippageMethod,
    SlippageModel, finite_or_none,
)
from src.modeling.splits import WalkForwardSplitter
from src.portfolio.analytics import mean, percentile, sample_stdev

#: Below this many trades a bootstrap describes the sample, not the
#: process it came from.
MIN_TRADES_FOR_BOOTSTRAP = 30


@dataclass
class ScenarioResult:
    """One perturbed run, kept whole rather than reduced to a number."""
    label: str
    dimension: str
    value: str
    result: BacktestResult

    @property
    def total_return(self) -> Optional[float]:
        return self.result.metrics.total_return

    @property
    def sharpe(self) -> Optional[float]:
        return self.result.metrics.sharpe

    @property
    def trades(self) -> int:
        return self.result.metrics.total_trades


@dataclass
class SensitivityReport:
    """
    A family of runs across one perturbed dimension.

    `is_fragile` is the summary judgement, and it is deliberately
    conservative: a strategy whose sign flips across the tested range
    is fragile regardless of how good the base case looked.
    """
    dimension: str
    scenarios: List[ScenarioResult] = field(default_factory=list)
    note: str = ""

    @property
    def returns(self) -> List[float]:
        return [s.total_return for s in self.scenarios if s.total_return is not None]

    @property
    def spread(self) -> Optional[float]:
        values = self.returns
        if len(values) < 2:
            return None
        return finite_or_none(max(values) - min(values))

    @property
    def flips_sign(self) -> bool:
        values = self.returns
        return bool(values) and min(values) < 0 < max(values)

    @property
    def is_fragile(self) -> Optional[bool]:
        if len(self.returns) < 2:
            return None
        return self.flips_sign

    def summary(self) -> Dict[str, object]:
        return {
            "dimension": self.dimension,
            "scenarios": len(self.scenarios),
            "returns": self.returns,
            "spread": self.spread,
            "flips_sign": self.flips_sign,
            "fragile": self.is_fragile,
            "note": self.note,
        }


#: A function that runs one configuration and returns its result. The
#: caller supplies it so this module never constructs an engine itself
#: and stays testable without a database.
RunnerFn = Callable[[BacktestConfiguration], BacktestResult]


class RobustnessHarness:
    """Runs one configuration under systematically perturbed assumptions."""

    def __init__(self, runner: RunnerFn):
        self.runner = runner

    # ---------------- costs ----------------

    def cost_sensitivity(self, base: BacktestConfiguration,
                         bps_levels: Sequence[float] = (0.0, 5.0, 10.0, 20.0)
                         ) -> SensitivityReport:
        """
        The same strategy at several commission levels (spec §60).

        The zero-cost run is included on purpose: comparing it to the
        realistic ones shows how much of the result was ever reachable.
        """
        report = SensitivityReport(
            dimension="commission_bps",
            note="each level is a full re-run; costs change which orders are "
                 "affordable, so results are not rescalable from one another")
        for bps in bps_levels:
            config = replace(
                base,
                name=f"{base.name} [cost {bps:g}bps]",
                costs=CostModel(version=f"cost-{bps:g}bps",
                                commission_bps=bps,
                                commission_per_share=base.costs.commission_per_share,
                                minimum_commission=base.costs.minimum_commission,
                                fee_bps=base.costs.fee_bps))
            report.scenarios.append(ScenarioResult(
                label=f"{bps:g} bps", dimension="commission_bps",
                value=f"{bps:g}", result=self.runner(config)))
        return report

    # ---------------- slippage ----------------

    def slippage_sensitivity(self, base: BacktestConfiguration,
                             bps_levels: Sequence[float] = (0.0, 5.0, 15.0, 30.0)
                             ) -> SensitivityReport:
        report = SensitivityReport(
            dimension="slippage_bps",
            note="fixed-bps slippage at each level; the zero case is an "
                 "unreachable upper bound")
        for bps in bps_levels:
            method = SlippageMethod.NONE if bps == 0 else SlippageMethod.FIXED_BPS
            config = replace(
                base,
                name=f"{base.name} [slip {bps:g}bps]",
                slippage=SlippageModel(version=f"slip-{bps:g}bps",
                                       method=method, base_bps=bps))
            report.scenarios.append(ScenarioResult(
                label=f"{bps:g} bps", dimension="slippage_bps",
                value=f"{bps:g}", result=self.runner(config)))
        return report

    # ---------------- parameters ----------------

    def parameter_sensitivity(self, base: BacktestConfiguration,
                              parameter: str,
                              values: Sequence[object]) -> SensitivityReport:
        """
        Perturb one named configuration field (spec §62).

        Deliberately one field at a time over a caller-supplied list.
        Spec §62 warns against uncontrolled sweeps, and an interface
        that only accepts an explicit list makes an accidental
        thousand-run grid search awkward rather than easy — while every
        run it does produce is preserved (spec §85).
        """
        if not hasattr(base, parameter):
            raise ValueError(f"{parameter!r} is not a configuration field")
        report = SensitivityReport(
            dimension=parameter,
            note="one field varied; all other assumptions held fixed")
        for value in values:
            config = replace(base, **{
                parameter: value,
                "name": f"{base.name} [{parameter}={value}]",
            })
            report.scenarios.append(ScenarioResult(
                label=f"{parameter}={value}", dimension=parameter,
                value=str(value), result=self.runner(config)))
        return report

    # ---------------- periods ----------------

    def period_sensitivity(self, base: BacktestConfiguration,
                           windows: Sequence[Tuple[datetime, datetime]]
                           ) -> SensitivityReport:
        """
        The same strategy over different sub-periods (spec §63).

        A strategy that only works in one window is visible here and
        nowhere in the aggregate numbers.
        """
        report = SensitivityReport(
            dimension="period",
            note="sub-periods of the base configuration; each is an independent run")
        for start, end in windows:
            if start >= end:
                continue
            config = replace(
                base, start=start, end=end,
                name=f"{base.name} [{start.date()}..{end.date()}]")
            report.scenarios.append(ScenarioResult(
                label=f"{start.date()}..{end.date()}", dimension="period",
                value=f"{start.isoformat()}/{end.isoformat()}",
                result=self.runner(config)))
        return report


# ============================================================
# Walk-forward
# ============================================================

def walk_forward_windows(start: datetime, end: datetime,
                         train_months: int = 6, test_months: int = 2,
                         step_months: int = 2, label_horizon_days: float = 5.0,
                         embargo_days: float = 1.0,
                         expanding: bool = True) -> List[Dict[str, datetime]]:
    """
    Train/test boundaries for walk-forward evaluation (spec §48).

    Delegates to Phase 9's `WalkForwardSplitter`, which already applies
    purging and embargo around each boundary. Writing a second splitter
    here would risk the backtest and the model evaluation disagreeing
    about what "out of sample" means — and they must not.

    The defaults are shorter than Phase 9's (6/2/2 months rather than
    36/6/6) because this database holds roughly nine months of price
    history. A 36-month training window would produce zero windows,
    which is a correct but useless answer.
    """
    splitter = WalkForwardSplitter(
        label_horizon_days=label_horizon_days, embargo_days=embargo_days,
        train_months=train_months, test_months=test_months,
        step_months=step_months, expanding=expanding)
    return splitter.generate_windows(start, end)


def walk_forward_configurations(base: BacktestConfiguration,
                                windows: Sequence[Dict[str, datetime]]
                                ) -> List[Tuple[Dict[str, datetime], BacktestConfiguration]]:
    """
    One configuration per TEST window.

    Only the test window is backtested. Running the training window
    through the simulator too would report performance on the data the
    model was fitted to, which is the in-sample number spec §47 insists
    must stay distinguishable from the out-of-sample one.
    """
    out: List[Tuple[Dict[str, datetime], BacktestConfiguration]] = []
    for window in windows:
        config = replace(
            base, start=window["test_start"], end=window["test_end"],
            name=f"{base.name} [oos {window['test_start'].date()}]")
        out.append((window, config))
    return out


# ============================================================
# Resampling
# ============================================================

@dataclass
class BootstrapResult:
    """
    A resampled distribution of outcomes, carrying its own assumption.

    `assumption` is a field rather than documentation because this
    number is easy to quote out of context, and the caveat has to
    travel with it.
    """
    iterations: int
    seed: int
    method: str
    assumption: str
    observations: int
    mean_total_pnl: Optional[float] = None
    stdev_total_pnl: Optional[float] = None
    percentile_5: Optional[float] = None
    percentile_50: Optional[float] = None
    percentile_95: Optional[float] = None
    probability_of_loss: Optional[float] = None
    insufficient_data: bool = False
    note: str = ""


def bootstrap_trades(trade_pnls: Sequence[float], iterations: int = 1000,
                     seed: int = 0) -> BootstrapResult:
    """
    Resample closed-trade P&L with replacement (spec §64, §65).

    Answers "how much of this result could be luck in the ORDER and MIX
    of trades" — not "what will happen next".

    The seed is recorded and fixed, so the same inputs reproduce the
    same distribution (spec §65). Fewer than 30 trades returns
    insufficient_data rather than a confident-looking interval built
    from a handful of observations.
    """
    result = BootstrapResult(
        iterations=iterations, seed=seed, method="iid trade bootstrap",
        assumption=("closed-trade outcomes are approximately independent; "
                    "with several correlated positions held at once they are "
                    "not, and the interval below is then too narrow"),
        observations=len(trade_pnls))

    values = [v for v in trade_pnls if v is not None and finite_or_none(v) is not None]
    if len(values) < MIN_TRADES_FOR_BOOTSTRAP:
        result.insufficient_data = True
        result.note = (f"{len(values)} trades, minimum {MIN_TRADES_FOR_BOOTSTRAP} "
                       f"for a resampled distribution")
        return result

    generator = random.Random(seed)
    totals: List[float] = []
    count = len(values)
    for _ in range(max(1, iterations)):
        draw = sum(values[generator.randrange(count)] for _ in range(count))
        finite = finite_or_none(draw)
        if finite is not None:
            totals.append(finite)

    if not totals:
        result.insufficient_data = True
        result.note = "resampling produced no finite totals"
        return result

    totals.sort()
    result.mean_total_pnl = finite_or_none(mean(totals))
    result.stdev_total_pnl = sample_stdev(totals)
    result.percentile_5 = percentile(totals, 0.05)
    result.percentile_50 = percentile(totals, 0.50)
    result.percentile_95 = percentile(totals, 0.95)
    result.probability_of_loss = finite_or_none(
        sum(1 for t in totals if t < 0) / len(totals))
    return result
