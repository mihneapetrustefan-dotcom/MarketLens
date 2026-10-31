"""
src/adaptive/allocation_repository.py
--------------------------------------------------
Storing shadow allocation plans (Phase 25.9I-D).

Same three properties as the 25.9I-B universe repository, for the same
reasons: writing a plan twice writes it once, a revision never erases
its predecessor, and nothing outside `adaptive_allocation_*` is touched.

The lineage back to the session universe is carried on both the plan and
each line (`session_snapshot_id`, `admission_decision_id`), so a future
attribution phase can ask "which universe decision led to this money"
and get an answer out of the database rather than a reconstruction.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.data_access.adaptive_allocation_schema import (
    initialize_adaptive_allocation_schema,
)
from src.domain.allocation_models import PortfolioAllocationPlan


class AdaptiveAllocationRepository:
    """Durable, append-only store for shadow plans and their lines."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        initialize_adaptive_allocation_schema(conn)

    def save_plan(self, plan: PortfolioAllocationPlan,
                  session_snapshot_id: Optional[str] = None,
                  created_at: Optional[datetime] = None) -> bool:
        """Persist one plan. True when it was new, False when already stored."""
        stamp = (created_at or datetime.now(timezone.utc)).astimezone(
            timezone.utc).isoformat()
        risk_budget = plan.risk_budget
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO adaptive_allocation_plans (plan_id, as_of, "
            "status, revision, previous_plan_id, account_equity, cash, "
            "base_currency, deployable_capital, deployed, reserved, unallocated, "
            "capital_above_ceiling, total_capital_at_risk, risk_budget_total, "
            "risk_budget_allocated, config_version, session_snapshot_id, "
            "lineage_id, cash_reserves, constraints, gaps, notes, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (plan.plan_id, plan.as_of.isoformat(), plan.status.value, plan.revision,
             plan.previous_plan_id, plan.capital_state.account_equity,
             plan.capital_state.cash, plan.capital_state.base_currency,
             plan.deployable_capital, plan.deployed, plan.reserved,
             plan.unallocated, plan.capital_above_ceiling,
             plan.total_capital_at_risk,
             risk_budget.total_risk_usd if risk_budget else None,
             risk_budget.allocated_risk_usd if risk_budget else None,
             plan.config_version, session_snapshot_id,
             plan.lineage.lineage_id if plan.lineage else None,
             json.dumps([{"amount_usd": r.amount_usd, "reason": r.reason}
                         for r in plan.cash_reserves]),
             json.dumps([{"name": c.name, "scope": c.scope, "limit": c.limit}
                         for c in plan.constraints]),
             json.dumps(list(plan.gaps)), json.dumps(list(plan.notes)), stamp))
        is_new = cursor.rowcount > 0

        for line in plan.lines:
            self.conn.execute(
                "INSERT OR IGNORE INTO adaptive_allocation_lines (plan_id, "
                "instrument_id, status, sector_id, requested_notional, "
                "allocated_notional, capital_at_risk, portfolio_weight, "
                "risk_status, risk_per_dollar, desirability, "
                "desirability_completeness, desirability_positives, "
                "desirability_penalties, desirability_unavailable, "
                "constraints_applied, rejection_reasons, reasons, "
                "session_snapshot_id, admission_decision_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (plan.plan_id, line.instrument_id, line.status.value,
                 line.sector_id, line.requested_notional, line.allocated_notional,
                 line.capital_at_risk, line.portfolio_weight,
                 line.risk.status.value, line.risk.risk_per_dollar,
                 line.desirability.overall, line.desirability.completeness,
                 json.dumps(line.desirability.positives, sort_keys=True),
                 json.dumps(line.desirability.penalties, sort_keys=True),
                 json.dumps(list(line.desirability.unavailable)),
                 json.dumps([{"constraint": c.constraint, "scope": c.scope,
                              "limited_to": c.limited_to, "removed": c.removed,
                              "detail": c.detail}
                             for c in line.constraints_applied]),
                 json.dumps([r.value for r in line.rejection_reasons]),
                 json.dumps(list(line.reasons)), line.session_snapshot_id,
                 line.admission_decision_id))
        self.conn.commit()
        return is_new

    # ---------------- reading ----------------

    def plan(self, plan_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT plan_id, as_of, status, revision, previous_plan_id, "
            "deployable_capital, deployed, reserved, unallocated, "
            "total_capital_at_risk, session_snapshot_id, gaps "
            "FROM adaptive_allocation_plans WHERE plan_id = ?", (plan_id,)).fetchone()
        if row is None:
            return None
        return {"plan_id": row[0], "as_of": row[1], "status": row[2],
                "revision": row[3], "previous_plan_id": row[4],
                "deployable_capital": row[5], "deployed": row[6],
                "reserved": row[7], "unallocated": row[8],
                "total_capital_at_risk": row[9], "session_snapshot_id": row[10],
                "gaps": json.loads(row[11] or "[]"),
                "lines": self.lines(plan_id)}

    def lines(self, plan_id: str) -> List[Dict[str, Any]]:
        return [{"instrument_id": r[0], "status": r[1],
                 "allocated_notional": r[2], "requested_notional": r[3],
                 "capital_at_risk": r[4], "risk_status": r[5],
                 "desirability": r[6], "reasons": json.loads(r[7] or "[]"),
                 "constraints_applied": json.loads(r[8] or "[]")}
                for r in self.conn.execute(
                    "SELECT instrument_id, status, allocated_notional, "
                    "requested_notional, capital_at_risk, risk_status, "
                    "desirability, reasons, constraints_applied "
                    "FROM adaptive_allocation_lines WHERE plan_id = ? "
                    "ORDER BY instrument_id", (plan_id,))]

    def revisions(self, session_snapshot_id: str) -> List[Dict[str, Any]]:
        """Every plan for one session universe, oldest first."""
        return [{"plan_id": r[0], "revision": r[1], "as_of": r[2],
                 "deployed": r[3], "previous_plan_id": r[4]}
                for r in self.conn.execute(
                    "SELECT plan_id, revision, as_of, deployed, previous_plan_id "
                    "FROM adaptive_allocation_plans WHERE session_snapshot_id = ? "
                    "ORDER BY revision", (session_snapshot_id,))]
