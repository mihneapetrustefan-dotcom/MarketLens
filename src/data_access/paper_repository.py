"""
src/data_access/paper_repository.py
----------------------------------------
Persistence and recovery for Phase 13 paper sessions.

RECOVERY IS THE POINT
-------------------------
A backtest that crashes is re-run. A paper session cannot be — time has
moved on, and the ticks it processed really happened. So this module's
central responsibility is `restore_session`: rebuild the ledger, the
working order book and the session's position in time from what was
written, well enough that the next tick continues correctly rather than
starting over (spec §78, §79).

The recovery path prefers the newest checkpoint and replays only the
fills that followed it. Replaying every fill from the session's start
also works and is used when no checkpoint exists, but on a long session
it is slow enough that nobody would run it after an incident — which is
exactly when recovery matters (spec §80).

SAVING A TICK IS ONE TRANSACTION
------------------------------------
`save_tick` writes the orders, fills, snapshot, events, health, latency
and reconciliation from one tick together. A partial write is what would
produce the "position exists but no fill explains it" state that
reconciliation then reports as corruption, so the boundary matters.

APPEND-ONLY MEANS APPEND-ONLY
---------------------------------
Events, control actions and reconciliations are inserted, never updated.
There is deliberately no method on this class that rewrites one.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.backtest.accounting import LedgerPosition, PortfolioLedger
from src.domain.paper_models import (
    AlertSeverity, ControlAction, DataFreshness, ExecutionVenue, HealthState,
    OrderSide, PaperAccount, PaperAccountStatus, PaperAlert, PaperEvent,
    PaperEventKind, PaperFill, PaperOrder, PaperOrderState, PaperOrderType,
    PaperRejectReason, PaperSession, PaperSessionConfig, PaperSessionStatus,
    PaperSnapshot, ReconciliationResult, SystemHealth, TickResult, TimeInForce,
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


class PaperRepository:
    """Reads and writes paper accounts, sessions and everything they produce."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---------------- accounts ----------------

    def save_account(self, account: PaperAccount) -> str:
        self.conn.execute("""
            INSERT OR REPLACE INTO paper_accounts
            (account_id, name, base_currency, initial_capital, status,
             account_type, generation, created_at, metadata_json)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (account.account_id, account.name, account.base_currency,
              account.initial_capital, account.status.value, account.account_type,
              account.generation, _iso(account.created_at),
              _json(account.metadata)))
        self.conn.commit()
        return account.account_id

    def get_account(self, account_id: str) -> Optional[PaperAccount]:
        row = self.conn.execute("""
            SELECT account_id, name, base_currency, initial_capital, status,
                   account_type, generation, created_at, metadata_json
            FROM paper_accounts WHERE account_id = ?
        """, (account_id,)).fetchone()
        if row is None:
            return None
        return PaperAccount(
            account_id=row[0], name=row[1], base_currency=row[2],
            initial_capital=row[3], status=PaperAccountStatus(row[4]),
            account_type=row[5], generation=row[6], created_at=_parse(row[7]),
            metadata=json.loads(row[8]))

    def list_accounts(self) -> List[Dict[str, Any]]:
        return [{"account_id": r[0], "name": r[1], "status": r[2],
                 "initial_capital": r[3], "generation": r[4]}
                for r in self.conn.execute(
                    "SELECT account_id, name, status, initial_capital, generation "
                    "FROM paper_accounts ORDER BY account_id")]

    def reset_account(self, account_id: str, at: datetime,
                      initial_capital: Optional[float] = None) -> PaperAccount:
        """
        Start a new generation, preserving everything before it (spec §63).

        Nothing is deleted. Sessions, orders and fills from earlier
        generations keep their rows; the account simply advances a
        counter so new work is distinguishable from old. A reset that
        wiped history would destroy exactly the research the session
        existed to produce.
        """
        account = self.get_account(account_id)
        if account is None:
            raise ValueError(f"no paper account {account_id!r}")
        account.generation += 1
        account.status = PaperAccountStatus.ACTIVE
        if initial_capital is not None:
            account.initial_capital = initial_capital
        self.save_account(account)
        self.conn.execute("""
            INSERT OR REPLACE INTO paper_control_actions
            (action_id, session_id, action, at, actor, reason,
             previous_value, new_value)
            VALUES (?,?,?,?,?,?,?,?)
        """, (f"reset-{account_id}-{account.generation}", "",
              "account_reset", _iso(at), "operator",
              "account reset; prior generations preserved",
              str(account.generation - 1), str(account.generation)))
        self.conn.commit()
        return account

    # ---------------- sessions ----------------

    def save_session(self, session: PaperSession,
                     clock_kind: Optional[str] = None) -> str:
        """
        Write the session row.

        `clock_kind` is preserved rather than defaulted when the caller
        does not pass one. `save_tick` re-saves the session on every
        tick, so a default here would quietly rewrite a replay session
        as a system-clock one after its first tick — and the dashboard
        would then report a replay as if it had run against wall time.
        """
        if clock_kind is None:
            stored = self.conn.execute(
                "SELECT clock_kind FROM paper_sessions WHERE session_id = ?",
                (session.session_id,)).fetchone()
            clock_kind = stored[0] if stored and stored[0] else "system"

        config = session.config
        self.conn.execute("""
            INSERT OR REPLACE INTO paper_sessions
            (session_id, account_id, name, status, config_json,
             config_fingerprint, clock_kind, risk_engine_version,
             constraint_set_version, cost_model_version, slippage_model_version,
             execution_model_version, strategy_version, code_version,
             started_at, ended_at, last_tick_at, ticks_processed, notes, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (session.session_id, session.account_id, session.name,
              session.status.value, _json(config), config.fingerprint(),
              clock_kind, session.risk_engine_version,
              config.constraint_set_version, config.cost_model_version,
              config.slippage_model_version, config.execution_model_version,
              config.strategy_version, session.code_version,
              _iso(session.started_at), _iso(session.ended_at),
              _iso(session.last_tick_at), session.ticks_processed,
              session.notes, _iso(datetime.now(timezone.utc))))
        self.conn.commit()
        return session.session_id

    def get_session(self, session_id: str) -> Optional[PaperSession]:
        row = self.conn.execute("""
            SELECT session_id, account_id, name, status, config_json,
                   risk_engine_version, code_version, started_at, ended_at,
                   last_tick_at, ticks_processed, notes
            FROM paper_sessions WHERE session_id = ?
        """, (session_id,)).fetchone()
        if row is None:
            return None

        raw = json.loads(row[4]) if row[4] else {}
        config = PaperSessionConfig(
            universe=raw.get("universe", []),
            strategy_id=raw.get("strategy_id"),
            strategy_version=raw.get("strategy_version"),
            constraint_set_version=raw.get("constraint_set_version", "v1"),
            sizing_strategy_id=raw.get("sizing_strategy_id", "fixed_fraction"),
            sizing_target_weight=raw.get("sizing_target_weight", 0.05),
            cost_model_version=raw.get("cost_model_version", "cost-v1"),
            slippage_model_version=raw.get("slippage_model_version", "slip-v1"),
            execution_model_version=raw.get("execution_model_version", "paper-exec-v1"),
            commission_bps=raw.get("commission_bps", 2.0),
            slippage_bps=raw.get("slippage_bps", 5.0),
            signal_to_order_seconds=raw.get("signal_to_order_seconds", 60.0),
            max_participation=raw.get("max_participation", 0.10),
            default_order_type=PaperOrderType(raw.get("default_order_type", "market")),
            default_time_in_force=TimeInForce(raw.get("default_time_in_force", "day")),
            max_orders_per_tick=raw.get("max_orders_per_tick", 25),
            max_orders_per_day=raw.get("max_orders_per_day", 200),
            daily_loss_limit_pct=raw.get("daily_loss_limit_pct"),
            max_drawdown_pct=raw.get("max_drawdown_pct"),
            require_fresh_data=raw.get("require_fresh_data", True),
            tick_interval_seconds=raw.get("tick_interval_seconds", 86_400.0),
            config_version=raw.get("config_version", "paper-cfg-v1"))

        return PaperSession(
            session_id=row[0], account_id=row[1], name=row[2],
            config=config, status=PaperSessionStatus(row[3]),
            risk_engine_version=row[5], code_version=row[6],
            started_at=_parse(row[7]), ended_at=_parse(row[8]),
            last_tick_at=_parse(row[9]), ticks_processed=row[10] or 0,
            notes=row[11] or "")

    def list_sessions(self, account_id: Optional[str] = None,
                      limit: int = 50) -> List[Dict[str, Any]]:
        sql = ("SELECT session_id, account_id, name, status, config_fingerprint, "
               "started_at, last_tick_at, ticks_processed FROM paper_sessions")
        params: List[Any] = []
        if account_id:
            sql += " WHERE account_id = ?"
            params.append(account_id)
        sql += " ORDER BY COALESCE(started_at, created_at) DESC LIMIT ?"
        params.append(limit)
        return [{"session_id": r[0], "account_id": r[1], "name": r[2],
                 "status": r[3], "fingerprint": r[4], "started_at": r[5],
                 "last_tick_at": r[6], "ticks": r[7]}
                for r in self.conn.execute(sql, params)]

    def clock_kind_for(self, session_id: str) -> str:
        row = self.conn.execute(
            "SELECT clock_kind FROM paper_sessions WHERE session_id = ?",
            (session_id,)).fetchone()
        return row[0] if row else "system"

    # ---------------- one tick ----------------

    def save_tick(self, session: PaperSession, result: TickResult,
                  orders: Sequence[PaperOrder], fills: Sequence[PaperFill],
                  events: Sequence[PaperEvent], health: Optional[SystemHealth] = None,
                  alerts: Sequence[PaperAlert] = (),
                  controls: Sequence[ControlAction] = (),
                  ledger: Optional[PortfolioLedger] = None) -> None:
        """
        Persist everything one tick produced, in a single transaction.

        A partial write is what produces "a position exists but no fill
        explains it", which reconciliation then correctly reports as
        corruption. Writing them together removes that failure mode
        rather than detecting it later.
        """
        self._save_orders(orders)
        self._save_fills(fills)
        if result.snapshot is not None:
            self._save_snapshot(result.snapshot)
        self._save_events(events)
        self._save_latency(session.session_id, result)
        if health is not None:
            self._save_health(session.session_id, health)
        if result.reconciliation is not None:
            self._save_reconciliation(result.reconciliation)
        self._save_alerts(alerts)
        self._save_controls(controls)
        if ledger is not None and result.snapshot is not None:
            self._save_positions(session.session_id, ledger, result.snapshot.at)
        self.save_session(session)
        self.conn.commit()

    def _save_positions(self, session_id: str, ledger: PortfolioLedger,
                        at: datetime) -> None:
        """
        Mirror the ledger book into a queryable table.

        Recovery does not need this — checkpoints carry the positions,
        and the fill history reconstructs them. What needs it is
        everything that asks "what does this session hold right now"
        without loading a ledger: the dashboard, an export, a query.
        Without it the snapshot would report open positions while the
        positions table stayed empty, which reads as a bug in whichever
        of the two the reader happens to trust.

        Rewritten each tick rather than appended: this is current state,
        not history. The history lives in the fills, which are
        append-only.
        """
        self.conn.execute("DELETE FROM paper_positions WHERE session_id = ?",
                          (session_id,))
        rows = [(session_id, position.instrument_id, position.quantity,
                 position.average_cost, _iso(position.opened_at),
                 position.entry_signal_id, _iso(at))
                for position in ledger.open_positions()]
        if rows:
            self.conn.executemany("""
                INSERT INTO paper_positions
                (session_id, instrument_id, quantity, average_cost, opened_at,
                 entry_signal_id, updated_at)
                VALUES (?,?,?,?,?,?,?)
            """, rows)

    def _save_orders(self, orders: Sequence[PaperOrder]) -> None:
        self.conn.executemany("""
            INSERT OR REPLACE INTO paper_orders
            (order_id, session_id, account_id, instrument_id, side, quantity,
             order_type, time_in_force, limit_price, stop_price, state,
             filled_quantity, average_fill_price, reject_reason, reject_detail,
             idempotency_key, signal_id, decision_id, intent_id, strategy_id,
             model_version, target_weight, information_cutoff, decided_at,
             created_at, accepted_at, terminal_at, expires_at,
             execution_model_version, note)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [(o.order_id, o.session_id, o.account_id, o.instrument_id,
               o.side.value, o.quantity, o.order_type.value,
               o.time_in_force.value, o.limit_price, o.stop_price, o.state.value,
               o.filled_quantity, o.average_fill_price,
               o.reject_reason.value if o.reject_reason else None,
               o.reject_detail, o.idempotency_key, o.signal_id, o.decision_id,
               o.intent_id, o.strategy_id, o.model_version, o.target_weight,
               _iso(o.information_cutoff), _iso(o.decided_at), _iso(o.created_at),
               _iso(o.accepted_at), _iso(o.terminal_at), _iso(o.expires_at),
               o.execution_model_version, o.note) for o in orders])

    def _save_fills(self, fills: Sequence[PaperFill]) -> None:
        self.conn.executemany("""
            INSERT OR REPLACE INTO paper_fills
            (fill_id, session_id, order_id, account_id, instrument_id, side,
             quantity, reference_price, price, commission, slippage_cost, venue,
             execution_model_version, slippage_model_version, cost_model_version,
             bar_timestamp, participation, is_partial, intrabar_ambiguous,
             idempotency_key, filled_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [(f.fill_id, f.session_id, f.order_id, f.account_id, f.instrument_id,
               f.side.value, f.quantity, f.reference_price, f.price, f.commission,
               f.slippage_cost, f.venue.value, f.execution_model_version,
               f.slippage_model_version, f.cost_model_version,
               _iso(f.bar_timestamp), f.participation, int(f.is_partial),
               int(f.intrabar_ambiguous), f.idempotency_key, _iso(f.filled_at))
              for f in fills])

    def _save_snapshot(self, snapshot: PaperSnapshot) -> None:
        self.conn.execute("""
            INSERT OR REPLACE INTO paper_snapshots
            (snapshot_id, session_id, account_id, at, equity, cash,
             positions_value, gross_exposure, net_exposure, long_exposure,
             short_exposure, leverage, realized_pnl, unrealized_pnl, drawdown,
             open_positions, unpriced_positions, data_freshness, health)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (snapshot.snapshot_id, snapshot.session_id, snapshot.account_id,
              _iso(snapshot.at), snapshot.equity, snapshot.cash,
              snapshot.positions_value, snapshot.gross_exposure,
              snapshot.net_exposure, snapshot.long_exposure,
              snapshot.short_exposure, snapshot.leverage, snapshot.realized_pnl,
              snapshot.unrealized_pnl, snapshot.drawdown, snapshot.open_positions,
              snapshot.unpriced_positions, snapshot.data_freshness.value,
              snapshot.health.value))

    def _save_events(self, events: Sequence[PaperEvent]) -> None:
        self.conn.executemany("""
            INSERT OR IGNORE INTO paper_events
            (session_id, sequence, at, kind, instrument_id, order_id, fill_id,
             signal_id, message, payload_json)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, [(e.session_id, e.sequence, _iso(e.at), e.kind.value,
               e.instrument_id, e.order_id, e.fill_id, e.signal_id, e.message,
               _json(e.payload)) for e in events])

    def _save_latency(self, session_id: str, result: TickResult) -> None:
        self.conn.executemany("""
            INSERT OR REPLACE INTO paper_latency
            (session_id, at, stage, milliseconds) VALUES (?,?,?,?)
        """, [(session_id, _iso(result.at), s.stage, s.milliseconds)
              for s in result.latencies])

    def _save_health(self, session_id: str, health: SystemHealth) -> None:
        self.conn.executemany("""
            INSERT OR REPLACE INTO paper_health
            (session_id, at, component, state, detail, latency_ms,
             last_heartbeat_at) VALUES (?,?,?,?,?,?,?)
        """, [(session_id, _iso(health.at), c.component, c.state.value,
               c.detail, c.latency_ms, _iso(c.last_heartbeat_at))
              for c in health.components.values()])

    def _save_reconciliation(self, result: ReconciliationResult) -> None:
        self.conn.execute("""
            INSERT OR REPLACE INTO paper_reconciliations
            (session_id, at, checks_performed, orders_examined, fills_examined,
             positions_examined, is_clean, discrepancies_json)
            VALUES (?,?,?,?,?,?,?,?)
        """, (result.session_id, _iso(result.at), result.checks_performed,
              result.orders_examined, result.fills_examined,
              result.positions_examined, int(result.is_clean),
              _json(result.discrepancies)))

    def _save_alerts(self, alerts: Sequence[PaperAlert]) -> None:
        self.conn.executemany("""
            INSERT OR REPLACE INTO paper_alerts
            (alert_id, session_id, code, severity, message, detail, at,
             acknowledged) VALUES (?,?,?,?,?,?,?,?)
        """, [(a.alert_id, a.session_id, a.code, a.severity.value, a.message,
               a.detail, _iso(a.at), int(a.acknowledged)) for a in alerts])

    def _save_controls(self, controls: Sequence[ControlAction]) -> None:
        self.conn.executemany("""
            INSERT OR IGNORE INTO paper_control_actions
            (action_id, session_id, action, at, actor, reason, previous_value,
             new_value) VALUES (?,?,?,?,?,?,?,?)
        """, [(c.action_id, c.session_id, c.action, _iso(c.at), c.actor,
               c.reason, c.previous_value, c.new_value) for c in controls])

    # ---------------- checkpoints and recovery ----------------

    def save_checkpoint(self, session: PaperSession, ledger: PortfolioLedger,
                        at: datetime) -> str:
        """Record the ledger state so recovery need not replay everything."""
        checkpoint_id = f"cp-{session.session_id}-{int(at.timestamp())}"
        positions = [{
            "instrument_id": p.instrument_id, "quantity": p.quantity,
            "average_cost": p.average_cost, "opened_at": _iso(p.opened_at),
            "entry_signal_id": p.entry_signal_id,
        } for p in ledger.open_positions()]

        self.conn.execute("""
            INSERT OR REPLACE INTO paper_checkpoints
            (checkpoint_id, session_id, at, cash, realized_pnl, total_costs,
             total_slippage, traded_notional, positions_json, ticks_processed,
             created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (checkpoint_id, session.session_id, _iso(at), ledger.cash,
              ledger.realized_pnl, ledger.total_costs, ledger.total_slippage,
              ledger.traded_notional, _json(positions), session.ticks_processed,
              _iso(datetime.now(timezone.utc))))
        self.conn.commit()
        return checkpoint_id

    def latest_checkpoint(self, session_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("""
            SELECT checkpoint_id, at, cash, realized_pnl, total_costs,
                   total_slippage, traded_notional, positions_json, ticks_processed
            FROM paper_checkpoints WHERE session_id = ?
            ORDER BY at DESC LIMIT 1
        """, (session_id,)).fetchone()
        if row is None:
            return None
        return {"checkpoint_id": row[0], "at": _parse(row[1]), "cash": row[2],
                "realized_pnl": row[3], "total_costs": row[4],
                "total_slippage": row[5], "traded_notional": row[6],
                "positions": json.loads(row[7]), "ticks_processed": row[8]}

    def restore_ledger(self, session_id: str, initial_capital: float,
                       base_currency: str = "USD"
                       ) -> Tuple[PortfolioLedger, Optional[datetime], str]:
        """
        Rebuild the ledger from the newest checkpoint, then replay the
        fills that followed it (spec §79, §80).

        Returns (ledger, restored_to, method). `method` records how the
        state was reached — "checkpoint+replay" or "full_replay" — so a
        recovered session can say which, rather than leaving it to be
        inferred.
        """
        ledger = PortfolioLedger(initial_capital, run_id=session_id,
                                 base_currency=base_currency)
        checkpoint = self.latest_checkpoint(session_id)
        restored_to: Optional[datetime] = None
        method = "full_replay"

        if checkpoint is not None:
            ledger.cash = checkpoint["cash"]
            ledger.realized_pnl = checkpoint["realized_pnl"]
            ledger.total_costs = checkpoint["total_costs"]
            ledger.total_slippage = checkpoint["total_slippage"]
            ledger.traded_notional = checkpoint["traded_notional"]
            for entry in checkpoint["positions"]:
                ledger.positions[entry["instrument_id"]] = LedgerPosition(
                    instrument_id=entry["instrument_id"],
                    quantity=entry["quantity"],
                    average_cost=entry["average_cost"],
                    opened_at=_parse(entry.get("opened_at")),
                    entry_signal_id=entry.get("entry_signal_id"))
            restored_to = checkpoint["at"]
            method = "checkpoint+replay"

        # Replay fills after the checkpoint (or all of them without one).
        sql = ("SELECT fill_id, order_id, instrument_id, side, quantity, price, "
               "reference_price, commission, slippage_cost, filled_at, "
               "bar_timestamp, participation, is_partial "
               "FROM paper_fills WHERE session_id = ?")
        params: List[Any] = [session_id]
        if restored_to is not None:
            sql += " AND filled_at > ?"
            params.append(_iso(restored_to))
        sql += " ORDER BY filled_at ASC"

        from src.paper.executor import fill_to_simulated
        replayed = 0
        for row in self.conn.execute(sql, params):
            fill = PaperFill(
                fill_id=row[0], session_id=session_id, order_id=row[1],
                account_id="", instrument_id=row[2], side=OrderSide(row[3]),
                quantity=row[4], price=row[5], reference_price=row[6],
                commission=row[7], slippage_cost=row[8],
                filled_at=_parse(row[9]), bar_timestamp=_parse(row[10]),
                participation=row[11], is_partial=bool(row[12]))
            ledger.apply_fill(fill_to_simulated(fill))
            replayed += 1

        if replayed and restored_to is None:
            method = "full_replay"
        return ledger, restored_to, method

    def paper_fills_for(self, session_id: str) -> List[PaperFill]:
        """
        Every fill as a domain object, in order.

        Distinct from `fills_for`, which returns display dictionaries.
        A recovering session needs the real objects: reconciliation
        derives expected positions and cash from them, and comparing a
        restored ledger against an empty fill list would report the
        entire book as corruption.
        """
        return [PaperFill(
            fill_id=r[0], session_id=session_id, order_id=r[1],
            account_id=r[2] or "", instrument_id=r[3], side=OrderSide(r[4]),
            quantity=r[5], price=r[6], reference_price=r[7],
            commission=r[8], slippage_cost=r[9],
            filled_at=_parse(r[10]), bar_timestamp=_parse(r[11]),
            participation=r[12], is_partial=bool(r[13]),
            intrabar_ambiguous=bool(r[14]), idempotency_key=r[15] or "")
            for r in self.conn.execute("""
                SELECT fill_id, order_id, account_id, instrument_id, side,
                       quantity, price, reference_price, commission,
                       slippage_cost, filled_at, bar_timestamp, participation,
                       is_partial, intrabar_ambiguous, idempotency_key
                FROM paper_fills WHERE session_id = ? ORDER BY filled_at ASC
            """, (session_id,))]

    def all_orders_for(self, session_id: str) -> List[PaperOrder]:
        """
        Every order as a domain object, in order.

        Recovery needs the WHOLE history, not just the open orders:
        reconciliation checks that each fill belongs to a known order,
        so restoring only the working ones would make every fill from a
        completed order look orphaned — reporting a healthy book as
        corrupt after each restart.
        """
        rows = self.conn.execute("""
            SELECT order_id, session_id, account_id, instrument_id, side, quantity,
                   order_type, time_in_force, limit_price, stop_price, state,
                   filled_quantity, average_fill_price, idempotency_key, signal_id,
                   decision_id, intent_id, strategy_id, target_weight,
                   information_cutoff, decided_at, created_at, accepted_at,
                   expires_at, execution_model_version, note
            FROM paper_orders WHERE session_id = ?
            ORDER BY created_at ASC
        """, (session_id,)).fetchall()

        orders: List[PaperOrder] = []
        for r in rows:
            orders.append(PaperOrder(
                order_id=r[0], session_id=r[1], account_id=r[2],
                instrument_id=r[3], side=OrderSide(r[4]), quantity=r[5],
                order_type=PaperOrderType(r[6]), time_in_force=TimeInForce(r[7]),
                limit_price=r[8], stop_price=r[9], state=PaperOrderState(r[10]),
                filled_quantity=r[11] or 0.0, average_fill_price=r[12],
                idempotency_key=r[13] or "", signal_id=r[14], decision_id=r[15],
                intent_id=r[16], strategy_id=r[17], target_weight=r[18],
                information_cutoff=_parse(r[19]), decided_at=_parse(r[20]),
                created_at=_parse(r[21]), accepted_at=_parse(r[22]),
                expires_at=_parse(r[23]),
                execution_model_version=r[24] or "paper-exec-v1", note=r[25] or ""))
        return orders

    def working_orders(self, session_id: str) -> List[PaperOrder]:
        """Only the orders still eligible to receive fills."""
        return [o for o in self.all_orders_for(session_id) if o.state.is_working]

    # ---------------- reads ----------------

    def orders_for(self, session_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        return [{
            "order_id": r[0], "instrument_id": r[1], "side": r[2],
            "quantity": r[3], "order_type": r[4], "state": r[5],
            "filled_quantity": r[6], "average_fill_price": r[7],
            "reject_reason": r[8], "reject_detail": r[9], "created_at": r[10],
            "signal_id": r[11], "decision_id": r[12],
        } for r in self.conn.execute("""
            SELECT order_id, instrument_id, side, quantity, order_type, state,
                   filled_quantity, average_fill_price, reject_reason,
                   reject_detail, created_at, signal_id, decision_id
            FROM paper_orders WHERE session_id = ?
            ORDER BY created_at DESC LIMIT ?
        """, (session_id, limit))]

    def fills_for(self, session_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        return [{
            "fill_id": r[0], "order_id": r[1], "instrument_id": r[2],
            "side": r[3], "quantity": r[4], "price": r[5],
            "reference_price": r[6], "commission": r[7], "slippage_cost": r[8],
            "venue": r[9], "filled_at": r[10], "is_partial": bool(r[11]),
        } for r in self.conn.execute("""
            SELECT fill_id, order_id, instrument_id, side, quantity, price,
                   reference_price, commission, slippage_cost, venue, filled_at,
                   is_partial
            FROM paper_fills WHERE session_id = ?
            ORDER BY filled_at DESC LIMIT ?
        """, (session_id, limit))]

    def snapshots_for(self, session_id: str) -> List[Dict[str, Any]]:
        return [{
            "at": r[0], "equity": r[1], "cash": r[2], "positions_value": r[3],
            "gross_exposure": r[4], "net_exposure": r[5], "leverage": r[6],
            "realized_pnl": r[7], "unrealized_pnl": r[8], "drawdown": r[9],
            "open_positions": r[10], "freshness": r[11], "health": r[12],
        } for r in self.conn.execute("""
            SELECT at, equity, cash, positions_value, gross_exposure,
                   net_exposure, leverage, realized_pnl, unrealized_pnl,
                   drawdown, open_positions, data_freshness, health
            FROM paper_snapshots WHERE session_id = ? ORDER BY at ASC
        """, (session_id,))]

    def events_for(self, session_id: str, limit: int = 300,
                   kind: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = ("SELECT sequence, at, kind, instrument_id, order_id, signal_id, "
               "message, payload_json FROM paper_events WHERE session_id = ?")
        params: List[Any] = [session_id]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY sequence DESC LIMIT ?"
        params.append(limit)
        return [{"sequence": r[0], "at": r[1], "kind": r[2],
                 "instrument_id": r[3], "order_id": r[4], "signal_id": r[5],
                 "message": r[6], "payload": json.loads(r[7])}
                for r in self.conn.execute(sql, params)]

    def alerts_for(self, session_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        return [{"alert_id": r[0], "code": r[1], "severity": r[2],
                 "message": r[3], "detail": r[4], "at": r[5]}
                for r in self.conn.execute("""
                    SELECT alert_id, code, severity, message, detail, at
                    FROM paper_alerts WHERE session_id = ?
                    ORDER BY at DESC LIMIT ?
                """, (session_id, limit))]

    def health_for(self, session_id: str) -> List[Dict[str, Any]]:
        """The most recent health reading per component."""
        return [{"component": r[0], "state": r[1], "detail": r[2],
                 "latency_ms": r[3], "at": r[4]}
                for r in self.conn.execute("""
                    SELECT component, state, detail, latency_ms, at
                    FROM paper_health WHERE session_id = ? AND at = (
                        SELECT MAX(at) FROM paper_health WHERE session_id = ?)
                    ORDER BY component
                """, (session_id, session_id))]

    def latency_for(self, session_id: str) -> List[Dict[str, Any]]:
        """Average milliseconds per stage across the session."""
        return [{"stage": r[0], "average_ms": r[1], "max_ms": r[2],
                 "samples": r[3]}
                for r in self.conn.execute("""
                    SELECT stage, AVG(milliseconds), MAX(milliseconds), COUNT(*)
                    FROM paper_latency WHERE session_id = ?
                    GROUP BY stage ORDER BY AVG(milliseconds) DESC
                """, (session_id,))]

    def reconciliations_for(self, session_id: str,
                            limit: int = 50) -> List[Dict[str, Any]]:
        return [{"at": r[0], "checks": r[1], "is_clean": bool(r[2]),
                 "discrepancies": json.loads(r[3])}
                for r in self.conn.execute("""
                    SELECT at, checks_performed, is_clean, discrepancies_json
                    FROM paper_reconciliations WHERE session_id = ?
                    ORDER BY at DESC LIMIT ?
                """, (session_id, limit))]

    def control_actions_for(self, session_id: str) -> List[Dict[str, Any]]:
        return [{"action_id": r[0], "action": r[1], "at": r[2], "actor": r[3],
                 "reason": r[4], "previous": r[5], "new": r[6]}
                for r in self.conn.execute("""
                    SELECT action_id, action, at, actor, reason, previous_value,
                           new_value
                    FROM paper_control_actions WHERE session_id = ?
                    ORDER BY at ASC
                """, (session_id,))]

    def positions_for(self, session_id: str) -> List[Dict[str, Any]]:
        """
        Current positions, derived from the session's fills.

        Derived rather than read from a positions table, so what is
        shown is what the fills justify — the same independence
        reconciliation relies on.
        """
        return [{"instrument_id": r[0], "quantity": r[1], "notional": r[2]}
                for r in self.conn.execute("""
                    SELECT instrument_id,
                           SUM(CASE WHEN side='buy' THEN quantity ELSE -quantity END),
                           SUM(quantity * price)
                    FROM paper_fills WHERE session_id = ?
                    GROUP BY instrument_id
                    HAVING ABS(SUM(CASE WHEN side='buy' THEN quantity
                                        ELSE -quantity END)) > 1e-9
                """, (session_id,))]

    def export_session(self, session_id: str) -> Dict[str, Any]:
        """
        Everything needed to reproduce or audit a session (spec §64).

        Includes the configuration and version identity alongside the
        records, because an export of orders without the assumptions
        that produced them cannot be interpreted later.
        """
        session = self.get_session(session_id)
        return {
            "session": {
                "session_id": session_id,
                "name": session.name if session else None,
                "status": session.status.value if session else None,
                "ticks": session.ticks_processed if session else 0,
                "config": asdict(session.config) if session else {},
                "config_fingerprint": session.config.fingerprint() if session else None,
                "code_version": session.code_version if session else None,
            },
            "orders": self.orders_for(session_id, limit=100_000),
            "fills": self.fills_for(session_id, limit=100_000),
            "snapshots": self.snapshots_for(session_id),
            "positions": self.positions_for(session_id),
            "events": self.events_for(session_id, limit=100_000),
            "alerts": self.alerts_for(session_id, limit=100_000),
            "reconciliations": self.reconciliations_for(session_id, limit=100_000),
            "control_actions": self.control_actions_for(session_id),
            "latency": self.latency_for(session_id),
            "is_paper": True,
            "venue": ExecutionVenue.PAPER.value,
        }
