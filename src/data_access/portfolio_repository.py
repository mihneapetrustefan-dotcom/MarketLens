"""
src/data_access/portfolio_repository.py
--------------------------------------------
Read/write access for Phase 11 (portfolios, positions, snapshots,
proposals, decisions, order intents).

DECISIONS ARE WRITTEN ONCE AND NEVER REWRITTEN
--------------------------------------------------
`save_decision` uses INSERT OR REPLACE keyed on a decision id that is
itself derived from (engine version, constraint version, portfolio,
anchor, proposal). Re-running the same evaluation over the same inputs
therefore rewrites the same row with the same content — idempotent —
while any genuinely different evaluation gets a different id and a new
row. What never happens is an old decision being edited to say
something new, which is what would break the audit trail.

THE EQUITY CURVE COMES FROM HERE
------------------------------------
`equity_curve` is the only sanctioned source of drawdown input. It
reads observed equity from stored snapshots, so a drawdown figure is
always backed by states this system actually recorded rather than a
curve reconstructed after the fact (spec §12).

SNAPSHOTS ARE POINT-IN-TIME BY CONSTRUCTION
-----------------------------------------------
Every read that takes an anchor filters `as_of <= anchor` in SQL. A
replay anchored last month cannot see a snapshot written yesterday,
even though that row exists in the same table.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.domain.portfolio_models import (
    AllocationChange, AllocationProposal, ConstraintScope, ConstraintSeverity,
    OrderIntent, PortfolioSnapshot, Position, PositionSource, PositionStatus,
    RiskDecision, RiskDecisionState, RiskProvenance, RiskViolation,
)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _parse(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


class PortfolioRepository:
    """Persistence for the Phase 11 tables."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---------------- portfolios ----------------

    def save_portfolio(self, portfolio_id: str, name: str, base_currency: str = "USD",
                       cash: float = 0.0, kind: str = "declared",
                       created_at: Optional[datetime] = None,
                       metadata: Optional[Dict[str, Any]] = None) -> str:
        self.conn.execute("""
            INSERT OR REPLACE INTO portfolios
            (portfolio_id, name, base_currency, cash, kind, created_at, metadata_json)
            VALUES (?,?,?,?,?,?,?)
        """, (portfolio_id, name, base_currency, cash, kind,
              _iso(created_at), json.dumps(metadata or {}, default=str)))
        self.conn.commit()
        return portfolio_id

    def get_portfolio(self, portfolio_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("""
            SELECT portfolio_id, name, base_currency, cash, kind, created_at, metadata_json
            FROM portfolios WHERE portfolio_id = ?
        """, (portfolio_id,)).fetchone()
        if row is None:
            return None
        return {"portfolio_id": row[0], "name": row[1], "base_currency": row[2],
                "cash": row[3], "kind": row[4], "created_at": _parse(row[5]),
                "metadata": json.loads(row[6])}

    def list_portfolios(self) -> List[Dict[str, Any]]:
        return [self.get_portfolio(row[0])
                for row in self.conn.execute(
                    "SELECT portfolio_id FROM portfolios ORDER BY portfolio_id")]

    # ---------------- positions ----------------

    def save_position(self, position: Position) -> str:
        self.conn.execute("""
            INSERT OR REPLACE INTO positions
            (position_id, portfolio_id, instrument_id, quantity, average_entry_price,
             currency, status, source, opened_at, closed_at, realized_pnl, metadata_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (position.position_id, position.portfolio_id, position.instrument_id,
              position.quantity, position.average_entry_price, position.currency,
              position.status.value, position.source.value, _iso(position.opened_at),
              _iso(position.closed_at), position.realized_pnl,
              json.dumps(position.metadata, default=str)))
        self.conn.commit()
        return position.position_id

    def _row_to_position(self, row: Sequence) -> Position:
        return Position(
            position_id=row[0], portfolio_id=row[1], instrument_id=row[2],
            quantity=row[3], average_entry_price=row[4], currency=row[5],
            status=PositionStatus(row[6]), source=PositionSource(row[7]),
            opened_at=_parse(row[8]), closed_at=_parse(row[9]),
            realized_pnl=row[10], metadata=json.loads(row[11]))

    def open_positions(self, portfolio_id: str,
                       as_of: Optional[datetime] = None) -> List[Position]:
        """
        Positions open at the anchor.

        With an anchor, "open" means opened at or before it and not yet
        closed by it — a position closed last week is correctly still
        open when replaying a month ago. Reading only `status` would
        apply today's lifecycle to a past moment, which is exactly the
        leak point-in-time correctness exists to prevent.
        """
        sql = """
            SELECT position_id, portfolio_id, instrument_id, quantity, average_entry_price,
                   currency, status, source, opened_at, closed_at, realized_pnl, metadata_json
            FROM positions WHERE portfolio_id = ?
        """
        params: List[Any] = [portfolio_id]

        if as_of is None:
            sql += " AND status = ?"
            params.append(PositionStatus.OPEN.value)
        else:
            anchor = as_of.isoformat()
            sql += (" AND (opened_at IS NULL OR opened_at <= ?)"
                    " AND (closed_at IS NULL OR closed_at > ?)")
            params.extend([anchor, anchor])

        sql += " ORDER BY instrument_id"
        return [self._row_to_position(row) for row in self.conn.execute(sql, params)]

    def close_position(self, position_id: str, closed_at: datetime,
                       realized_pnl: Optional[float] = None) -> None:
        self.conn.execute("""
            UPDATE positions SET status = ?, closed_at = ?, realized_pnl = ?
            WHERE position_id = ?
        """, (PositionStatus.CLOSED.value, _iso(closed_at), realized_pnl, position_id))
        self.conn.commit()

    # ---------------- snapshots ----------------

    def save_snapshot(self, snapshot_id: str, snapshot: PortfolioSnapshot,
                      metrics_json: Optional[Dict[str, Any]] = None,
                      computed_at: Optional[datetime] = None) -> str:
        self.conn.execute("""
            INSERT OR REPLACE INTO portfolio_state_snapshots
            (snapshot_id, portfolio_id, as_of, base_currency, cash, equity,
             gross_exposure, net_exposure, long_exposure, short_exposure, leverage,
             unrealized_pnl, realized_pnl, position_count, is_complete, unvalued_count,
             is_multi_currency, computed_at, metrics_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (snapshot_id, snapshot.portfolio_id, _iso(snapshot.as_of),
              snapshot.base_currency, snapshot.cash, snapshot.equity,
              snapshot.gross_exposure, snapshot.net_exposure, snapshot.long_exposure,
              snapshot.short_exposure, snapshot.leverage, snapshot.unrealized_pnl,
              snapshot.realized_pnl, len(snapshot.valuations),
              int(snapshot.is_complete), len(snapshot.unvalued_positions),
              int(snapshot.is_multi_currency), _iso(computed_at),
              json.dumps(metrics_json or {}, default=str)))
        self.conn.commit()
        return snapshot_id

    def equity_curve(self, portfolio_id: str,
                     as_of: Optional[datetime] = None
                     ) -> List[Tuple[datetime, float]]:
        """
        Observed equity over time — the ONLY sanctioned drawdown input.

        Incomplete snapshots are excluded: their equity is understated
        by however much could not be priced, and a fake trough produced
        by a pricing gap is indistinguishable from a real loss once it
        is in the curve.
        """
        sql = """
            SELECT as_of, equity FROM portfolio_state_snapshots
            WHERE portfolio_id = ? AND equity IS NOT NULL AND is_complete = 1
        """
        params: List[Any] = [portfolio_id]
        if as_of is not None:
            sql += " AND as_of <= ?"
            params.append(as_of.isoformat())
        sql += " ORDER BY as_of ASC"
        return [(datetime.fromisoformat(row[0]), row[1])
                for row in self.conn.execute(sql, params)]

    # ---------------- proposals ----------------

    def save_proposal(self, proposal: AllocationProposal,
                      created_at: Optional[datetime] = None) -> str:
        self.conn.execute("""
            INSERT OR REPLACE INTO allocation_proposals
            (proposal_id, portfolio_id, as_of, sizing_strategy_id, sizing_version,
             note, created_at)
            VALUES (?,?,?,?,?,?,?)
        """, (proposal.proposal_id, proposal.portfolio_id, _iso(proposal.as_of),
              proposal.sizing_strategy_id, proposal.sizing_version, proposal.note,
              _iso(created_at)))

        self.conn.execute(
            "DELETE FROM allocation_changes WHERE proposal_id = ?", (proposal.proposal_id,))
        for change in proposal.changes:
            self.conn.execute("""
                INSERT OR REPLACE INTO allocation_changes
                (proposal_id, instrument_id, current_weight, target_weight,
                 current_quantity, target_quantity, signal_id, reason)
                VALUES (?,?,?,?,?,?,?,?)
            """, (proposal.proposal_id, change.instrument_id, change.current_weight,
                  change.target_weight, change.current_quantity, change.target_quantity,
                  change.signal_id, change.reason))
        self.conn.commit()
        return proposal.proposal_id

    def get_proposal(self, proposal_id: str) -> Optional[AllocationProposal]:
        row = self.conn.execute("""
            SELECT proposal_id, portfolio_id, as_of, sizing_strategy_id,
                   sizing_version, note
            FROM allocation_proposals WHERE proposal_id = ?
        """, (proposal_id,)).fetchone()
        if row is None:
            return None

        proposal = AllocationProposal(
            proposal_id=row[0], portfolio_id=row[1], as_of=_parse(row[2]),
            sizing_strategy_id=row[3], sizing_version=row[4], note=row[5])

        for change_row in self.conn.execute("""
            SELECT instrument_id, current_weight, target_weight, current_quantity,
                   target_quantity, signal_id, reason
            FROM allocation_changes WHERE proposal_id = ? ORDER BY instrument_id
        """, (proposal_id,)):
            proposal.changes.append(AllocationChange(
                instrument_id=change_row[0], current_weight=change_row[1],
                target_weight=change_row[2], current_quantity=change_row[3],
                target_quantity=change_row[4], signal_id=change_row[5],
                reason=change_row[6]))
            if change_row[5]:
                proposal.source_signal_ids.append(change_row[5])
        return proposal

    # ---------------- decisions ----------------

    def save_decision(self, decision: RiskDecision,
                      created_at: Optional[datetime] = None) -> str:
        p = decision.provenance
        self.conn.execute("""
            INSERT OR REPLACE INTO risk_decisions
            (decision_id, portfolio_id, proposal_id, state, as_of, summary,
             reasons_json, evaluated_scopes_json, skipped_scopes_json,
             risk_engine_version, constraint_set_version, sizing_version,
             snapshot_as_of, information_cutoff, price_data_as_of,
             provenance_inputs_json, metrics_json, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (decision.decision_id, decision.portfolio_id, decision.proposal_id,
              decision.state.value, _iso(decision.as_of), decision.summary,
              json.dumps(decision.reasons), json.dumps(decision.evaluated_scopes),
              json.dumps(decision.skipped_scopes), p.risk_engine_version,
              p.constraint_set_version, p.sizing_version,
              _iso(p.portfolio_snapshot_as_of), _iso(p.information_cutoff),
              _iso(p.price_data_as_of), json.dumps(p.inputs, default=str),
              json.dumps(self._metrics_payload(decision), default=str),
              _iso(created_at)))

        self.conn.execute(
            "DELETE FROM risk_violations WHERE decision_id = ?", (decision.decision_id,))
        for violation in decision.violations:
            self.conn.execute("""
                INSERT OR REPLACE INTO risk_violations
                (decision_id, constraint_id, scope, severity, message,
                 observed_value, current_value, limit_value, applies_to, remediated)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (decision.decision_id, violation.constraint_id, violation.scope.value,
                  violation.severity.value, violation.message, violation.observed_value,
                  violation.current_value, violation.limit_value,
                  violation.applies_to or "", int(violation.remediated)))
        self.conn.commit()
        return decision.decision_id

    def _metrics_payload(self, decision: RiskDecision) -> Dict[str, Any]:
        """
        Flatten the metrics that justified a decision.

        Stored on the decision rather than only on a snapshot so the
        numbers a verdict rested on stay attached to it, even if the
        snapshot is later recomputed under a different configuration.
        """
        metrics = decision.metrics
        if metrics is None:
            return {}
        return {
            "volatility": metrics.volatility.value,
            "volatility_observations": metrics.volatility.observations,
            "volatility_insufficient": metrics.volatility.insufficient_data,
            "var_95": metrics.value_at_risk.value,
            "expected_shortfall_95": metrics.value_at_risk.expected_shortfall,
            "var_insufficient": metrics.value_at_risk.insufficient_data,
            "max_drawdown": metrics.drawdown.max_drawdown,
            "current_drawdown": metrics.drawdown.current_drawdown,
            "hhi": metrics.concentration.hhi,
            "effective_positions": metrics.concentration.effective_positions,
            "largest_weight": metrics.concentration.largest_weight,
            "average_correlation": metrics.correlation.average_correlation,
            "highly_correlated_pairs": len(metrics.correlation.highly_correlated_pairs),
            "unavailable": metrics.unavailable,
        }

    def get_decision(self, decision_id: str) -> Optional[RiskDecision]:
        row = self.conn.execute("""
            SELECT decision_id, portfolio_id, proposal_id, state, as_of, summary,
                   reasons_json, evaluated_scopes_json, skipped_scopes_json,
                   risk_engine_version, constraint_set_version, sizing_version,
                   snapshot_as_of, information_cutoff, price_data_as_of,
                   provenance_inputs_json
            FROM risk_decisions WHERE decision_id = ?
        """, (decision_id,)).fetchone()
        if row is None:
            return None

        decision = RiskDecision(
            decision_id=row[0], portfolio_id=row[1], proposal_id=row[2],
            state=RiskDecisionState(row[3]), as_of=_parse(row[4]), summary=row[5],
            reasons=json.loads(row[6]), evaluated_scopes=json.loads(row[7]),
            skipped_scopes=json.loads(row[8]),
            provenance=RiskProvenance(
                risk_engine_version=row[9], constraint_set_version=row[10],
                sizing_version=row[11], portfolio_snapshot_as_of=_parse(row[12]),
                information_cutoff=_parse(row[13]), price_data_as_of=_parse(row[14]),
                inputs=json.loads(row[15])))

        for violation_row in self.conn.execute("""
            SELECT constraint_id, scope, severity, message, observed_value,
                   current_value, limit_value, applies_to, remediated
            FROM risk_violations WHERE decision_id = ?
        """, (decision_id,)):
            decision.violations.append(RiskViolation(
                constraint_id=violation_row[0], scope=ConstraintScope(violation_row[1]),
                severity=ConstraintSeverity(violation_row[2]), message=violation_row[3],
                observed_value=violation_row[4], current_value=violation_row[5],
                limit_value=violation_row[6], applies_to=violation_row[7] or None,
                remediated=bool(violation_row[8])))
        return decision

    def decisions_for(self, portfolio_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Recent decisions as summary rows — for listings, not for replay."""
        return [{"decision_id": r[0], "state": r[1], "as_of": _parse(r[2]),
                 "summary": r[3], "proposal_id": r[4],
                 "constraint_set_version": r[5], "violations": r[6]}
                for r in self.conn.execute("""
                    SELECT d.decision_id, d.state, d.as_of, d.summary, d.proposal_id,
                           d.constraint_set_version,
                           (SELECT COUNT(*) FROM risk_violations v
                             WHERE v.decision_id = d.decision_id)
                    FROM risk_decisions d
                    WHERE d.portfolio_id = ?
                    ORDER BY d.as_of DESC LIMIT ?
                """, (portfolio_id, limit))]

    # ---------------- order intents (inert) ----------------

    def save_intent(self, intent: OrderIntent) -> str:
        """
        Record an intent. Recording is NOT sending — there is nowhere to
        send it, and no code in this phase attempts to.
        """
        self.conn.execute("""
            INSERT OR REPLACE INTO order_intents
            (intent_id, portfolio_id, instrument_id, side, target_weight,
             target_quantity, source_signal_id, decision_id, reason,
             created_at, valid_until)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (intent.intent_id, intent.portfolio_id, intent.instrument_id, intent.side,
              intent.target_weight, intent.target_quantity, intent.source_signal_id,
              intent.decision_id, intent.reason, _iso(intent.created_at),
              _iso(intent.valid_until)))
        self.conn.commit()
        return intent.intent_id

    def intents_for_decision(self, decision_id: str) -> List[OrderIntent]:
        return [OrderIntent(
            intent_id=r[0], portfolio_id=r[1], instrument_id=r[2], side=r[3],
            target_weight=r[4], target_quantity=r[5], source_signal_id=r[6],
            decision_id=r[7], reason=r[8], created_at=_parse(r[9]),
            valid_until=_parse(r[10]))
            for r in self.conn.execute("""
                SELECT intent_id, portfolio_id, instrument_id, side, target_weight,
                       target_quantity, source_signal_id, decision_id, reason,
                       created_at, valid_until
                FROM order_intents WHERE decision_id = ? ORDER BY instrument_id
            """, (decision_id,))]
