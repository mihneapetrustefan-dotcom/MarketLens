"""
src/backtest/guards.py
---------------------------
Temporal defenses against look-ahead bias (Phase 12, spec §51, §52, §93).

WHY THESE ARE EXCEPTIONS, NOT WARNINGS
------------------------------------------
A backtest that peeks at the future does not produce obviously-broken
output. It produces a beautiful equity curve, a high Sharpe ratio, and
a strategy someone might fund. The failure is invisible in the result
and only detectable in the code — which is precisely why it has to be
detectable BY the code.

So every guard here raises. A run that leaks stops with a located
error naming the two timestamps that crossed, rather than finishing and
reporting numbers nobody can trust. This mirrors Phase 6's
`LookAheadViolation`, which took the same position for event studies.

THE CHAIN THIS ENFORCES
---------------------------
    feature_time  <=  information_cutoff      nothing modelled after the cutoff
    information_cutoff <= signal_time         a signal cannot precede its evidence
    signal_time   <=  order_time              an order cannot precede its signal
    order_time    <   fill_time               a fill cannot happen at its own order
    decision_time <   outcome_time            an outcome cannot precede the decision

The strict inequality on the fill is the load-bearing one. Filling at
the same timestamp as the order means executing on information that
arrived simultaneously — the classic same-bar-close error that makes
almost any strategy look profitable.

WHAT A GUARD CANNOT CATCH
-----------------------------
These check ORDERING, not provenance. If an upstream phase computed a
feature using future data and stamped it with an early timestamp, no
inequality here will notice. That protection lives where the feature is
built (Phase 8's point-in-time context) and where prices are read
(Phase 11's SQL anchor). The guards are the last line, not the only one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional


class TemporalViolation(Exception):
    """
    Raised when the historical decision chain runs backwards.

    Deliberately fatal. A run that continues past one of these is
    producing numbers whose meaning nobody can defend.
    """


def _require_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (got a naive datetime)")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be in UTC (got offset {value.utcoffset()})")
    return value


@dataclass
class TemporalGuard:
    """
    Enforces and counts the ordering constraints of one run.

    Counting matters as much as enforcing: a run reporting zero checks
    performed is not a safe run, it is an unguarded one, and the two
    are indistinguishable from the result alone. `checks_performed`
    makes the difference visible, and a test asserts it is non-zero for
    any run that traded.
    """

    run_id: str = ""
    checks_performed: int = 0
    #: Counts per check name, for the run's reproducibility record.
    by_check: Dict[str, int] = field(default_factory=dict)
    #: When False, violations are recorded instead of raised. Reserved
    #: for diagnostics that need to survey a whole run's problems; the
    #: engine always runs strict.
    strict: bool = True
    recorded: List[str] = field(default_factory=list)

    def _record(self, check: str) -> None:
        self.checks_performed += 1
        self.by_check[check] = self.by_check.get(check, 0) + 1

    def _fail(self, check: str, message: str) -> None:
        self.recorded.append(f"{check}: {message}")
        if self.strict:
            raise TemporalViolation(message)

    # ---------------- the chain ----------------

    def check_feature_not_future(self, feature_time: Optional[datetime],
                                 information_cutoff: datetime,
                                 what: str = "feature") -> None:
        """Nothing that fed a decision may be stamped after its cutoff."""
        _require_utc(information_cutoff, "information_cutoff")
        self._record("feature_not_future")
        if feature_time is None:
            return
        _require_utc(feature_time, "feature_time")
        if feature_time > information_cutoff:
            self._fail("feature_not_future",
                       f"{what} is stamped {feature_time.isoformat()}, after the "
                       f"information cutoff {information_cutoff.isoformat()}")

    def check_signal_after_information(self, signal_time: Optional[datetime],
                                       information_cutoff: Optional[datetime]) -> None:
        """A signal cannot exist before the evidence it was built from."""
        self._record("signal_after_information")
        if signal_time is None or information_cutoff is None:
            return
        _require_utc(signal_time, "signal_time")
        _require_utc(information_cutoff, "information_cutoff")
        if signal_time < information_cutoff:
            self._fail("signal_after_information",
                       f"signal at {signal_time.isoformat()} precedes its own "
                       f"information cutoff {information_cutoff.isoformat()}")

    def check_order_after_signal(self, order_time: datetime,
                                 signal_time: Optional[datetime]) -> None:
        self._record("order_after_signal")
        if signal_time is None:
            return
        _require_utc(order_time, "order_time")
        _require_utc(signal_time, "signal_time")
        if order_time < signal_time:
            self._fail("order_after_signal",
                       f"order at {order_time.isoformat()} precedes its signal "
                       f"at {signal_time.isoformat()}")

    def check_fill_after_order(self, fill_time: datetime, order_time: datetime,
                               allow_same_moment: bool = False) -> None:
        """
        A fill must follow its order.

        `allow_same_moment` exists only for the explicitly-selected
        SAME_BAR_CLOSE timing, which the configuration marks as
        unrealistic and which raises a research-quality warning on the
        run. Everywhere else the inequality is strict.
        """
        self._record("fill_after_order")
        _require_utc(fill_time, "fill_time")
        _require_utc(order_time, "order_time")
        if allow_same_moment:
            if fill_time < order_time:
                self._fail("fill_after_order",
                           f"fill at {fill_time.isoformat()} precedes its order "
                           f"at {order_time.isoformat()}")
            return
        if fill_time <= order_time:
            self._fail("fill_after_order",
                       f"fill at {fill_time.isoformat()} is not strictly after its "
                       f"order at {order_time.isoformat()} — filling on "
                       f"simultaneous information is look-ahead")

    def check_outcome_after_decision(self, outcome_time: datetime,
                                     decision_time: datetime) -> None:
        """An outcome measured at or before its decision is measuring the past."""
        self._record("outcome_after_decision")
        _require_utc(outcome_time, "outcome_time")
        _require_utc(decision_time, "decision_time")
        if outcome_time <= decision_time:
            self._fail("outcome_after_decision",
                       f"outcome at {outcome_time.isoformat()} is not after the "
                       f"decision at {decision_time.isoformat()}")

    # ---------------- data reads ----------------

    def check_bar_not_future(self, bar_time: datetime, anchor: datetime,
                             what: str = "bar") -> None:
        """A price used for valuation must not postdate the anchor."""
        self._record("bar_not_future")
        _require_utc(bar_time, "bar_time")
        _require_utc(anchor, "anchor")
        if bar_time > anchor:
            self._fail("bar_not_future",
                       f"{what} at {bar_time.isoformat()} is after the valuation "
                       f"anchor {anchor.isoformat()}")

    def check_within_horizon(self, moment: datetime, end: datetime) -> None:
        """Nothing in a run may reach past the configured end date."""
        self._record("within_horizon")
        _require_utc(moment, "moment")
        _require_utc(end, "end")
        if moment > end:
            self._fail("within_horizon",
                       f"{moment.isoformat()} is beyond the backtest end "
                       f"{end.isoformat()}")

    def check_model_trained_before(self, trained_at: Optional[datetime],
                                   information_cutoff: datetime,
                                   model_id: str = "model") -> None:
        """
        A model may not generate a historical prediction if it was
        trained on data from after that moment (spec §47, §49).

        This is the guard against the most seductive error in the whole
        phase: training once on all history and replaying it backwards.
        The resulting curve is not a backtest, it is a description of
        how well the model memorised its own training set.
        """
        self._record("model_trained_before")
        if trained_at is None:
            return
        _require_utc(trained_at, "trained_at")
        _require_utc(information_cutoff, "information_cutoff")
        if trained_at > information_cutoff:
            self._fail("model_trained_before",
                       f"{model_id} was trained at {trained_at.isoformat()}, after "
                       f"the {information_cutoff.isoformat()} decision it is being "
                       f"used for — this is in-sample replay, not a backtest")

    def summary(self) -> Dict[str, object]:
        return {
            "checks_performed": self.checks_performed,
            "by_check": dict(self.by_check),
            "violations_recorded": len(self.recorded),
            "strict": self.strict,
        }
