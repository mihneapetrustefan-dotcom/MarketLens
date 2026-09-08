"""
src/trading/repository.py
-------------------------------
Persistence for the Phase 25 loop, and the atomic cycle claim.

THE CLAIM IS THE WHOLE RESTART STORY (§10, §13)
---------------------------------------------------
`claim()` is a single conditional UPDATE:

    UPDATE trading_cycles SET claimed_by = ?
     WHERE cycle_id = ? AND status = 'claimed' AND claimed_by = ''

Either one worker's UPDATE changes a row or nobody's does. Two workers
that both wake on the same anchor therefore cannot both advance it, and
a worker that dies mid-cycle leaves a claimed row that `reclaim_stale`
can take back after a timeout. The pattern is Phase 23.5's, applied
unchanged, because it was tested there against a real race.

Combined with `cycle_id_for(session, anchor)` — deterministic — this
means a retried GitHub Actions job computes the SAME cycle id, finds
the row already terminal, and does nothing. That is §10's "repeated
scheduled job" case, and it costs one index lookup.

WHAT THIS MODULE MAY WRITE
------------------------------
Only the fourteen tables in `TRADING_LOOP_TABLES`. Execution orders,
fills, risk decisions, outcomes, attributions and memory are written by
the phases that own them, through their own repositories, and
`tests/trading/test_boundary.py` counts rows in every table in the
database before and after a cycle to prove it.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.data_access.trading_loop_schema import (
    TRADING_LOOP_TABLES, initialize_trading_loop_schema,
)
from src.domain.trading_loop_models import (
    LOOP_METHOD_VERSION, ActualPosition, CanonicalAccountState, CycleResult,
    CycleStatus, PaperSessionRecord, PaperValidation, PositionDelta,
    SignalEligibility, StageResult, TargetPosition, TradeLineage, TradingMode,
    require_utc,
)

#: A cycle claimed longer ago than this is assumed dead. Generous:
#: a GitHub Actions runner can be slow, and reclaiming a live cycle is
#: worse than waiting for a dead one.
DEFAULT_STALE_CLAIM_SECONDS = 3600.0


def _iso(moment: Optional[datetime]) -> Optional[str]:
    return moment.isoformat() if moment else None


def _parse(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class TradingLoopRepository:
    """Reads and writes the Phase 25 tables. Owns no business rule."""

    def __init__(self, conn: sqlite3.Connection,
                 method_version: str = LOOP_METHOD_VERSION):
        self.conn = conn
        self.method_version = method_version

    def initialize(self) -> None:
        initialize_trading_loop_schema(self.conn)

    # ---------------- sessions (§28, §29) ----------------

    def save_session(self, session: PaperSessionRecord) -> None:
        self.conn.execute("""
            INSERT INTO paper_loop_sessions
            (session_id, name, mode, method_version, broker_id, account_id,
             strategy_id, strategy_version, challenger_id, trained_model_id,
             model_status, experimental, constraint_version,
             feature_set_version, dataset_version, configuration_fingerprint,
             configuration_json, cycle_seconds, status, started_at, ended_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(session_id) DO UPDATE SET
                status = excluded.status, ended_at = excluded.ended_at,
                model_status = excluded.model_status
        """, (session.session_id, session.name, session.mode.value,
              session.method_version, session.broker_id, session.account_id,
              session.strategy_id, session.strategy_version,
              session.challenger_id, session.trained_model_id,
              session.model_status, int(session.experimental),
              session.constraint_version, session.feature_set_version,
              session.dataset_version, session.configuration_fingerprint,
              session.configuration_json, session.cycle_seconds,
              session.status, _iso(session.started_at), _iso(session.ended_at)))
        self.conn.commit()

    def get_session(self, session_id: str) -> Optional[PaperSessionRecord]:
        row = self.conn.execute("""
            SELECT session_id, name, mode, method_version, broker_id,
                   account_id, strategy_id, strategy_version, challenger_id,
                   trained_model_id, model_status, experimental,
                   constraint_version, feature_set_version, dataset_version,
                   configuration_fingerprint, configuration_json,
                   cycle_seconds, status, started_at, ended_at
              FROM paper_loop_sessions WHERE session_id = ?
        """, (session_id,)).fetchone()
        if row is None:
            return None
        mode, _ = TradingMode.resolve(row[2])
        # A stored session that does not resolve to PAPER cannot be
        # reconstructed -- `PaperSessionRecord` refuses a non-paper
        # mode at construction, which is the guarantee we want. Report
        # it as absent rather than raising into the caller's cycle.
        if mode is not TradingMode.PAPER:
            return None
        return PaperSessionRecord(
            session_id=row[0], name=row[1], mode=TradingMode.PAPER,
            method_version=row[3], broker_id=row[4], account_id=row[5],
            strategy_id=row[6], strategy_version=row[7], challenger_id=row[8],
            trained_model_id=row[9], model_status=row[10],
            experimental=bool(row[11]), constraint_version=row[12],
            feature_set_version=row[13], dataset_version=row[14],
            configuration_fingerprint=row[15], configuration_json=row[16],
            cycle_seconds=int(row[17] or 900), status=row[18],
            started_at=_parse(row[19]), ended_at=_parse(row[20]))

    def list_sessions(self, limit: int = 50) -> List[Dict[str, Any]]:
        return [{"session_id": r[0], "name": r[1], "mode": r[2],
                 "status": r[3], "broker_id": r[4], "account_id": r[5],
                 "challenger_id": r[6], "experimental": bool(r[7]),
                 "started_at": r[8], "ended_at": r[9]}
                for r in self.conn.execute("""
                    SELECT session_id, name, mode, status, broker_id,
                           account_id, challenger_id, experimental,
                           started_at, ended_at
                      FROM paper_loop_sessions
                     ORDER BY started_at DESC LIMIT ?
                """, (limit,))]

    # ---------------- cycles (§10, §13) ----------------

    def open_cycle(self, cycle_id: str, session_id: str, anchor: datetime,
                   at: datetime) -> bool:
        """
        Create the cycle row if it does not exist.

        Returns whether this call created it. INSERT OR IGNORE rather
        than a SELECT-then-INSERT: the check-then-act version has a
        window in which two workers both see nothing and both insert,
        and the primary key would then reject one of them with an
        exception instead of a False.
        """
        require_utc(anchor, "anchor")
        require_utc(at, "at")
        cursor = self.conn.execute("""
            INSERT OR IGNORE INTO trading_cycles
            (cycle_id, session_id, method_version, anchor, status)
            VALUES (?,?,?,?,?)
        """, (cycle_id, session_id, self.method_version, anchor.isoformat(),
              CycleStatus.CLAIMED.value))
        self.conn.commit()
        return cursor.rowcount == 1

    def claim(self, cycle_id: str, worker: str, at: datetime) -> bool:
        """
        Take exclusive ownership of a cycle. Atomic.

        Returns False when somebody else holds it or it already
        finished -- and a False is a normal outcome, not an error. A
        retried workflow gets False and exits having changed nothing,
        which is precisely the behaviour §10 asks for.
        """
        require_utc(at, "at")
        if not worker:
            raise ValueError("claiming a cycle requires a worker identity")
        cursor = self.conn.execute("""
            UPDATE trading_cycles
               SET claimed_by = ?, claimed_at = ?, worker = ?
             WHERE cycle_id = ? AND status = ? AND claimed_by = ''
        """, (worker, at.isoformat(), worker, cycle_id,
              CycleStatus.CLAIMED.value))
        self.conn.commit()
        return cursor.rowcount == 1

    def reclaim_stale(self, at: datetime,
                      timeout_seconds: float = DEFAULT_STALE_CLAIM_SECONDS
                      ) -> List[str]:
        """
        Release cycles whose worker never came back.

        Without this a crashed run blocks its anchor permanently, and
        the loop would silently stop advancing while every component
        reported healthy -- the failure mode that looks fine.
        """
        require_utc(at, "at")
        cutoff = (at - timedelta(seconds=timeout_seconds)).isoformat()
        rows = list(self.conn.execute("""
            SELECT cycle_id FROM trading_cycles
             WHERE status = ? AND claimed_by != '' AND claimed_at < ?
        """, (CycleStatus.CLAIMED.value, cutoff)))
        reclaimed: List[str] = []
        for (cycle_id,) in rows:
            cursor = self.conn.execute("""
                UPDATE trading_cycles SET claimed_by = '', claimed_at = NULL
                 WHERE cycle_id = ? AND status = ? AND claimed_at < ?
            """, (cycle_id, CycleStatus.CLAIMED.value, cutoff))
            if cursor.rowcount == 1:
                reclaimed.append(cycle_id)
        self.conn.commit()
        return reclaimed

    def cycle_status(self, cycle_id: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT status FROM trading_cycles WHERE cycle_id = ?",
            (cycle_id,)).fetchone()
        return str(row[0]) if row else None

    def save_cycle(self, result: CycleResult, finished_at: datetime) -> None:
        require_utc(finished_at, "finished_at")
        self.conn.execute("""
            UPDATE trading_cycles SET
                status = ?, mode = ?, health = ?, finished_at = ?,
                signals_seen = ?, signals_eligible = ?, targets_set = ?,
                intents_created = ?, intents_rejected = ?,
                orders_submitted = ?, orders_rejected = ?, fills_recorded = ?,
                positions_reconciled = ?, discrepancies = ?,
                outcomes_recorded = ?, blocks_json = ?, timestamps_json = ?,
                detail = ?
             WHERE cycle_id = ?
        """, (result.status.value, result.mode.value, result.health.value,
              finished_at.isoformat(), result.signals_seen,
              result.signals_eligible, result.targets_set,
              result.intents_created, result.intents_rejected,
              result.orders_submitted, result.orders_rejected,
              result.fills_recorded, result.positions_reconciled,
              result.discrepancies, result.outcomes_recorded,
              json.dumps([b.as_dict() for b in result.blocks]),
              json.dumps(result.timestamps.as_dict()), result.detail,
              result.cycle_id))
        for stage in result.stages:
            self.save_stage(stage)
        self.conn.commit()

    def save_stage(self, stage: StageResult) -> None:
        self.conn.execute("""
            INSERT INTO trading_cycle_stages
            (cycle_id, stage, outcome, detail, count, duration_ms,
             block_reason, started_at, finished_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(cycle_id, stage) DO UPDATE SET
                outcome = excluded.outcome, detail = excluded.detail,
                count = excluded.count, duration_ms = excluded.duration_ms,
                block_reason = excluded.block_reason,
                finished_at = excluded.finished_at
        """, (stage.cycle_id, stage.stage.value, stage.outcome.value,
              stage.detail, stage.count, stage.duration_ms,
              stage.block.reason.value if stage.block else "",
              _iso(stage.started_at), _iso(stage.finished_at)))

    def recent_cycles(self, session_id: Optional[str] = None,
                      limit: int = 50) -> List[Dict[str, Any]]:
        sql = ("SELECT cycle_id, session_id, anchor, status, mode, health, "
               "signals_seen, signals_eligible, targets_set, intents_created, "
               "orders_submitted, orders_rejected, fills_recorded, "
               "positions_reconciled, discrepancies, outcomes_recorded, "
               "blocks_json, finished_at FROM trading_cycles")
        params: Tuple[Any, ...] = ()
        if session_id:
            sql += " WHERE session_id = ?"
            params = (session_id,)
        sql += " ORDER BY anchor DESC LIMIT ?"
        params = params + (limit,)
        return [{"cycle_id": r[0], "session_id": r[1], "anchor": r[2],
                 "status": r[3], "mode": r[4], "health": r[5],
                 "signals_seen": r[6], "signals_eligible": r[7],
                 "targets_set": r[8], "intents_created": r[9],
                 "orders_submitted": r[10], "orders_rejected": r[11],
                 "fills_recorded": r[12], "positions_reconciled": r[13],
                 "discrepancies": r[14], "outcomes_recorded": r[15],
                 "blocks": json.loads(r[16] or "[]"), "finished_at": r[17]}
                for r in self.conn.execute(sql, params)]

    def last_finished_at(self, session_id: str) -> Optional[datetime]:
        row = self.conn.execute("""
            SELECT MAX(finished_at) FROM trading_cycles
             WHERE session_id = ? AND finished_at IS NOT NULL
        """, (session_id,)).fetchone()
        return _parse(row[0]) if row else None

    # ---------------- eligibility (§14) ----------------

    def save_eligibility(self, verdicts: Sequence[SignalEligibility]) -> int:
        for verdict in verdicts:
            self.conn.execute("""
                INSERT INTO signal_eligibility
                (cycle_id, signal_id, method_version, instrument_id, code,
                 detail, checks_performed, trained_model_id, model_status,
                 strategy_id, experimental, evaluated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(cycle_id, signal_id, method_version)
                DO UPDATE SET code = excluded.code, detail = excluded.detail,
                    checks_performed = excluded.checks_performed
            """, (verdict.cycle_id, verdict.signal_id, verdict.method_version,
                  verdict.instrument_id, verdict.code.value, verdict.detail,
                  verdict.checks_performed, verdict.trained_model_id,
                  verdict.model_status, verdict.strategy_id,
                  int(verdict.experimental), _iso(verdict.evaluated_at)))
        self.conn.commit()
        return len(verdicts)

    def eligibility_summary(self, limit_cycles: int = 20) -> Dict[str, int]:
        rows = self.conn.execute("""
            SELECT code, COUNT(*) FROM signal_eligibility
             WHERE cycle_id IN (SELECT cycle_id FROM trading_cycles
                                 ORDER BY anchor DESC LIMIT ?)
             GROUP BY code
        """, (limit_cycles,))
        return {str(r[0]): int(r[1]) for r in rows}

    # ---------------- targets, actuals, deltas (§16) ----------------

    def save_targets(self, targets: Sequence[TargetPosition]) -> int:
        for target in targets:
            self.conn.execute("""
                INSERT INTO position_targets
                (cycle_id, instrument_id, method_version, target_quantity,
                 target_weight, reference_price, signal_id, decision_id,
                 portfolio_id, reason, decided_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(cycle_id, instrument_id, method_version)
                DO UPDATE SET target_quantity = excluded.target_quantity,
                    target_weight = excluded.target_weight,
                    reference_price = excluded.reference_price
            """, (target.cycle_id, target.instrument_id, self.method_version,
                  target.target_quantity, target.target_weight,
                  target.reference_price, target.signal_id, target.decision_id,
                  target.portfolio_id, target.reason, _iso(target.decided_at)))
        self.conn.commit()
        return len(targets)

    def save_actuals(self, actuals: Sequence[ActualPosition]) -> int:
        for actual in actuals:
            self.conn.execute("""
                INSERT INTO position_actuals
                (cycle_id, instrument_id, method_version, quantity,
                 average_price, market_price, unrealized_pnl, realized_pnl,
                 origin, broker_id, account_id, observed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(cycle_id, instrument_id, method_version)
                DO UPDATE SET quantity = excluded.quantity,
                    average_price = excluded.average_price,
                    market_price = excluded.market_price,
                    unrealized_pnl = excluded.unrealized_pnl,
                    origin = excluded.origin
            """, (actual.cycle_id, actual.instrument_id, self.method_version,
                  actual.quantity, actual.average_price, actual.market_price,
                  actual.unrealized_pnl, actual.realized_pnl,
                  actual.origin.value, actual.broker_id, actual.account_id,
                  _iso(actual.observed_at)))
        self.conn.commit()
        return len(actuals)

    def save_deltas(self, cycle_id: str, deltas: Sequence[PositionDelta],
                    at: datetime) -> int:
        require_utc(at, "at")
        for delta in deltas:
            self.conn.execute("""
                INSERT INTO position_deltas
                (cycle_id, instrument_id, method_version, target_quantity,
                 actual_quantity, pending_quantity, outstanding, action, side,
                 computed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(cycle_id, instrument_id, method_version)
                DO UPDATE SET outstanding = excluded.outstanding,
                    action = excluded.action, side = excluded.side
            """, (cycle_id, delta.instrument_id, self.method_version,
                  delta.target_quantity, delta.actual_quantity,
                  delta.pending_quantity, delta.outstanding, delta.action,
                  delta.side, at.isoformat()))
        self.conn.commit()
        return len(deltas)

    def latest_positions(self) -> List[Dict[str, Any]]:
        """
        The most recent AUTHORITATIVE actual position per instrument.

        Filtered on origin inside the query rather than by the caller.
        §16's whole point is that a reader must not be able to get
        intentions back from a positions query, and a filter the caller
        applies is a filter the caller can forget.
        """
        return [{"instrument_id": r[0], "quantity": r[1],
                 "average_price": r[2], "market_price": r[3],
                 "unrealized_pnl": r[4], "realized_pnl": r[5],
                 "observed_at": r[6], "cycle_id": r[7]}
                for r in self.conn.execute("""
                    SELECT p.instrument_id, p.quantity, p.average_price,
                           p.market_price, p.unrealized_pnl, p.realized_pnl,
                           p.observed_at, p.cycle_id
                      FROM position_actuals p
                      JOIN (SELECT instrument_id, MAX(observed_at) AS newest
                              FROM position_actuals
                             WHERE origin = 'broker_reconciled'
                             GROUP BY instrument_id) latest
                        ON p.instrument_id = latest.instrument_id
                       AND p.observed_at = latest.newest
                     WHERE p.origin = 'broker_reconciled'
                     ORDER BY p.instrument_id
                """)]

    def outstanding_deltas(self, cycle_id: str) -> List[Dict[str, Any]]:
        return [{"instrument_id": r[0], "target_quantity": r[1],
                 "actual_quantity": r[2], "outstanding": r[3], "action": r[4]}
                for r in self.conn.execute("""
                    SELECT instrument_id, target_quantity, actual_quantity,
                           outstanding, action
                      FROM position_deltas
                     WHERE cycle_id = ? AND action NOT IN ('noop', 'no_target')
                     ORDER BY instrument_id
                """, (cycle_id,))]

    # ---------------- account state (§4) ----------------

    def save_account_state(self, state: CanonicalAccountState) -> None:
        self.conn.execute("""
            INSERT INTO loop_account_states
            (cycle_id, broker_id, account_id, method_version, source,
             base_currency, cash, equity, buying_power, available_funds,
             margin_used, margin_available, realized_pnl, unrealized_pnl,
             open_positions, open_orders, pending_orders, connection_state,
             detail, observed_at, synchronized_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(cycle_id, broker_id, account_id, method_version)
            DO UPDATE SET source = excluded.source, cash = excluded.cash,
                equity = excluded.equity, buying_power = excluded.buying_power,
                unrealized_pnl = excluded.unrealized_pnl,
                connection_state = excluded.connection_state,
                detail = excluded.detail
        """, (state.cycle_id, state.broker_id, state.account_id,
              self.method_version, state.source.value, state.base_currency,
              state.cash, state.equity, state.buying_power,
              state.available_funds, state.margin_used, state.margin_available,
              state.realized_pnl, state.unrealized_pnl, state.open_positions,
              state.open_orders, state.pending_orders, state.connection_state,
              state.detail, _iso(state.observed_at),
              _iso(state.synchronized_at)))
        self.conn.commit()

    def latest_account_state(self) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("""
            SELECT broker_id, account_id, source, base_currency, cash, equity,
                   buying_power, realized_pnl, unrealized_pnl, open_positions,
                   open_orders, connection_state, observed_at, detail
              FROM loop_account_states
             ORDER BY observed_at DESC LIMIT 1
        """).fetchone()
        if row is None:
            return None
        return {"broker_id": row[0], "account_id": row[1], "source": row[2],
                "base_currency": row[3], "cash": row[4], "equity": row[5],
                "buying_power": row[6], "realized_pnl": row[7],
                "unrealized_pnl": row[8], "open_positions": row[9],
                "open_orders": row[10], "connection_state": row[11],
                "observed_at": row[12], "detail": row[13]}

    # ---------------- lineage (§10, §18) ----------------

    def save_lineage(self, chains: Sequence[TradeLineage]) -> int:
        for chain in chains:
            self.conn.execute("""
                INSERT INTO trade_lineage
                (lineage_id, cycle_id, method_version, instrument_id,
                 signal_id, decision_id, intent_id, order_id, fill_id,
                 position_instrument_id, outcome_id, trained_model_id,
                 model_version, strategy_id, strategy_version, challenger_id,
                 complete, broken, recorded_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(lineage_id) DO UPDATE SET
                    cycle_id = excluded.cycle_id,
                    order_id = excluded.order_id, fill_id = excluded.fill_id,
                    position_instrument_id = excluded.position_instrument_id,
                    outcome_id = excluded.outcome_id,
                    complete = excluded.complete, broken = excluded.broken
            """, (_lineage_id(chain), chain.cycle_id, self.method_version,
                  chain.instrument_id, chain.signal_id, chain.decision_id,
                  chain.intent_id, chain.order_id, chain.fill_id,
                  chain.position_instrument_id, chain.outcome_id,
                  chain.trained_model_id, chain.model_version,
                  chain.strategy_id, chain.strategy_version,
                  chain.challenger_id, int(chain.is_complete),
                  int(chain.is_broken), _iso(chain.recorded_at)))
        self.conn.commit()
        return len(chains)

    def lineage_for(self, cycle_id: Optional[str] = None,
                    limit: int = 200) -> List[Dict[str, Any]]:
        sql = ("SELECT lineage_id, cycle_id, instrument_id, signal_id, "
               "decision_id, intent_id, order_id, fill_id, "
               "position_instrument_id, outcome_id, complete, broken, "
               "recorded_at FROM trade_lineage")
        params: Tuple[Any, ...] = ()
        if cycle_id:
            sql += " WHERE cycle_id = ?"
            params = (cycle_id,)
        sql += " ORDER BY recorded_at DESC LIMIT ?"
        return [{"lineage_id": r[0], "cycle_id": r[1], "instrument_id": r[2],
                 "signal_id": r[3], "decision_id": r[4], "intent_id": r[5],
                 "order_id": r[6], "fill_id": r[7],
                 "position_instrument_id": r[8], "outcome_id": r[9],
                 "complete": bool(r[10]), "broken": bool(r[11]),
                 "recorded_at": r[12]}
                for r in self.conn.execute(sql, params + (limit,))]

    def broken_lineage_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM trade_lineage WHERE broken = 1").fetchone()
        return int(row[0]) if row else 0

    # ---------------- validation (§21, §38) ----------------

    def save_validation(self, validation: PaperValidation) -> None:
        payload = validation.as_dict()
        self.conn.execute("""
            INSERT INTO paper_validations
            (validation_id, strategy_id, strategy_version, session_id,
             method_version, baseline_id, baseline_version, challenger_id,
             state, decisions, orders, fills, rejected_orders,
             completed_trades, risk_violations, operational_failures,
             signals_seen, realized_pnl, unrealized_pnl, max_drawdown,
             turnover, gross_exposure, dimensions_json, unmeasured_json,
             conclusive, configuration_fingerprint, notes, started_at, ended_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(validation_id) DO UPDATE SET
                state = excluded.state, decisions = excluded.decisions,
                orders = excluded.orders, fills = excluded.fills,
                rejected_orders = excluded.rejected_orders,
                completed_trades = excluded.completed_trades,
                risk_violations = excluded.risk_violations,
                operational_failures = excluded.operational_failures,
                signals_seen = excluded.signals_seen,
                realized_pnl = excluded.realized_pnl,
                unrealized_pnl = excluded.unrealized_pnl,
                max_drawdown = excluded.max_drawdown,
                turnover = excluded.turnover,
                gross_exposure = excluded.gross_exposure,
                dimensions_json = excluded.dimensions_json,
                unmeasured_json = excluded.unmeasured_json,
                conclusive = excluded.conclusive,
                notes = excluded.notes, ended_at = excluded.ended_at
        """, (validation.validation_id, validation.strategy_id,
              validation.strategy_version, validation.session_id,
              validation.method_version, validation.baseline_id,
              validation.baseline_version, validation.challenger_id,
              validation.state.value, validation.decisions, validation.orders,
              validation.fills, validation.rejected_orders,
              validation.completed_trades, validation.risk_violations,
              validation.operational_failures, validation.signals_seen,
              validation.realized_pnl, validation.unrealized_pnl,
              validation.max_drawdown, validation.turnover,
              validation.gross_exposure,
              json.dumps(payload["dimensions"]),
              json.dumps(payload["unmeasured"]),
              int(validation.is_conclusive()),
              validation.configuration_fingerprint, validation.notes,
              _iso(validation.started_at), _iso(validation.ended_at)))
        self.conn.commit()

    def get_validation(self, validation_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("""
            SELECT validation_id, strategy_id, strategy_version, session_id,
                   challenger_id, state, decisions, orders, fills,
                   rejected_orders, completed_trades, risk_violations,
                   operational_failures, realized_pnl, max_drawdown,
                   dimensions_json, unmeasured_json, conclusive, notes
              FROM paper_validations WHERE validation_id = ?
        """, (validation_id,)).fetchone()
        if row is None:
            return None
        return {"validation_id": row[0], "strategy_id": row[1],
                "strategy_version": row[2], "session_id": row[3],
                "challenger_id": row[4], "state": row[5], "decisions": row[6],
                "orders": row[7], "fills": row[8], "rejected_orders": row[9],
                "completed_trades": row[10], "risk_violations": row[11],
                "operational_failures": row[12], "realized_pnl": row[13],
                "max_drawdown": row[14],
                "dimensions": json.loads(row[15] or "[]"),
                "unmeasured": json.loads(row[16] or "[]"),
                "conclusive": bool(row[17]), "notes": row[18]}

    def list_validations(self, limit: int = 50) -> List[Dict[str, Any]]:
        return [{"validation_id": r[0], "strategy_id": r[1],
                 "strategy_version": r[2], "challenger_id": r[3],
                 "state": r[4], "orders": r[5], "fills": r[6],
                 "completed_trades": r[7], "realized_pnl": r[8],
                 "conclusive": bool(r[9]), "unmeasured": json.loads(r[10] or "[]"),
                 "started_at": r[11]}
                for r in self.conn.execute("""
                    SELECT validation_id, strategy_id, strategy_version,
                           challenger_id, state, orders, fills,
                           completed_trades, realized_pnl, conclusive,
                           unmeasured_json, started_at
                      FROM paper_validations
                     ORDER BY started_at DESC LIMIT ?
                """, (limit,))]

    def save_review(self, validation_id: str, from_state: str, to_state: str,
                    reviewer: str, reason: str, at: datetime) -> str:
        """
        Append a human decision. Never updates.

        Same shape as Phase 18's `promote()` and Phase 24's `review()`:
        a named reviewer and a reason, neither with a default, because
        an approval nobody signed is not an approval.
        """
        require_utc(at, "at")
        if not reviewer:
            raise ValueError("a review requires a named reviewer")
        if not reason:
            raise ValueError("a review requires a reason")
        review_id = "pvr-" + uuid.uuid4().hex[:16]
        self.conn.execute("""
            INSERT INTO paper_validation_reviews
            (review_id, validation_id, from_state, to_state, reviewer, reason,
             method_version, reviewed_at)
            VALUES (?,?,?,?,?,?,?,?)
        """, (review_id, validation_id, from_state, to_state, reviewer, reason,
              self.method_version, at.isoformat()))
        self.conn.execute(
            "UPDATE paper_validations SET state = ? WHERE validation_id = ?",
            (to_state, validation_id))
        self.conn.commit()
        return review_id

    def reviews_for(self, validation_id: str) -> List[Dict[str, Any]]:
        return [{"review_id": r[0], "from_state": r[1], "to_state": r[2],
                 "reviewer": r[3], "reason": r[4], "reviewed_at": r[5]}
                for r in self.conn.execute("""
                    SELECT review_id, from_state, to_state, reviewer, reason,
                           reviewed_at FROM paper_validation_reviews
                     WHERE validation_id = ? ORDER BY reviewed_at
                """, (validation_id,))]

    # ---------------- audit (§32) ----------------

    def audit(self, actor: str, action: str, at: datetime, *,
              session_id: str = "", cycle_id: str = "", subject_id: str = "",
              detail: str = "") -> str:
        require_utc(at, "at")
        audit_id = "tla-" + uuid.uuid4().hex[:16]
        self.conn.execute("""
            INSERT INTO trading_loop_audit
            (audit_id, method_version, actor, action, session_id, cycle_id,
             subject_id, detail, occurred_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (audit_id, self.method_version, actor, action, session_id,
              cycle_id, subject_id, detail, at.isoformat()))
        self.conn.commit()
        return audit_id

    def audit_trail(self, limit: int = 100) -> List[Dict[str, Any]]:
        return [{"audit_id": r[0], "actor": r[1], "action": r[2],
                 "session_id": r[3], "cycle_id": r[4], "subject_id": r[5],
                 "detail": r[6], "occurred_at": r[7]}
                for r in self.conn.execute("""
                    SELECT audit_id, actor, action, session_id, cycle_id,
                           subject_id, detail, occurred_at
                      FROM trading_loop_audit
                     ORDER BY occurred_at DESC LIMIT ?
                """, (limit,))]


def _lineage_id(chain: TradeLineage) -> str:
    """
    Deterministic lineage identity, keyed on the ORDER.

    Not on the cycle. A chain is COMPLETED ACROSS CYCLES: the order is
    created in one, the fill arrives in a later one, the outcome later
    still. Keying on the cycle produced a fresh partial row each time
    and the original never gained its fill -- so `is_complete` read
    False forever while the trade was in fact fully recorded elsewhere.

    Falls back to the cycle and instrument only when there is no order
    yet, which is the pre-submission case where nothing downstream can
    exist to attach.
    """
    import hashlib
    key = chain.order_id or chain.intent_id
    raw = (f"order|{key}" if key
           else f"cycle|{chain.cycle_id}|{chain.instrument_id}|"
                f"{chain.signal_id or ''}")
    return "lin-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
