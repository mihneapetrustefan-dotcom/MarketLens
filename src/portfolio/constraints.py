"""
src/portfolio/constraints.py
---------------------------------
The risk limits, in one versioned place (Phase 11, spec §18–§20, §42).

WHY A SET WITH A VERSION, RATHER THAN CONSTANTS
---------------------------------------------------
Spec §19 forbids hard-coding limits throughout the application, and the
reason is auditability rather than tidiness. Every RiskDecision records
the constraint-set version that produced it. Raising the sector cap
from 40% to 45% next month therefore does not retroactively make last
month's rejection look wrong — that decision still names v1, and
replaying it under v1 reproduces it exactly.

Scattering `if weight > 0.20` across the codebase would make that
impossible, and would also guarantee that the day someone needs to
loosen a limit, they loosen it in one of the four places it appears.

HARD VERSUS SOFT
--------------------
A HARD breach rejects. A SOFT breach does not pass quietly — it
records a violation and downgrades the decision to REQUIRES_REVIEW.

The distinction exists because a system where every threshold is a hard
stop gets "fixed" in practice by quietly widening the thresholds, which
destroys the information they carried. Limits that describe a
genuinely unacceptable state (leverage, single-position size) are hard.
Limits that describe a state worth a human look (unusual volatility, a
concentrated but defensible book) are soft.

THE DEFAULTS ARE STARTING POINTS, NOT FINDINGS
--------------------------------------------------
Every number below is a conventional, defensible default. None of them
was tuned against this project's outcomes, and they should not be
described as optimal: with 1,248 verified legacy recommendations and
zero actionable signals, tuning thresholds against results would be
fitting noise. This is the same position Phase 10's ValidationConfig
took, for the same reason.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from src.domain.portfolio_models import (
    ConstraintScope, ConstraintSeverity, ConstraintSet, RiskConstraint, TradingState,
)

DEFAULT_CONSTRAINT_VERSION = "v1"


def default_constraint_set() -> ConstraintSet:
    """
    The shipped v1 limits.

    Each entry states what it protects against, so a later reader can
    judge whether the number still serves that purpose rather than
    guessing why it was chosen.
    """
    return ConstraintSet(
        version=DEFAULT_CONSTRAINT_VERSION,
        name="default",
        trading_state=TradingState.ENABLED,
        constraints=[
            # ---- position level ----
            RiskConstraint(
                constraint_id="max_position_weight",
                scope=ConstraintScope.POSITION_WEIGHT,
                severity=ConstraintSeverity.HARD,
                max_value=0.20,
                description=("No single instrument above 20% of equity. Caps the "
                             "damage any one wrong call can do."),
            ),
            # ---- sector / asset class ----
            RiskConstraint(
                constraint_id="max_sector_weight",
                scope=ConstraintScope.SECTOR_WEIGHT,
                severity=ConstraintSeverity.HARD,
                max_value=0.40,
                description=("No sector above 40% of equity. Several positions that "
                             "move together are one bet, and position-level caps "
                             "alone will not catch that."),
            ),
            RiskConstraint(
                constraint_id="max_asset_class_weight",
                scope=ConstraintScope.ASSET_CLASS_WEIGHT,
                severity=ConstraintSeverity.SOFT,
                max_value=0.60,
                description=("No asset class above 60%. Soft: a deliberately "
                             "equity-only book is legitimate and should prompt a "
                             "look, not a rejection."),
            ),
            # ---- portfolio level ----
            RiskConstraint(
                constraint_id="max_gross_exposure",
                scope=ConstraintScope.GROSS_EXPOSURE,
                severity=ConstraintSeverity.HARD,
                max_value=1.50,
                description=("Gross exposure at most 1.5x equity. Long and short "
                             "legs both consume this."),
            ),
            RiskConstraint(
                constraint_id="max_net_exposure",
                scope=ConstraintScope.NET_EXPOSURE,
                severity=ConstraintSeverity.HARD,
                max_value=1.00,
                description="Net directional exposure at most 1.0x equity.",
            ),
            RiskConstraint(
                constraint_id="max_leverage",
                scope=ConstraintScope.LEVERAGE,
                severity=ConstraintSeverity.HARD,
                max_value=1.50,
                description=("Borrowing limit. Redundant with gross exposure while "
                             "cash is non-negative, and kept separate because it "
                             "stops being redundant the moment it is not."),
            ),
            # ---- concentration and measured risk ----
            RiskConstraint(
                constraint_id="max_concentration_hhi",
                scope=ConstraintScope.CONCENTRATION_HHI,
                severity=ConstraintSeverity.SOFT,
                max_value=0.25,
                description=("HHI at most 0.25 — an effective breadth of at least "
                             "4 positions. Soft: a small book is concentrated by "
                             "arithmetic, not by poor judgement."),
            ),
            RiskConstraint(
                constraint_id="max_portfolio_volatility",
                scope=ConstraintScope.PORTFOLIO_VOLATILITY,
                severity=ConstraintSeverity.SOFT,
                max_value=0.40,
                description=("Annualized volatility at most 40%. Soft because it is "
                             "a measurement of a past window under current weights, "
                             "not a forecast."),
            ),
            RiskConstraint(
                constraint_id="max_drawdown",
                scope=ConstraintScope.DRAWDOWN,
                severity=ConstraintSeverity.SOFT,
                max_value=0.25,
                description=("Flags once observed drawdown passes 25%. Requires real "
                             "snapshot history; silent until that exists."),
            ),
            # ---- inputs ----
            RiskConstraint(
                constraint_id="min_signal_confidence",
                scope=ConstraintScope.MIN_SIGNAL_CONFIDENCE,
                severity=ConstraintSeverity.HARD,
                min_value=0.40,
                description=("A signal must reach 0.40 confidence before it may add "
                             "exposure. Deliberately stricter than Phase 10's 0.25 "
                             "generation floor: being worth recording and being "
                             "worth money are different bars."),
            ),
            RiskConstraint(
                constraint_id="max_liquidity_participation",
                scope=ConstraintScope.MIN_LIQUIDITY,
                severity=ConstraintSeverity.SOFT,
                max_value=0.10,
                description=("A position should stay under 10% of average daily "
                             "volume. Beyond that, exiting moves the price against "
                             "you."),
            ),
        ],
    )


# ============================================================
# Persistence
# ============================================================

class ConstraintRepository:
    """Loads and stores constraint sets so a decision's version can be resolved later."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def save(self, constraint_set: ConstraintSet) -> None:
        self.conn.execute("""
            INSERT OR REPLACE INTO risk_constraint_sets
            (version, name, trading_state, created_at, description)
            VALUES (?,?,?,?,?)
        """, (constraint_set.version, constraint_set.name,
              constraint_set.trading_state.value,
              datetime.now(timezone.utc).isoformat(), ""))

        for constraint in constraint_set.constraints:
            self.conn.execute("""
                INSERT OR REPLACE INTO risk_constraints
                (constraint_set_version, constraint_id, scope, severity,
                 max_value, min_value, applies_to, description, enabled)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (constraint_set.version, constraint.constraint_id,
                  constraint.scope.value, constraint.severity.value,
                  constraint.max_value, constraint.min_value,
                  constraint.applies_to, constraint.description,
                  int(constraint.enabled)))
        self.conn.commit()

    def load(self, version: str) -> Optional[ConstraintSet]:
        """
        Rebuild a stored set, or None when that version was never
        recorded.

        None rather than a fallback to the current defaults: silently
        substituting today's limits for a version that is missing would
        make a replay claim to reproduce a decision it actually
        re-decided under different rules.
        """
        row = self.conn.execute(
            "SELECT version, name, trading_state FROM risk_constraint_sets WHERE version = ?",
            (version,)).fetchone()
        if row is None:
            return None

        constraints: List[RiskConstraint] = []
        for (constraint_id, scope, severity, max_value, min_value,
             applies_to, description, enabled) in self.conn.execute("""
                SELECT constraint_id, scope, severity, max_value, min_value,
                       applies_to, description, enabled
                FROM risk_constraints WHERE constraint_set_version = ?
                ORDER BY constraint_id
            """, (version,)):
            constraints.append(RiskConstraint(
                constraint_id=constraint_id, scope=ConstraintScope(scope),
                severity=ConstraintSeverity(severity), max_value=max_value,
                min_value=min_value, applies_to=applies_to,
                description=description, enabled=bool(enabled)))

        return ConstraintSet(version=row[0], name=row[1],
                             trading_state=TradingState(row[2]),
                             constraints=constraints)

    def load_or_default(self, version: str = DEFAULT_CONSTRAINT_VERSION) -> ConstraintSet:
        """
        Load a version, seeding it from the shipped defaults if absent.

        Seeding writes the defaults under that version, so the set a
        decision refers to is on record from the first run rather than
        living only in code that may change underneath it.
        """
        existing = self.load(version)
        if existing is not None:
            return existing
        seeded = default_constraint_set()
        seeded.version = version
        self.save(seeded)
        return seeded
