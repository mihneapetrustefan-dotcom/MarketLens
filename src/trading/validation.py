"""
src/trading/validation.py
-------------------------------
What paper trading established about a strategy, and the governance
ladder it may climb (§21, §22, §38, §41).

PAPER PERFORMANCE IS EVIDENCE, NOT PROOF (§21)
--------------------------------------------------
The spec says it in those words and the code has to mean it. So:

  * there is no total score, no ranking and no ordering -- seven named
    quality dimensions and nothing that sums them (§41);
  * an unmeasured dimension is unmeasured, never assumed passed -- the
    Phase 24 lesson, and `DimensionReading.measured` defaults False;
  * `is_conclusive()` requires BOTH a real sample and every dimension
    measured, and on any record this project can currently produce it
    returns False;
  * higher recent P&L moves nothing. `compare()` reports the
    difference and refuses to declare a winner.

THE LADDER, AND WHERE IT STOPS (§38)
----------------------------------------
    RESEARCH_CANDIDATE -> BACKTEST_VALIDATED -> PAPER_ELIGIBLE
      -> PAPER_RUNNING -> PAPER_EVALUATED -> HUMAN_REVIEW
      -> LIVE_ELIGIBLE

Only three of those transitions happen automatically, and they are the
three that record a fact rather than a judgement: a validated backtest,
a session starting, a session ending. Every other step needs a named
person and a recorded reason, and `LIVE_ELIGIBLE` needs one this
codebase will not provide -- `assert_transition` refuses it, `review()`
refuses it, and there is no function anywhere in `src/trading` that
sets it.

THE JOIN TO PHASE 24
------------------------
A challenger becomes eligible for paper by reaching Phase 24's
`PAPER_CANDIDATE`, which itself required a named reviewer and a
recorded reason. Phase 25 does not add a new challenger state and does
not reach into `challengers` to change one; it READS the status and
refuses anything below it.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.domain.trading_loop_models import (
    LOOP_METHOD_VERSION, DimensionReading, PaperStrategyState, PaperValidation,
    PromotionRefused, QualityDimension, assert_transition, require_utc,
)
from src.trading.repository import TradingLoopRepository

#: The same effective-sample floor every phase since Phase 9 has used.
#: Imported rather than restated wherever possible; here it is named
#: once because `ModelEvaluation` is a modelling type and this is not a
#: model.
MIN_PAPER_TRADES = 30

#: Phase 24 statuses a challenger may enter paper from. Exactly one,
#: and it is the one a person had to sign.
PAPER_ELIGIBLE_CHALLENGER_STATUSES = ("paper_candidate",)


class NotEligibleForPaper(Exception):
    """Raised when something asks to paper-trade a challenger that may not."""


def validation_id_for(strategy_id: str, strategy_version: str,
                      session_id: str,
                      method_version: str = LOOP_METHOD_VERSION) -> str:
    """
    Deterministic identity: one validation per strategy version per session.

    A second `start()` for the same three therefore updates the record
    rather than creating a rival one -- the mistake Phase 23 made with
    observation ids, which collapsed four findings into one and
    reported the wrong count.
    """
    raw = f"{method_version}|{strategy_id}|{strategy_version}|{session_id}"
    return "pv-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def challenger_status(conn: sqlite3.Connection,
                      challenger_id: str) -> Optional[str]:
    """
    The latest status Phase 24 recorded for a challenger.

    Returns None when the challenger tables do not exist, which is
    different from a challenger that exists and is not approved --
    `assert_challenger_eligible` reports the two differently.
    """
    try:
        row = conn.execute("""
            SELECT status FROM challengers
             WHERE challenger_id = ?
             ORDER BY version DESC LIMIT 1
        """, (challenger_id,)).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error).lower():
            return None
        raise
    return str(row[0]) if row else None


def assert_challenger_eligible(conn: sqlite3.Connection,
                               challenger_id: str) -> str:
    """
    Refuse to paper-trade a challenger nobody approved for paper (§38).

    Phase 24's `PAPER_CANDIDATE` already required a named reviewer and
    a recorded reason. Re-deriving that judgement here would create a
    second authority on the same question, so this reads the status and
    refuses everything else.
    """
    status = challenger_status(conn, challenger_id)
    if status is None:
        raise NotEligibleForPaper(
            f"challenger {challenger_id} is unknown to this database; Phase 24 "
            f"has no record of it, so nothing has approved it for paper")
    if status not in PAPER_ELIGIBLE_CHALLENGER_STATUSES:
        raise NotEligibleForPaper(
            f"challenger {challenger_id} is {status.upper()}. Paper trading "
            f"requires PAPER_CANDIDATE, which a named reviewer records in "
            f"Phase 24 -- it is not something this phase may grant.")
    return status


class PaperValidator:
    """
    Opens, updates and closes the paper record for one strategy.

    Holds no thresholds of its own beyond `MIN_PAPER_TRADES`, and
    produces no verdict. What it produces is a record that a person or
    a later phase can read, with the parts nobody measured clearly
    marked as such.
    """

    def __init__(self, conn: sqlite3.Connection,
                 method_version: str = LOOP_METHOD_VERSION):
        self.conn = conn
        self.method_version = method_version
        self.repository = TradingLoopRepository(conn, method_version)
        self.repository.initialize()

    # ---------------- lifecycle ----------------

    def start(self, *, strategy_id: str, strategy_version: str,
              session_id: str, at: datetime,
              challenger_id: Optional[str] = None,
              baseline_id: Optional[str] = None,
              baseline_version: Optional[str] = None,
              configuration_fingerprint: str = "") -> PaperValidation:
        """
        Open a paper validation record.

        A challenger id is CHECKED, not trusted: `assert_challenger_eligible`
        raises unless Phase 24 recorded PAPER_CANDIDATE. That is the
        §38 boundary, and putting it here means a caller cannot start a
        run for an unapproved challenger by supplying the id.
        """
        require_utc(at, "at")
        if challenger_id:
            assert_challenger_eligible(self.conn, challenger_id)

        validation = PaperValidation(
            validation_id=validation_id_for(strategy_id, strategy_version,
                                            session_id, self.method_version),
            strategy_id=strategy_id, strategy_version=strategy_version,
            session_id=session_id, method_version=self.method_version,
            baseline_id=baseline_id, baseline_version=baseline_version,
            challenger_id=challenger_id,
            state=PaperStrategyState.PAPER_ELIGIBLE,
            started_at=at,
            configuration_fingerprint=configuration_fingerprint)
        assert_transition(PaperStrategyState.PAPER_ELIGIBLE,
                          PaperStrategyState.PAPER_RUNNING)
        validation.state = PaperStrategyState.PAPER_RUNNING
        self.repository.save_validation(validation)
        self.repository.audit("paper-validator", "validation_started", at,
                              session_id=session_id,
                              subject_id=validation.validation_id,
                              detail=f"{strategy_id}@{strategy_version}")
        return validation

    def update(self, validation: PaperValidation, cycles: Sequence[Any],
               *, outcomes: Sequence[Dict[str, Any]] = (),
               risk_violations: int = 0,
               operational_failures: int = 0,
               at: Optional[datetime] = None) -> PaperValidation:
        """
        Fold a set of cycles into the running record (§21).

        Counts come from the cycles rather than being incremented as
        the loop runs, so a re-run recomputes the same totals instead
        of doubling them. The same reason Phase 22 keyed experiments on
        their inputs.
        """
        validation.signals_seen = sum(c.get("signals_seen", 0) for c in cycles)
        validation.decisions = sum(c.get("targets_set", 0) for c in cycles)
        validation.orders = sum(c.get("orders_submitted", 0) for c in cycles)
        validation.rejected_orders = sum(c.get("orders_rejected", 0)
                                         for c in cycles)
        validation.fills = sum(c.get("fills_recorded", 0) for c in cycles)
        validation.risk_violations = risk_violations
        validation.operational_failures = operational_failures

        closed = [o for o in outcomes if not o.get("is_open")]
        validation.completed_trades = len(closed)
        realized = [o.get("net_pnl") for o in closed
                    if o.get("net_pnl") is not None]
        validation.realized_pnl = sum(realized) if realized else None
        unrealized = [o.get("gross_pnl") for o in outcomes
                      if o.get("is_open") and o.get("gross_pnl") is not None]
        validation.unrealized_pnl = sum(unrealized) if unrealized else None
        validation.turnover = sum(
            abs(float(o.get("quantity") or 0.0))
            * float(o.get("entry_price") or 0.0) for o in outcomes) or None

        validation.dimensions = measure_dimensions(validation, outcomes)
        self.repository.save_validation(validation)
        return validation

    def finish(self, validation: PaperValidation, at: datetime,
               note: str = "") -> PaperValidation:
        """
        Close the run. PAPER_RUNNING -> PAPER_EVALUATED, and no further.

        The next step, HUMAN_REVIEW, is not reachable from here:
        `AUTOMATIC_TRANSITIONS[PAPER_EVALUATED]` is empty, and
        `assert_transition` would refuse. A person calls `review()`.
        """
        require_utc(at, "at")
        assert_transition(validation.state, PaperStrategyState.PAPER_EVALUATED)
        validation.state = PaperStrategyState.PAPER_EVALUATED
        validation.ended_at = at
        if note:
            validation.notes = note
        self.repository.save_validation(validation)
        self.repository.audit("paper-validator", "validation_finished", at,
                              session_id=validation.session_id,
                              subject_id=validation.validation_id,
                              detail=note)
        return validation

    # ---------------- the human step ----------------

    def review(self, validation_id: str, *, to_state: PaperStrategyState,
               reviewer: str, reason: str, at: datetime) -> str:
        """
        Record a person's decision. Append-only.

        `reviewer` and `reason` have no defaults, exactly as Phase 18's
        `promote()` and Phase 24's `review()` -- an approval nobody
        signed is not an approval. Changing your mind writes a second
        row; nothing is ever updated in place.

        LIVE_ELIGIBLE is refused here as well as in `assert_transition`,
        because this is the function a future developer would reach for
        first and the refusal should be where they are looking.
        """
        require_utc(at, "at")
        if to_state.is_beyond_paper:
            raise PromotionRefused(
                "LIVE_ELIGIBLE cannot be recorded by this system. Phase 25 "
                "validates in paper and stops there; moving past it is a "
                "decision that needs a mechanism this codebase does not have.")
        current = self.repository.get_validation(validation_id)
        if current is None:
            raise ValueError(f"no paper validation {validation_id!r}")
        if current["state"] == PaperStrategyState.PAPER_RUNNING.value:
            raise ValueError(
                "this run is still PAPER_RUNNING. Finish it before reviewing "
                "it, so the review is about a record that stopped changing.")
        return self.repository.save_review(
            validation_id, from_state=current["state"],
            to_state=to_state.value, reviewer=reviewer, reason=reason, at=at)


# ======================================================================
# The seven dimensions (§41)
# ======================================================================

def measure_dimensions(validation: PaperValidation,
                       outcomes: Sequence[Dict[str, Any]]
                       ) -> List[DimensionReading]:
    """
    Measure what the record supports, and say which it does not.

    Every dimension §41 names gets a reading. A reading with
    `measured=False` carries the REASON it could not be measured, which
    is the difference between "we looked and found nothing" and "we
    could not look" -- the distinction Phases 23 and 24 both had to
    learn the hard way.

    Nothing here combines dimensions. There is no weighted sum, no
    ranking, and `PaperValidation` has no field one could be stored in.
    """
    readings: List[DimensionReading] = []
    closed = [o for o in outcomes if not o.get("is_open")]

    # --- model: not measurable from execution data ----------------
    # Model quality is Phase 18's question, answered against held-out
    # data, not against a handful of paper trades. Claiming to measure
    # it here from P&L would be exactly the collapse §41 forbids.
    readings.append(DimensionReading(
        QualityDimension.MODEL, measured=False,
        detail="model quality is Phase 18's evaluation against held-out "
               "data; paper P&L is not a substitute and is not used as one"))

    # --- signal: conversion from signal to order ------------------
    if validation.signals_seen:
        readings.append(DimensionReading(
            QualityDimension.SIGNAL, measured=True,
            value=validation.signal_to_trade_rate,
            sample_size=validation.signals_seen,
            detail=f"{validation.orders} order(s) from "
                   f"{validation.signals_seen} signal(s) seen"))
    else:
        readings.append(DimensionReading(
            QualityDimension.SIGNAL, measured=False,
            detail="no signal reached the loop"))

    # --- portfolio: did targets become positions ------------------
    if validation.decisions:
        readings.append(DimensionReading(
            QualityDimension.PORTFOLIO, measured=True,
            value=(validation.orders / validation.decisions),
            sample_size=validation.decisions,
            detail=f"{validation.orders} order(s) from "
                   f"{validation.decisions} target(s)"))
    else:
        readings.append(DimensionReading(
            QualityDimension.PORTFOLIO, measured=False,
            detail="no portfolio target was produced"))

    # --- risk: violations per order -------------------------------
    if validation.orders:
        readings.append(DimensionReading(
            QualityDimension.RISK, measured=True,
            value=validation.risk_violations / validation.orders,
            sample_size=validation.orders,
            detail=f"{validation.risk_violations} violation(s) over "
                   f"{validation.orders} order(s)"))
    else:
        readings.append(DimensionReading(
            QualityDimension.RISK, measured=False,
            detail="no order was placed, so no risk behaviour was exercised"))

    # --- execution: slippage against the decision price -----------
    slippages = [o.get("slippage_bps") for o in outcomes
                 if o.get("slippage_bps") is not None]
    if slippages:
        readings.append(DimensionReading(
            QualityDimension.EXECUTION, measured=True,
            value=sum(slippages) / len(slippages),
            sample_size=len(slippages),
            detail=f"mean slippage {sum(slippages) / len(slippages):.1f} bps "
                   f"over {len(slippages)} fill(s)"))
    else:
        readings.append(DimensionReading(
            QualityDimension.EXECUTION, measured=False,
            detail="no fill carries both a decision price and a fill price"))

    # --- strategy: realised return, and only with a real sample ---
    if len(closed) >= MIN_PAPER_TRADES and validation.realized_pnl is not None:
        readings.append(DimensionReading(
            QualityDimension.STRATEGY, measured=True,
            value=validation.realized_pnl, sample_size=len(closed),
            detail=f"realised P&L over {len(closed)} closed trade(s)"))
    else:
        readings.append(DimensionReading(
            QualityDimension.STRATEGY, measured=False,
            detail=(f"{len(closed)} closed trade(s); a strategy claim needs "
                    f"at least {MIN_PAPER_TRADES}, and a smaller sample would "
                    f"be a story about noise")))

    # --- operational: cycles that failed --------------------------
    readings.append(DimensionReading(
        QualityDimension.OPERATIONAL, measured=True,
        value=float(validation.operational_failures),
        sample_size=validation.orders + validation.rejected_orders,
        detail=f"{validation.operational_failures} operational failure(s)"))

    return readings


# ======================================================================
# Baseline comparison (§22)
# ======================================================================

@dataclass
class Comparison:
    """
    Two paper records, side by side, with no winner declared.

    Deliberately has no `better`, no `winner` and no ordering. §22 says
    avoid naive ranking, and the way to avoid it is to have nothing
    that could rank. A reader who wants to choose has both records and
    the list of what neither measured.
    """
    challenger: Dict[str, Any]
    baseline: Dict[str, Any]
    differences: Dict[str, Optional[float]]
    unmeasured_either: List[str]
    conclusive: bool
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"challenger": self.challenger, "baseline": self.baseline,
                "differences": self.differences,
                "unmeasured_either": self.unmeasured_either,
                "conclusive": self.conclusive, "note": self.note}


def compare(challenger: Dict[str, Any],
            baseline: Dict[str, Any]) -> Comparison:
    """
    Report the difference between two paper records. Nothing more.

    `conclusive` is True only when BOTH records are conclusive on their
    own terms, and on anything this project can currently produce it is
    False -- because no paper run has 30 closed trades and no run has
    every dimension measured.
    """
    fields = ("realized_pnl", "max_drawdown", "orders", "fills",
              "completed_trades", "rejected_orders", "risk_violations",
              "operational_failures")
    differences: Dict[str, Optional[float]] = {}
    for field_name in fields:
        left = challenger.get(field_name)
        right = baseline.get(field_name)
        if left is None or right is None:
            differences[field_name] = None
            continue
        differences[field_name] = float(left) - float(right)

    unmeasured = sorted(set(challenger.get("unmeasured") or [])
                        | set(baseline.get("unmeasured") or []))
    conclusive = bool(challenger.get("conclusive")) and bool(
        baseline.get("conclusive"))
    note = ("" if conclusive else
            "neither record supports a claim about which strategy is better: "
            + (f"{len(unmeasured)} dimension(s) unmeasured" if unmeasured
               else "the sample is too small"))
    return Comparison(challenger=challenger, baseline=baseline,
                      differences=differences, unmeasured_either=unmeasured,
                      conclusive=conclusive, note=note)
