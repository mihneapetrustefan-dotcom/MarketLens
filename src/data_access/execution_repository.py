"""
src/data_access/execution_repository.py
--------------------------------------------
Persistence and recovery for Phase 14 execution (spec §53, §60, §61).

RECOVERY IS WHY THIS EXISTS
-------------------------------
An execution system that cannot say what it did before the crash is not
safe to restart, because the first thing it will do is re-derive
intents it already acted on. `restore` rebuilds the orchestrator's
book, its state history and — critically — its deduplication sets, so a
restarted process recognises its own earlier work.

The dangerous case is an order that was in flight when the process
died: submitted, with no outcome recorded. `in_flight_orders` finds
those, and the caller moves them to UNKNOWN so reconciliation asks the
broker rather than anything assuming.

ONE TICK IS ONE TRANSACTION
-------------------------------
`save_execution` writes an order, its transitions, its fills and its
events together. A partial write is what produces "a position exists
but no fill explains it", which reconciliation then correctly reports
as corruption — so the boundary removes the failure mode rather than
detecting it afterwards.

APPEND-ONLY MEANS APPEND-ONLY
---------------------------------
Events, reconciliation records, audit entries and state transitions are
inserted, never updated. There is deliberately no method on this class
that rewrites one. An ORDER legitimately changes state, so it upserts
— but on its primary key only, never on `INSERT OR REPLACE`, which
resolves a conflict on any unique index by deleting the row it
collided with. Its HISTORY does not change at all.

NO SECRETS, STRUCTURALLY
----------------------------
No method here accepts a credential, and no column exists to hold one.
A database dump of these tables can be shared without redaction.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from src.domain.broker_models import (
    AuditEvent, Broker, BrokerAccount, BrokerCapability, BrokerHealth,
    BrokerInstrumentMapping, CanonicalOrderSide, CanonicalOrderType,
    CanonicalTimeInForce, ExecutionEnvironment, ExecutionError, ExecutionEvent,
    ExecutionEventType, ExecutionFill, ExecutionOrder, ExecutionOrderState,
    ExecutionPermission, ExecutionRejectCode, MismatchKind, OrderStateTransition,
    PositionAccounting, ReconciliationMismatch, ReconciliationRecord,
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


class ExecutionRepository:
    """Reads and writes everything Phase 14 produces."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---------------- brokers and accounts ----------------

    def save_broker(self, broker: Broker) -> str:
        self.conn.execute("""
            INSERT OR REPLACE INTO brokers
            (broker_id, name, environment, adapter, enabled, implemented,
             created_at, metadata_json)
            VALUES (?,?,?,?,?,?,?,?)
        """, (broker.broker_id, broker.name, broker.environment.value,
              broker.adapter, int(broker.enabled), int(broker.implemented),
              _iso(broker.created_at), _json(broker.metadata)))
        self.conn.commit()
        return broker.broker_id

    def get_broker(self, broker_id: str) -> Optional[Broker]:
        row = self.conn.execute("""
            SELECT broker_id, name, environment, adapter, enabled, implemented,
                   created_at, metadata_json
            FROM brokers WHERE broker_id = ?
        """, (broker_id,)).fetchone()
        if row is None:
            return None
        return Broker(
            broker_id=row[0], name=row[1],
            environment=ExecutionEnvironment(row[2]), adapter=row[3],
            enabled=bool(row[4]), implemented=bool(row[5]),
            created_at=_parse(row[6]), metadata=_loads(row[7], {}))

    def list_brokers(self) -> List[Broker]:
        return [b for b in (self.get_broker(r[0]) for r in self.conn.execute(
            "SELECT broker_id FROM brokers ORDER BY broker_id")) if b]

    def save_account(self, account: BrokerAccount) -> str:
        self.conn.execute("""
            INSERT OR REPLACE INTO broker_accounts
            (account_id, broker_id, name, environment, base_currency, enabled,
             position_accounting, permissions_json, linked_reference,
             created_at, metadata_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (account.account_id, account.broker_id, account.name,
              account.environment.value, account.base_currency,
              int(account.enabled), account.position_accounting.value,
              _json([p.value for p in account.permissions]),
              account.linked_reference, _iso(account.created_at),
              _json(account.metadata)))
        self.conn.commit()
        return account.account_id

    def get_account(self, account_id: str) -> Optional[BrokerAccount]:
        row = self.conn.execute("""
            SELECT account_id, broker_id, name, environment, base_currency,
                   enabled, position_accounting, permissions_json,
                   linked_reference, created_at, metadata_json
            FROM broker_accounts WHERE account_id = ?
        """, (account_id,)).fetchone()
        if row is None:
            return None
        return BrokerAccount(
            account_id=row[0], broker_id=row[1], name=row[2],
            environment=ExecutionEnvironment(row[3]), base_currency=row[4],
            enabled=bool(row[5]),
            position_accounting=PositionAccounting(row[6]),
            permissions=tuple(ExecutionPermission(p)
                              for p in _loads(row[7], [])),
            linked_reference=row[8], created_at=_parse(row[9]),
            metadata=_loads(row[10], {}))

    def accounts_for(self, broker_id: str) -> List[BrokerAccount]:
        return [a for a in (self.get_account(r[0]) for r in self.conn.execute(
            "SELECT account_id FROM broker_accounts WHERE broker_id = ? "
            "ORDER BY account_id", (broker_id,))) if a]

    def save_capability(self, capability: BrokerCapability,
                        at: Optional[datetime] = None) -> None:
        self.conn.execute("""
            INSERT OR REPLACE INTO broker_capability
            (broker_id, capability_json, position_accounting,
             rate_limit_per_minute, recorded_at, notes)
            VALUES (?,?,?,?,?,?)
        """, (capability.broker_id, _json(capability.as_dict()),
              capability.position_accounting.value,
              capability.rate_limit_per_minute, _iso(at), capability.notes))
        self.conn.commit()

    def save_health(self, health: BrokerHealth) -> None:
        self.conn.execute("""
            INSERT OR REPLACE INTO broker_health
            (broker_id, at, state, latency_ms, consecutive_failures, detail)
            VALUES (?,?,?,?,?,?)
        """, (health.broker_id, _iso(health.at), health.state.value,
              health.latency_ms, health.consecutive_failures, health.detail))
        self.conn.commit()

    # ---------------- instrument mappings ----------------

    def save_mapping(self, mapping: BrokerInstrumentMapping) -> None:
        self.conn.execute("""
            INSERT OR REPLACE INTO broker_instrument_mapping
            (canonical_instrument_id, broker_id, broker_symbol, venue,
             asset_class, currency, tick_size, lot_size, minimum_quantity,
             quantity_increment, price_precision, contract_multiplier,
             timezone_name, trading_hours, tradable, broker_payload_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (mapping.canonical_instrument_id, mapping.broker_id,
              mapping.broker_symbol, mapping.venue, mapping.asset_class,
              mapping.currency, mapping.tick_size, mapping.lot_size,
              mapping.minimum_quantity, mapping.quantity_increment,
              mapping.price_precision, mapping.contract_multiplier,
              mapping.timezone_name, mapping.trading_hours,
              int(mapping.tradable), _json(mapping.broker_payload)))
        self.conn.commit()

    def save_mappings(self, mappings: Iterable[BrokerInstrumentMapping]) -> int:
        count = 0
        for mapping in mappings:
            self.save_mapping(mapping)
            count += 1
        return count

    # ---------------- the transactional write ----------------

    def save_execution(self, order: ExecutionOrder,
                       transitions: Sequence[OrderStateTransition] = (),
                       fills: Sequence[ExecutionFill] = (),
                       events: Sequence[ExecutionEvent] = (),
                       errors: Sequence[ExecutionError] = (),
                       audit: Sequence[AuditEvent] = ()) -> None:
        """
        Write one order and everything it produced, together.

        A partial write here is the state reconciliation would report
        as corruption, so the transaction boundary removes the failure
        rather than leaving it to be detected.
        """
        self._save_order(order)
        self._save_transitions(transitions)
        self._save_fills(fills)
        self._save_events(events)
        self._save_errors(errors)
        self._save_audit(audit)
        self.conn.commit()

    def _save_order(self, order: ExecutionOrder) -> None:
        """
        Upsert on the PRIMARY KEY only.

        Deliberately not `INSERT OR REPLACE`: that resolves a conflict
        on ANY unique index by deleting the conflicting row, so a
        second order carrying an existing idempotency key would
        silently erase the first one it was supposed to collide with.
        Conflicting on `order_id` is an ordinary state update;
        conflicting on `idempotency_key` must raise.
        """
        self.conn.execute("""
            INSERT INTO execution_orders
            (order_id, intent_id, broker_id, account_id, instrument_id, side,
             quantity, order_type, time_in_force, limit_price, stop_price,
             state, filled_quantity, average_fill_price, reject_code,
             reject_detail, idempotency_key, client_order_id, broker_order_id,
             broker_symbol, correlation_id, signal_id, prediction_id,
             model_version, strategy_id, portfolio_id, decision_id,
             execution_policy, environment, intent_at, validated_at,
             submitted_at, acknowledged_at, terminal_at, expires_at,
             decision_price, reference_price, bid, ask, submitted_price,
             commission, fees, note)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(order_id) DO UPDATE SET
                state = excluded.state,
                filled_quantity = excluded.filled_quantity,
                average_fill_price = excluded.average_fill_price,
                reject_code = excluded.reject_code,
                reject_detail = excluded.reject_detail,
                broker_order_id = excluded.broker_order_id,
                validated_at = excluded.validated_at,
                submitted_at = excluded.submitted_at,
                acknowledged_at = excluded.acknowledged_at,
                terminal_at = excluded.terminal_at,
                commission = excluded.commission,
                fees = excluded.fees,
                submitted_price = excluded.submitted_price,
                note = excluded.note
        """, (order.order_id, order.intent_id, order.broker_id,
              order.account_id, order.instrument_id, order.side.value,
              order.quantity, order.order_type.value,
              order.time_in_force.value, order.limit_price, order.stop_price,
              order.state.value, order.filled_quantity,
              order.average_fill_price,
              order.reject_code.value if order.reject_code else None,
              order.reject_detail, order.idempotency_key,
              order.client_order_id, order.broker_order_id,
              order.broker_symbol, order.correlation_id, order.signal_id,
              order.prediction_id, order.model_version, order.strategy_id,
              order.portfolio_id, order.decision_id, order.execution_policy,
              order.environment.value, _iso(order.intent_at),
              _iso(order.validated_at), _iso(order.submitted_at),
              _iso(order.acknowledged_at), _iso(order.terminal_at),
              _iso(order.expires_at), order.decision_price,
              order.reference_price, order.bid, order.ask,
              order.submitted_price, order.commission, order.fees, order.note))

    def _save_transitions(self,
                          transitions: Sequence[OrderStateTransition]) -> None:
        # INSERT OR IGNORE, never REPLACE: history is append-only, and
        # a re-save must not be able to rewrite what was recorded.
        self.conn.executemany("""
            INSERT OR IGNORE INTO order_state_history
            (order_id, sequence, from_state, to_state, at, reason, event_id,
             correlation_id)
            VALUES (?,?,?,?,?,?,?,?)
        """, [(t.order_id, t.sequence,
               t.from_state.value if t.from_state else None,
               t.to_state.value, _iso(t.at), t.reason, t.event_id,
               t.correlation_id) for t in transitions])

    def _save_fills(self, fills: Sequence[ExecutionFill]) -> None:
        self.conn.executemany("""
            INSERT OR REPLACE INTO execution_fills
            (fill_id, order_id, broker_id, account_id, instrument_id, side,
             quantity, price, filled_at, execution_id, broker_order_id,
             commission, fees, exchange_fees, financing, taxes, currency,
             reference_price, liquidity, idempotency_key, correlation_id,
             raw_payload_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [(f.fill_id, f.order_id, f.broker_id, f.account_id,
               f.instrument_id, f.side.value, f.quantity, f.price,
               _iso(f.filled_at), f.execution_id, f.broker_order_id,
               f.commission, f.fees, f.exchange_fees, f.financing, f.taxes,
               f.currency, f.reference_price, f.liquidity, f.idempotency_key,
               f.correlation_id, _json(f.raw_broker_payload)) for f in fills])

    def _save_events(self, events: Sequence[ExecutionEvent]) -> None:
        self.conn.executemany("""
            INSERT OR IGNORE INTO execution_events
            (event_id, event_type, at, received_at, source, broker_id,
             account_id, order_id, broker_order_id, fill_id, instrument_id,
             correlation_id, idempotency_key, sequence, payload_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [(e.event_id, e.event_type.value, _iso(e.at), _iso(e.received_at),
               e.source, e.broker_id, e.account_id, e.order_id,
               e.broker_order_id, e.fill_id, e.instrument_id, e.correlation_id,
               e.idempotency_key, e.sequence, _json(e.payload))
              for e in events])

    def _save_errors(self, errors: Sequence[ExecutionError]) -> None:
        self.conn.executemany("""
            INSERT OR IGNORE INTO execution_errors
            (error_id, at, code, message, broker_id, account_id, order_id,
             correlation_id, retryable, context_json)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, [(e.error_id, _iso(e.at), e.code.value, e.message, e.broker_id,
               e.account_id, e.order_id, e.correlation_id, int(e.retryable),
               _json(e.context)) for e in errors])

    def _save_audit(self, audit: Sequence[AuditEvent]) -> None:
        self.conn.executemany("""
            INSERT OR IGNORE INTO execution_audit
            (audit_id, at, action, actor, subject_type, subject_id,
             correlation_id, detail, payload_json)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, [(a.audit_id, _iso(a.at), a.action, a.actor, a.subject_type,
               a.subject_id, a.correlation_id, a.detail, _json(a.payload))
              for a in audit])

    def save_reconciliation(self, record: ReconciliationRecord) -> None:
        """Append-only. There is no method that edits a stored record."""
        self.conn.execute("""
            INSERT OR IGNORE INTO reconciliation_records
            (reconciliation_id, broker_id, account_id, at, scope,
             orders_compared, fills_compared, positions_compared,
             checks_performed, is_clean, mismatches_json, correlation_id, detail)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (record.reconciliation_id, record.broker_id, record.account_id,
              _iso(record.at), record.scope, record.orders_compared,
              record.fills_compared, record.positions_compared,
              record.checks_performed, int(record.is_clean),
              _json(record.mismatches), record.correlation_id, record.detail))
        self.conn.commit()

    # ---------------- reads ----------------

    def _order_from_row(self, r: Tuple[Any, ...]) -> ExecutionOrder:
        return ExecutionOrder(
            order_id=r[0], intent_id=r[1], broker_id=r[2], account_id=r[3],
            instrument_id=r[4], side=CanonicalOrderSide(r[5]), quantity=r[6],
            order_type=CanonicalOrderType(r[7]),
            time_in_force=CanonicalTimeInForce(r[8]),
            limit_price=r[9], stop_price=r[10],
            state=ExecutionOrderState(r[11]), filled_quantity=r[12],
            average_fill_price=r[13],
            reject_code=ExecutionRejectCode(r[14]) if r[14] else None,
            reject_detail=r[15] or "", idempotency_key=r[16] or "",
            client_order_id=r[17] or "", broker_order_id=r[18],
            broker_symbol=r[19] or "", correlation_id=r[20] or "",
            signal_id=r[21], prediction_id=r[22], model_version=r[23],
            strategy_id=r[24], portfolio_id=r[25], decision_id=r[26],
            execution_policy=r[27] or "market",
            environment=ExecutionEnvironment(r[28]),
            intent_at=_parse(r[29]), validated_at=_parse(r[30]),
            submitted_at=_parse(r[31]), acknowledged_at=_parse(r[32]),
            terminal_at=_parse(r[33]), expires_at=_parse(r[34]),
            decision_price=r[35], reference_price=r[36], bid=r[37], ask=r[38],
            submitted_price=r[39], commission=r[40] or 0.0, fees=r[41] or 0.0,
            note=r[42] or "")

    ORDER_COLUMNS = """
        order_id, intent_id, broker_id, account_id, instrument_id, side,
        quantity, order_type, time_in_force, limit_price, stop_price, state,
        filled_quantity, average_fill_price, reject_code, reject_detail,
        idempotency_key, client_order_id, broker_order_id, broker_symbol,
        correlation_id, signal_id, prediction_id, model_version, strategy_id,
        portfolio_id, decision_id, execution_policy, environment, intent_at,
        validated_at, submitted_at, acknowledged_at, terminal_at, expires_at,
        decision_price, reference_price, bid, ask, submitted_price,
        commission, fees, note
    """

    def get_order(self, order_id: str) -> Optional[ExecutionOrder]:
        row = self.conn.execute(
            f"SELECT {self.ORDER_COLUMNS} FROM execution_orders WHERE order_id = ?",
            (order_id,)).fetchone()
        return self._order_from_row(row) if row else None

    def orders_for(self, broker_id: Optional[str] = None,
                   account_id: Optional[str] = None,
                   limit: int = 500) -> List[ExecutionOrder]:
        sql = f"SELECT {self.ORDER_COLUMNS} FROM execution_orders"
        clauses, params = [], []
        if broker_id:
            clauses.append("broker_id = ?")
            params.append(broker_id)
        if account_id:
            clauses.append("account_id = ?")
            params.append(account_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY COALESCE(intent_at, '') DESC LIMIT ?"
        params.append(limit)
        return [self._order_from_row(r) for r in self.conn.execute(sql, params)]

    def in_flight_orders(self) -> List[ExecutionOrder]:
        """
        Orders handed to a broker with no outcome recorded.

        After a crash these are the ones that matter: the venue may
        hold an order nothing local knows the fate of. The caller moves
        them to UNKNOWN rather than assuming either way.
        """
        states = (ExecutionOrderState.SUBMITTING.value,
                  ExecutionOrderState.SUBMITTED.value)
        return [self._order_from_row(r) for r in self.conn.execute(
            f"SELECT {self.ORDER_COLUMNS} FROM execution_orders "
            f"WHERE state IN (?,?)", states)]

    def transitions_for(self, order_id: str) -> List[OrderStateTransition]:
        return [OrderStateTransition(
            order_id=r[0], sequence=r[1],
            from_state=ExecutionOrderState(r[2]) if r[2] else None,
            to_state=ExecutionOrderState(r[3]), at=_parse(r[4]),
            reason=r[5] or "", event_id=r[6], correlation_id=r[7] or "")
            for r in self.conn.execute("""
                SELECT order_id, sequence, from_state, to_state, at, reason,
                       event_id, correlation_id
                FROM order_state_history WHERE order_id = ?
                ORDER BY sequence ASC
            """, (order_id,))]

    def fills_for(self, order_id: Optional[str] = None,
                  broker_id: Optional[str] = None) -> List[ExecutionFill]:
        sql = """
            SELECT fill_id, order_id, broker_id, account_id, instrument_id,
                   side, quantity, price, filled_at, execution_id,
                   broker_order_id, commission, fees, exchange_fees, financing,
                   taxes, currency, reference_price, liquidity,
                   idempotency_key, correlation_id, raw_payload_json
            FROM execution_fills
        """
        clauses, params = [], []
        if order_id:
            clauses.append("order_id = ?")
            params.append(order_id)
        if broker_id:
            clauses.append("broker_id = ?")
            params.append(broker_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY COALESCE(filled_at, '') ASC"
        return [ExecutionFill(
            fill_id=r[0], order_id=r[1], broker_id=r[2], account_id=r[3],
            instrument_id=r[4], side=CanonicalOrderSide(r[5]), quantity=r[6],
            price=r[7], filled_at=_parse(r[8]), execution_id=r[9],
            broker_order_id=r[10], commission=r[11], fees=r[12],
            exchange_fees=r[13], financing=r[14], taxes=r[15], currency=r[16],
            reference_price=r[17], liquidity=r[18] or "",
            idempotency_key=r[19] or "", correlation_id=r[20] or "",
            raw_broker_payload=_loads(r[21], {}))
            for r in self.conn.execute(sql, params)]

    def seen_event_keys(self) -> Set[str]:
        """
        Every event key already applied.

        Restoring these is what keeps a redelivery after reconnect from
        being treated as new — and reconnecting is precisely what makes
        a venue replay its recent events.
        """
        return {r[0] for r in self.conn.execute(
            "SELECT idempotency_key FROM execution_events "
            "WHERE idempotency_key != ''")}

    def reconciliations_for(self, broker_id: Optional[str] = None,
                            limit: int = 50) -> List[Dict[str, Any]]:
        sql = """
            SELECT reconciliation_id, broker_id, account_id, at, scope,
                   orders_compared, fills_compared, positions_compared,
                   checks_performed, is_clean, mismatches_json, detail
            FROM reconciliation_records
        """
        params: List[Any] = []
        if broker_id:
            sql += " WHERE broker_id = ?"
            params.append(broker_id)
        sql += " ORDER BY at DESC LIMIT ?"
        params.append(limit)
        return [{
            "reconciliation_id": r[0], "broker_id": r[1], "account_id": r[2],
            "at": r[3], "scope": r[4], "orders_compared": r[5],
            "fills_compared": r[6], "positions_compared": r[7],
            "checks_performed": r[8], "is_clean": bool(r[9]),
            "mismatches": _loads(r[10], []), "detail": r[11] or "",
        } for r in self.conn.execute(sql, params)]

    def errors_for(self, limit: int = 50) -> List[Dict[str, Any]]:
        return [{
            "error_id": r[0], "at": r[1], "code": r[2], "message": r[3],
            "broker_id": r[4], "order_id": r[5], "retryable": bool(r[6]),
        } for r in self.conn.execute("""
            SELECT error_id, at, code, message, broker_id, order_id, retryable
            FROM execution_errors ORDER BY COALESCE(at, '') DESC LIMIT ?
        """, (limit,))]

    def audit_for(self, subject_id: Optional[str] = None,
                  limit: int = 100) -> List[Dict[str, Any]]:
        sql = """
            SELECT audit_id, at, action, actor, subject_type, subject_id,
                   correlation_id, detail
            FROM execution_audit
        """
        params: List[Any] = []
        if subject_id:
            sql += " WHERE subject_id = ?"
            params.append(subject_id)
        sql += " ORDER BY COALESCE(at, '') DESC LIMIT ?"
        params.append(limit)
        return [{
            "audit_id": r[0], "at": r[1], "action": r[2], "actor": r[3],
            "subject_type": r[4], "subject_id": r[5], "correlation_id": r[6],
            "detail": r[7],
        } for r in self.conn.execute(sql, params)]

    # ---------------- recovery ----------------

    def restore(self, orchestrator, broker_id: Optional[str] = None,
                account_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Rebuild an orchestrator's book from storage (spec §60).

        Loads orders, their full transition history, their fills and
        the seen-event keys. The idempotency index is rebuilt from the
        orders themselves inside `orchestrator.seed`, so it cannot
        drift out of step with the book it protects.
        """
        orders = self.orders_for(broker_id, account_id, limit=10_000)
        transitions: List[OrderStateTransition] = []
        for order in orders:
            transitions.extend(self.transitions_for(order.order_id))
        fills = self.fills_for(broker_id=broker_id)
        event_keys = self.seen_event_keys()

        orchestrator.seed(orders=orders, fills=fills, transitions=transitions,
                          event_keys=event_keys)

        in_flight = [o for o in orders if o.state.is_in_flight]
        return {
            "orders": len(orders),
            "transitions": len(transitions),
            "fills": len(fills),
            "event_keys": len(event_keys),
            "in_flight": len(in_flight),
            "in_flight_ids": [o.order_id for o in in_flight],
        }

    # ---------------- controls ----------------

    def save_control(self, key: str, enabled: bool, at: datetime,
                     actor: str = "system", reason: str = "") -> None:
        if key == "live_execution_enabled":
            raise ValueError(
                "there is no live execution control to set in Phase 14; "
                "real-money execution is absent, not disabled")
        self.conn.execute("""
            INSERT OR REPLACE INTO execution_controls
            (control_key, enabled, updated_at, actor, reason)
            VALUES (?,?,?,?,?)
        """, (key, int(enabled), _iso(at), actor, reason))
        self.conn.commit()

    def load_controls(self) -> Dict[str, bool]:
        return {r[0]: bool(r[1]) for r in self.conn.execute(
            "SELECT control_key, enabled FROM execution_controls")}
