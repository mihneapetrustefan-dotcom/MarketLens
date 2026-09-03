"""
src/data_access/governance_repository.py
---------------------------------------------
Persistence for Phase 16 governance, sessions and outcomes
(spec §36, §37, §52, §67, §73).

RECOVERY IS WHY THIS EXISTS
-------------------------------
Spec §36: on restart the system restores its state, reconnects,
reconciles, and only then resumes. This class is the "restores its
state" half — the approval that was active, the session that was
running, the day's realized P&L, the latched limit breaches.

Losing any of those is worse than losing an order. A restarted process
that forgot its daily loss limit had latched would happily resume
trading through it.

APPEND-ONLY WHERE IT MATTERS
--------------------------------
Session events, journal entries and alerts use INSERT OR IGNORE and
have no update path. Sessions and approvals upsert on their primary
key, because a session legitimately changes state — but its EVENTS do
not change, and that is where the history lives.

NO SECRETS
--------------
Same property as every table since Phase 14: no method accepts a
credential and no column can hold one.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.domain.broker_models import CanonicalOrderSide, ExecutionRejectCode
from src.execution.governance import (
    ApprovalState, ExecutionLevel, PromotionRequest, ReadinessAssessment,
    ReadinessCategory, ReadinessVerdict,
)
from src.execution.limits import DayState, LimitBreach
from src.execution.monitoring import (
    Alert, AlertSeverity, Capability, CapabilityState, EnvironmentComparison,
    ExecutionMetrics, SystemHealth,
)
from src.execution.outcomes import (
    ExecutionQuality, ExitReason, JournalEntry, MissReason, MissedTrade,
    TradeLineage, TradeOutcome, TradePostMortem,
)
from src.execution.session import (
    PreflightCheck, SessionConfiguration, SessionEvent, SessionState,
    SessionSummary, TradingSession,
)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _parse(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def _json(value: Any) -> str:
    def convert(obj):
        if is_dataclass(obj) and not isinstance(obj, type):
            return {k: convert(v) for k, v in asdict(obj).items()}
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, dict):
            return {str(k): convert(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple, set)):
            return [convert(v) for v in obj]
        if hasattr(obj, "value"):
            return obj.value
        return obj
    return json.dumps(convert(value), default=str)


def _loads(raw: Optional[str], default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


class GovernanceRepository:
    """Reads and writes everything Phase 16 produces."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ================================================================
    # Approvals
    # ================================================================

    def save_approval(self, request: PromotionRequest) -> str:
        self.conn.execute("""
            INSERT INTO promotion_requests
            (request_id, level, level_label, state, requested_by, requested_at,
             reason, approved_by, approved_at, expires_at, decision_note,
             gate_snapshot_json, readiness_snapshot_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(request_id) DO UPDATE SET
                state = excluded.state,
                approved_by = excluded.approved_by,
                approved_at = excluded.approved_at,
                expires_at = excluded.expires_at,
                decision_note = excluded.decision_note,
                gate_snapshot_json = excluded.gate_snapshot_json,
                readiness_snapshot_json = excluded.readiness_snapshot_json
        """, (request.request_id, int(request.level), request.level.label,
              request.state.value, request.requested_by,
              _iso(request.requested_at), request.reason, request.approved_by,
              _iso(request.approved_at), _iso(request.expires_at),
              request.decision_note, _json(request.gate_snapshot),
              _json(request.readiness_snapshot)))
        self.conn.commit()
        return request.request_id

    def load_approvals(self) -> List[PromotionRequest]:
        out: List[PromotionRequest] = []
        for row in self.conn.execute("""
            SELECT request_id, level, state, requested_by, requested_at, reason,
                   approved_by, approved_at, expires_at, decision_note,
                   gate_snapshot_json, readiness_snapshot_json
            FROM promotion_requests ORDER BY requested_at
        """):
            request = PromotionRequest(
                request_id=row[0], level=ExecutionLevel(row[1]),
                requested_by=row[3], requested_at=_parse(row[4]),
                reason=row[5] or "")
            request.state = ApprovalState(row[2])
            request.approved_by = row[6]
            request.approved_at = _parse(row[7])
            request.expires_at = _parse(row[8])
            request.decision_note = row[9] or ""
            request.gate_snapshot = _loads(row[10], {})
            request.readiness_snapshot = _loads(row[11], {})
            out.append(request)
        return out

    def save_readiness(self, assessment: ReadinessAssessment,
                       actor: str = "system") -> str:
        assessment_id = f"rdy-{uuid.uuid4().hex[:16]}"
        self.conn.execute("""
            INSERT OR REPLACE INTO readiness_assessments
            (assessment_id, at, is_ready, verdicts_json, notes_json, actor)
            VALUES (?,?,?,?,?,?)
        """, (assessment_id, _iso(assessment.at), int(assessment.is_ready),
              _json({c.value: v.value for c, v in assessment.verdicts.items()}),
              _json({c.value: n for c, n in assessment.notes.items()}), actor))
        self.conn.commit()
        return assessment_id

    def latest_readiness(self) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("""
            SELECT assessment_id, at, is_ready, verdicts_json, notes_json, actor
            FROM readiness_assessments ORDER BY at DESC LIMIT 1
        """).fetchone()
        if row is None:
            return None
        return {"assessment_id": row[0], "at": row[1], "is_ready": bool(row[2]),
                "verdicts": _loads(row[3], {}), "notes": _loads(row[4], {}),
                "actor": row[5]}

    # ================================================================
    # Sessions
    # ================================================================

    def save_session(self, session: TradingSession) -> str:
        config = session.config
        self.conn.execute("""
            INSERT INTO trading_sessions
            (session_id, state, operator, broker_id, account_id, environment,
             level, approval_id, config_json, config_fingerprint,
             model_version, strategy_version, feature_version, signal_version,
             risk_config_version, execution_config_version, code_version,
             capital_limit, daily_loss_limit, created_at, started_at, ended_at,
             termination_reason, preflight_json, summary_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(session_id) DO UPDATE SET
                state = excluded.state,
                started_at = excluded.started_at,
                ended_at = excluded.ended_at,
                termination_reason = excluded.termination_reason,
                preflight_json = excluded.preflight_json,
                summary_json = excluded.summary_json
        """, (session.session_id, session.state.value, session.operator,
              config.broker_id, config.account_id, config.environment.value,
              int(config.level), session.approval_id, _json(config.as_dict()),
              session.fingerprint, config.model_version,
              config.strategy_version, config.feature_version,
              config.signal_version, config.risk_config_version,
              config.execution_config_version, config.code_version,
              config.capital_limit, config.daily_loss_limit,
              _iso(session.created_at), _iso(session.started_at),
              _iso(session.ended_at), session.termination_reason,
              _json([{"name": c.name, "passed": c.passed,
                      "measured": c.measured, "detail": c.detail}
                     for c in session.preflight]),
              _json(session.summary.as_dict() if session.summary else {})))
        self._save_session_events(session.events)
        self.conn.commit()
        return session.session_id

    def _save_session_events(self, events: Sequence[SessionEvent]) -> None:
        # INSERT OR IGNORE: session history is append-only, and a
        # re-save must not rewrite what was recorded.
        self.conn.executemany("""
            INSERT OR IGNORE INTO session_events
            (session_id, sequence, at, action, actor, from_state, to_state,
             reason, payload_json)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, [(e.session_id, e.sequence, _iso(e.at), e.action, e.actor,
               e.from_state, e.to_state, e.reason, _json(e.payload))
              for e in events])

    def load_session(self, session_id: str) -> Optional[TradingSession]:
        row = self.conn.execute("""
            SELECT session_id, state, operator, config_json, approval_id,
                   created_at, started_at, ended_at, termination_reason,
                   preflight_json
            FROM trading_sessions WHERE session_id = ?
        """, (session_id,)).fetchone()
        if row is None:
            return None

        raw = _loads(row[3], {})
        from src.domain.broker_models import ExecutionEnvironment
        config = SessionConfiguration(
            broker_id=raw.get("broker_id", "ibkr"),
            account_id=raw.get("account_id", ""),
            environment=ExecutionEnvironment(raw.get("environment", "paper")),
            level=ExecutionLevel(raw.get("level", 2)),
            strategies=tuple(raw.get("strategies") or ()),
            capital_limit=raw.get("capital_limit"),
            max_order_notional=raw.get("max_order_notional"),
            daily_loss_limit=raw.get("daily_loss_limit"),
            max_open_positions=raw.get("max_open_positions"),
            model_version=raw.get("model_version"),
            strategy_version=raw.get("strategy_version"),
            feature_version=raw.get("feature_version"),
            signal_version=raw.get("signal_version"),
            risk_config_version=raw.get("risk_config_version", "v1"),
            execution_config_version=raw.get("execution_config_version",
                                             "exec-v1"),
            code_version=raw.get("code_version", "phase16-v1"),
            execution_policy=raw.get("execution_policy", "market"))

        session = TradingSession(
            session_id=row[0], config=config, operator=row[2],
            created_at=_parse(row[5]), approval_id=row[4])
        session.state = SessionState(row[1])
        session.started_at = _parse(row[6])
        session.ended_at = _parse(row[7])
        session.termination_reason = row[8] or ""
        session.preflight = [
            PreflightCheck(name=c["name"], passed=c["passed"],
                           measured=c.get("measured", True),
                           detail=c.get("detail", ""))
            for c in _loads(row[9], [])]
        session.events = [
            SessionEvent(session_id=e[0], sequence=e[1], at=_parse(e[2]),
                         action=e[3], actor=e[4], from_state=e[5],
                         to_state=e[6], reason=e[7] or "",
                         payload=_loads(e[8], {}))
            for e in self.conn.execute("""
                SELECT session_id, sequence, at, action, actor, from_state,
                       to_state, reason, payload_json
                FROM session_events WHERE session_id = ? ORDER BY sequence
            """, (session_id,))]
        return session

    def active_session(self) -> Optional[TradingSession]:
        """
        The session that was running, if any.

        Restart recovery starts here (spec §36): a process that forgot
        it had an active session would open a second one and lose the
        day's accumulated limits.
        """
        # ACTIVE or PAUSED only. A session left in CREATED never
        # started — it failed its preflight — and treating that dead
        # record as "running" would block every subsequent attempt to
        # open a real one.
        row = self.conn.execute("""
            SELECT session_id FROM trading_sessions
            WHERE state IN ('active','paused')
            ORDER BY COALESCE(started_at, created_at) DESC LIMIT 1
        """).fetchone()
        return self.load_session(row[0]) if row else None

    def abandoned_sessions(self, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Sessions that were created and never started.

        Kept rather than deleted — a failed preflight is evidence — but
        reported separately so they are not mistaken for running ones.
        """
        return [{"session_id": r[0], "operator": r[1], "created_at": r[2],
                 "preflight": _loads(r[3], [])}
                for r in self.conn.execute("""
            SELECT session_id, operator, created_at, preflight_json
            FROM trading_sessions WHERE state IN ('created','validating')
            ORDER BY COALESCE(created_at, '') DESC LIMIT ?
        """, (limit,))]

    def list_sessions(self, limit: int = 25) -> List[Dict[str, Any]]:
        return [{
            "session_id": r[0], "state": r[1], "operator": r[2],
            "environment": r[3], "level": r[4], "account_id": r[5],
            "started_at": r[6], "ended_at": r[7],
            "fingerprint": r[8], "summary": _loads(r[9], {}),
        } for r in self.conn.execute("""
            SELECT session_id, state, operator, environment, level, account_id,
                   started_at, ended_at, config_fingerprint, summary_json
            FROM trading_sessions
            ORDER BY COALESCE(started_at, created_at) DESC LIMIT ?
        """, (limit,))]

    # ---------------- day state ----------------

    def save_day_state(self, session_id: str, day: DayState,
                       at: Optional[datetime] = None) -> None:
        self.conn.execute("""
            INSERT OR REPLACE INTO session_day_state
            (session_id, day, realized_pnl, unrealized_pnl, starting_equity,
             current_equity, peak_equity, orders_submitted, orders_rejected,
             updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (session_id, day.day or "", day.realized_pnl, day.unrealized_pnl,
              day.starting_equity, day.current_equity, day.peak_equity,
              day.orders_submitted, day.orders_rejected, _iso(at)))
        self.conn.commit()

    def load_day_state(self, session_id: str,
                       day: str) -> Optional[DayState]:
        row = self.conn.execute("""
            SELECT day, realized_pnl, unrealized_pnl, starting_equity,
                   current_equity, peak_equity, orders_submitted, orders_rejected
            FROM session_day_state WHERE session_id = ? AND day = ?
        """, (session_id, day)).fetchone()
        if row is None:
            return None
        return DayState(day=row[0], realized_pnl=row[1], unrealized_pnl=row[2],
                        starting_equity=row[3], current_equity=row[4],
                        peak_equity=row[5], orders_submitted=row[6],
                        orders_rejected=row[7])

    # ================================================================
    # Trade outcomes and misses
    # ================================================================

    def save_outcome(self, outcome: TradeOutcome,
                     session_id: Optional[str] = None) -> str:
        lineage, quality = outcome.lineage, outcome.quality
        self.conn.execute("""
            INSERT OR REPLACE INTO trade_outcomes
            (outcome_id, session_id, instrument_id, side, quantity, environment,
             is_open, entry_at, exit_at, entry_price, exit_price, gross_pnl,
             fees, net_pnl, return_pct, holding_days, exit_reason,
             market_regime, event_context_json, correlation_id, model_id,
             model_version, prediction_id, feature_version, signal_id,
             signal_version, strategy_id, strategy_version, portfolio_id,
             decision_id, risk_config_version, intent_id, order_id,
             client_order_id, broker_order_id, execution_ids_json,
             fill_ids_json, execution_config_version, code_version,
             lineage_complete, decision_price, reference_price, bid, ask,
             submitted_price, fill_price, slippage, slippage_bps, commission,
             submit_latency_ms, ack_latency_ms, fill_latency_ms,
             post_mortem_json, reviewed_by, reviewed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (outcome.outcome_id, session_id or lineage.session_id,
              outcome.instrument_id, outcome.side.value, outcome.quantity,
              outcome.environment, int(outcome.is_open),
              _iso(outcome.entry_at), _iso(outcome.exit_at),
              outcome.entry_price, outcome.exit_price, outcome.gross_pnl,
              outcome.fees, outcome.net_pnl, outcome.return_pct,
              outcome.holding_days, outcome.exit_reason.value,
              outcome.market_regime, _json(list(outcome.event_context)),
              lineage.correlation_id, lineage.model_id, lineage.model_version,
              lineage.prediction_id, lineage.feature_version, lineage.signal_id,
              lineage.signal_version, lineage.strategy_id,
              lineage.strategy_version, lineage.portfolio_id,
              lineage.decision_id, lineage.risk_config_version,
              lineage.intent_id, lineage.order_id, lineage.client_order_id,
              lineage.broker_order_id, _json(list(lineage.execution_ids)),
              _json(list(lineage.fill_ids)), lineage.execution_config_version,
              lineage.code_version, int(lineage.is_complete),
              quality.decision_price, quality.reference_price, quality.bid,
              quality.ask, quality.submitted_price, quality.fill_price,
              quality.slippage, quality.slippage_bps, quality.commission,
              quality.submit_latency_ms, quality.ack_latency_ms,
              quality.fill_latency_ms, _json(outcome.post_mortem.as_dict()),
              outcome.post_mortem.reviewed_by,
              _iso(outcome.post_mortem.reviewed_at)))
        self.conn.commit()
        return outcome.outcome_id

    def query_outcomes(self, *, strategy_id: Optional[str] = None,
                       model_version: Optional[str] = None,
                       instrument_id: Optional[str] = None,
                       market_regime: Optional[str] = None,
                       session_id: Optional[str] = None,
                       limit: int = 200) -> List[Dict[str, Any]]:
        """
        The query a future learning phase will make (spec §67).

        By strategy, model, instrument, regime or session — which is
        why those columns are indexed rather than buried in a JSON
        blob.
        """
        sql = """
            SELECT outcome_id, session_id, instrument_id, side, quantity,
                   environment, entry_at, exit_at, entry_price, exit_price,
                   net_pnl, return_pct, holding_days, exit_reason,
                   market_regime, strategy_id, model_version, signal_id,
                   correlation_id, slippage_bps, commission, fees,
                   lineage_complete, post_mortem_json
            FROM trade_outcomes
        """
        clauses, params = [], []
        for column, value in (("strategy_id", strategy_id),
                              ("model_version", model_version),
                              ("instrument_id", instrument_id),
                              ("market_regime", market_regime),
                              ("session_id", session_id)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY COALESCE(exit_at, entry_at, '') DESC LIMIT ?"
        params.append(limit)

        return [{
            "outcome_id": r[0], "session_id": r[1], "instrument_id": r[2],
            "side": r[3], "quantity": r[4], "environment": r[5],
            "entry_at": r[6], "exit_at": r[7], "entry_price": r[8],
            "exit_price": r[9], "net_pnl": r[10], "return_pct": r[11],
            "holding_days": r[12], "exit_reason": r[13],
            "market_regime": r[14], "strategy_id": r[15],
            "model_version": r[16], "signal_id": r[17],
            "correlation_id": r[18], "slippage_bps": r[19],
            "commission": r[20], "fees": r[21],
            "lineage_complete": bool(r[22]),
            "post_mortem": _loads(r[23], {}),
        } for r in self.conn.execute(sql, params)]

    def save_missed(self, missed: MissedTrade,
                    session_id: Optional[str] = None) -> str:
        self.conn.execute("""
            INSERT OR REPLACE INTO missed_trades
            (missed_id, at, session_id, instrument_id, reason, detail, side,
             intended_quantity, reference_price, reject_code,
             prevented_by_system, forward_return, correlation_id, signal_id,
             strategy_id, lineage_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (missed.missed_id, _iso(missed.at),
              session_id or missed.session_id, missed.instrument_id,
              missed.reason.value, missed.detail,
              missed.side.value if missed.side else None,
              missed.intended_quantity, missed.reference_price,
              missed.reject_code.value if missed.reject_code else None,
              int(missed.was_prevented), missed.forward_return,
              missed.lineage.correlation_id, missed.lineage.signal_id,
              missed.lineage.strategy_id, _json(missed.lineage.as_dict())))
        self.conn.commit()
        return missed.missed_id

    def missed_summary(self) -> Dict[str, Any]:
        rows = list(self.conn.execute("""
            SELECT reason, prevented_by_system, COUNT(*)
            FROM missed_trades GROUP BY reason, prevented_by_system
        """))
        return {
            "total": sum(r[2] for r in rows),
            "prevented_by_system": sum(r[2] for r in rows if r[1]),
            "by_reason": {r[0]: r[2] for r in rows},
        }

    # ================================================================
    # Journal, alerts, health
    # ================================================================

    def save_journal(self, entries: Sequence[JournalEntry]) -> int:
        self.conn.executemany("""
            INSERT OR IGNORE INTO execution_journal
            (entry_id, at, session_id, kind, summary, correlation_id,
             detail_json)
            VALUES (?,?,?,?,?,?,?)
        """, [(e.entry_id, _iso(e.at), e.session_id, e.kind, e.summary,
               e.correlation_id, _json(e.detail)) for e in entries])
        self.conn.commit()
        return len(entries)

    def journal_for(self, session_id: Optional[str] = None,
                    limit: int = 200) -> List[Dict[str, Any]]:
        sql = ("SELECT entry_id, at, session_id, kind, summary, "
               "correlation_id, detail_json FROM execution_journal")
        params: List[Any] = []
        if session_id:
            sql += " WHERE session_id = ?"
            params.append(session_id)
        sql += " ORDER BY COALESCE(at, '') DESC LIMIT ?"
        params.append(limit)
        return [{"entry_id": r[0], "at": r[1], "session_id": r[2],
                 "kind": r[3], "summary": r[4], "correlation_id": r[5],
                 "detail": _loads(r[6], {})}
                for r in self.conn.execute(sql, params)]

    def save_alerts(self, alerts: Sequence[Alert]) -> int:
        self.conn.executemany("""
            INSERT INTO execution_alerts
            (alert_id, at, session_id, code, severity, message, detail,
             order_id, acknowledged, acknowledged_by)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(alert_id) DO UPDATE SET
                acknowledged = excluded.acknowledged,
                acknowledged_by = excluded.acknowledged_by
        """, [(a.alert_id, _iso(a.at), a.session_id, a.code, a.severity.value,
               a.message, a.detail, a.order_id, int(a.acknowledged),
               a.acknowledged_by) for a in alerts])
        self.conn.commit()
        return len(alerts)

    def open_alerts(self, limit: int = 50) -> List[Dict[str, Any]]:
        return [{"alert_id": r[0], "at": r[1], "session_id": r[2],
                 "code": r[3], "severity": r[4], "message": r[5],
                 "detail": r[6]}
                for r in self.conn.execute("""
            SELECT alert_id, at, session_id, code, severity, message, detail
            FROM execution_alerts WHERE acknowledged = 0
            ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'error' THEN 1
                                   WHEN 'warning' THEN 2 ELSE 3 END,
                     COALESCE(at, '') DESC
            LIMIT ?
        """, (limit,))]

    def save_health(self, session_id: str, health: SystemHealth) -> int:
        self.conn.executemany("""
            INSERT OR REPLACE INTO system_health_readings
            (session_id, at, capability, state, detail, latency_ms, age_seconds)
            VALUES (?,?,?,?,?,?,?)
        """, [(session_id, _iso(health.at), c.value, r.state.value, r.detail,
               r.latency_ms, r.age_seconds)
              for c, r in health.readings.items()])
        self.conn.commit()
        return len(health.readings)

    def save_metrics(self, session_id: str, at: datetime,
                     metrics: ExecutionMetrics) -> None:
        self.conn.execute("""
            INSERT OR REPLACE INTO execution_metrics
            (session_id, at, metrics_json) VALUES (?,?,?)
        """, (session_id, _iso(at), _json(metrics.as_dict())))
        self.conn.commit()

    # ================================================================
    # Limit breaches
    # ================================================================

    def save_breach(self, breach: LimitBreach, detail: str, at: datetime,
                    session_id: Optional[str] = None,
                    order_id: Optional[str] = None) -> str:
        breach_id = f"lb-{uuid.uuid4().hex[:16]}"
        self.conn.execute("""
            INSERT INTO limit_breaches
            (breach_id, at, session_id, limit_name, detail, order_id, latched)
            VALUES (?,?,?,?,?,?,?)
        """, (breach_id, _iso(at), session_id, breach.value, detail, order_id,
              int(breach.requires_reactivation)))
        self.conn.commit()
        return breach_id

    def clear_breach(self, breach_id: str, actor: str, reason: str,
                     at: datetime) -> bool:
        """
        Record a human clearing a latched breach (spec §12).

        Requires an actor and a reason. A limit that could clear itself
        anonymously would not be a limit.
        """
        if not actor or not reason:
            raise ValueError("clearing a breach requires an actor and a reason")
        cursor = self.conn.execute("""
            UPDATE limit_breaches
            SET latched = 0, cleared_at = ?, cleared_by = ?, clear_reason = ?
            WHERE breach_id = ? AND latched = 1
        """, (_iso(at), actor, reason, breach_id))
        self.conn.commit()
        return cursor.rowcount > 0

    def latched_breaches(self) -> List[Dict[str, Any]]:
        return [{"breach_id": r[0], "at": r[1], "limit": r[2], "detail": r[3],
                 "session_id": r[4]}
                for r in self.conn.execute("""
            SELECT breach_id, at, limit_name, detail, session_id
            FROM limit_breaches WHERE latched = 1
            ORDER BY COALESCE(at, '') DESC
        """)]

    def save_comparison(self, comparison: EnvironmentComparison) -> str:
        comparison_id = f"cmp-{uuid.uuid4().hex[:16]}"
        summary = comparison.summary()
        self.conn.execute("""
            INSERT OR REPLACE INTO environment_comparisons
            (comparison_id, at, live_trades, live_days, conclusive, rows_json,
             notes_json)
            VALUES (?,?,?,?,?,?,?)
        """, (comparison_id, _iso(comparison.at), comparison.live_trades,
              comparison.live_days, int(comparison.is_conclusive),
              _json(summary["rows"]), _json(summary["notes"])))
        self.conn.commit()
        return comparison_id

    # ================================================================
    # Recovery (spec §36)
    # ================================================================

    def restore(self, governor, now: datetime) -> Dict[str, Any]:
        """
        Rebuild governance state after a restart.

        Approvals, the active session, the day's accumulated P&L and
        any latched limit breaches. A restarted process that forgot its
        daily loss limit had latched would resume trading through it.
        """
        approvals = self.load_approvals()
        for approval in approvals:
            governor.requests[approval.request_id] = approval

        session = self.active_session()
        latched = self.latched_breaches()
        day = None
        if session is not None:
            day = self.load_day_state(session.session_id,
                                      now.date().isoformat())
            if day is not None:
                session.day = day

        return {
            "approvals": len(approvals),
            "active_approval": (governor.active_approval(now).request_id
                                if governor.active_approval(now) else None),
            "effective_level": int(governor.effective_level(now)),
            "session_id": session.session_id if session else None,
            "session_state": session.state.value if session else None,
            "day_restored": day is not None,
            "latched_breaches": [b["limit"] for b in latched],
            "open_alerts": len(self.open_alerts()),
        }
