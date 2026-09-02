"""
src/execution/service.py
-----------------------------
The execution service facade (Phase 14, spec §45, §46, §47).

WHY THIS IS NOT AN HTTP API
-------------------------------
The spec asks for endpoints. This repository has no web framework, no
server, no request lifecycle and no user accounts — every phase runs as
a batch job and publishes a static page. Adding FastAPI for one phase
would be the parallel architecture §0 forbids, and would leave twelve
phases reachable one way and one another.

So the OPERATIONS §45 lists are implemented here, as a typed facade,
and the CLI and dashboard consume them. What an HTTP layer would add is
transport and a session; what it would not change is the operation set,
the permission model, or the safety guarantees. When this project grows
a server, these methods are what it would expose, and the routes map
one to one:

    GET  /brokers                      list_brokers()
    GET  /brokers/{id}                 get_broker()
    GET  /brokers/{id}/health          broker_health()
    GET  /brokers/{id}/capabilities    capabilities()
    GET  /accounts                     list_accounts()
    GET  /accounts/{id}                get_account()
    GET  /accounts/{id}/positions      positions()
    GET  /accounts/{id}/orders         orders()
    GET  /accounts/{id}/fills          fills()
    POST /execution/validate           validate()
    POST /execution/dry-run            dry_run()
    POST /execution/order              submit()
    POST /orders/{id}/cancel           cancel()
    POST /orders/{id}/modify           modify()
    GET  /execution/events             events()
    GET  /execution/reconciliation     reconciliations()

THE SAFETY PROPERTY §46 REQUIRES
------------------------------------
`submit()` cannot place a real-money order. Not because a route guard
rejects it, but because every layer beneath refuses: the environment
cannot be LIVE on any registered broker, the safety gate refuses LIVE
before anything else runs, and no adapter capable of a live order
exists. The check here is the fourth of four.

PERMISSIONS WITHOUT USERS
-----------------------------
There is no auth system to reuse, so a caller presents its permissions
explicitly as a `Caller`. That is weaker than an authenticated session
and this module says so rather than implying otherwise. What it does
preserve is the distinction that matters: reading execution state and
causing execution are different permissions, and the live one cannot be
granted at all — `check_permission` refuses `LIVE_EXECUTION_ADMIN` even
when a caller claims to hold it.

NO BROKER OBJECT LEAVES THIS FACADE
---------------------------------------
Every return value is a canonical type or a plain dict. An SDK object
returned from here would put a venue's vocabulary into the dashboard,
the CLI and every future consumer at once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.data_access.execution_repository import ExecutionRepository
from src.domain.broker_models import (
    AccountSnapshot, Broker, BrokerAccount, BrokerCapability, BrokerHealth,
    DryRunResult, ExecutionEnvironment, ExecutionError, ExecutionEvent,
    ExecutionFill, ExecutionOrder, ExecutionOrderState, ExecutionPermission,
    ExecutionRejectCode, ExecutionResult, PositionSnapshot,
    READ_ONLY_PERMISSIONS, ReconciliationRecord, ValidationResult, explain,
)
from src.execution.orchestrator import (
    BrokerRegistry, ExecutionOrchestrator, IntentRequest,
)
from src.execution.safety import ExecutionSafety, SafetyVerdict


@dataclass
class Caller:
    """
    Who is asking, and what they may do.

    Explicit rather than inferred from a session, because there are no
    sessions here. `read_only` is the default so that a caller
    constructed without thought cannot execute.
    """
    name: str = "operator"
    permissions: Tuple[ExecutionPermission, ...] = READ_ONLY_PERMISSIONS

    @staticmethod
    def read_only(name: str = "viewer") -> "Caller":
        return Caller(name=name, permissions=READ_ONLY_PERMISSIONS)

    @staticmethod
    def paper_trader(name: str = "paper-operator") -> "Caller":
        return Caller(name=name, permissions=READ_ONLY_PERMISSIONS + (
            ExecutionPermission.DRY_RUN_EXECUTION,
            ExecutionPermission.PAPER_EXECUTION))

    def holds(self, permission: ExecutionPermission) -> bool:
        return permission in self.permissions


class PermissionDenied(Exception):
    """Raised when a caller attempts something it may not do."""

    def __init__(self, verdict: SafetyVerdict):
        super().__init__(verdict.explanation)
        self.verdict = verdict
        self.code = verdict.code


class ExecutionService:
    """
    The operation surface for execution.

    Holds the orchestrator, the registry and the repository, and is the
    only place a caller's permissions are checked. Everything below it
    assumes the check already happened, which is why nothing below it
    is public API.
    """

    def __init__(self, orchestrator: ExecutionOrchestrator,
                 repository: Optional[ExecutionRepository] = None):
        self.orchestrator = orchestrator
        self.repository = repository
        self.registry: BrokerRegistry = orchestrator.registry
        self.safety: ExecutionSafety = orchestrator.safety

    # ---------------- permission helper ----------------

    def _require(self, caller: Caller,
                 permission: ExecutionPermission) -> None:
        verdict = self.safety.check_permission(caller.permissions, permission)
        if not verdict.permitted:
            raise PermissionDenied(verdict)

    # ---------------- brokers ----------------

    def list_brokers(self, caller: Caller) -> List[Dict[str, Any]]:
        """
        Every registered venue, implemented or not.

        Unimplemented venues are listed with `implemented: False` and a
        reason rather than hidden. A venue the operator cannot see is
        worse than one they can see is off.
        """
        self._require(caller, ExecutionPermission.VIEW_EXECUTION)
        out: List[Dict[str, Any]] = []
        for entry in self.registry.all():
            capability = entry.gateway.get_capabilities()
            out.append({
                "broker_id": entry.broker.broker_id,
                "name": entry.broker.name,
                "environment": entry.broker.environment.value,
                "adapter": entry.broker.adapter or entry.gateway.version,
                "enabled": entry.broker.enabled,
                "implemented": entry.broker.implemented,
                "can_trade": entry.broker.can_trade,
                "connection": entry.gateway.connection_state().value,
                "accounts": sorted(entry.accounts),
                "notes": capability.notes,
            })
        return out

    def get_broker(self, caller: Caller, broker_id: str) -> Optional[Dict[str, Any]]:
        self._require(caller, ExecutionPermission.VIEW_EXECUTION)
        for record in self.list_brokers(caller):
            if record["broker_id"] == broker_id:
                return record
        return None

    def broker_health(self, caller: Caller, broker_id: str,
                      now: datetime) -> Optional[BrokerHealth]:
        self._require(caller, ExecutionPermission.VIEW_EXECUTION)
        entry = self.registry.get(broker_id)
        return entry.gateway.health_check(now) if entry else None

    def capabilities(self, caller: Caller,
                     broker_id: str) -> Optional[BrokerCapability]:
        self._require(caller, ExecutionPermission.VIEW_EXECUTION)
        entry = self.registry.get(broker_id)
        return entry.gateway.get_capabilities() if entry else None

    def trading_hours(self, caller: Caller, broker_id: str,
                      instrument_id: str) -> str:
        self._require(caller, ExecutionPermission.VIEW_EXECUTION)
        entry = self.registry.get(broker_id)
        return entry.gateway.get_trading_hours(instrument_id) if entry else ""

    # ---------------- accounts ----------------

    def list_accounts(self, caller: Caller) -> List[Dict[str, Any]]:
        self._require(caller, ExecutionPermission.VIEW_ACCOUNT)
        out: List[Dict[str, Any]] = []
        for entry in self.registry.all():
            for account in entry.accounts.values():
                out.append({
                    "account_id": account.account_id,
                    "broker_id": account.broker_id,
                    "name": account.name,
                    "environment": account.environment.value,
                    "base_currency": account.base_currency,
                    "enabled": account.enabled,
                    "can_trade": account.can_trade,
                    "position_accounting": account.position_accounting.value,
                    "linked_reference": account.linked_reference,
                })
        return out

    def get_account(self, caller: Caller, broker_id: str, account_id: str,
                    now: datetime) -> Optional[AccountSnapshot]:
        self._require(caller, ExecutionPermission.VIEW_ACCOUNT)
        entry = self.registry.get(broker_id)
        return entry.gateway.get_account(account_id, now) if entry else None

    def positions(self, caller: Caller, broker_id: str, account_id: str,
                  now: datetime) -> List[PositionSnapshot]:
        self._require(caller, ExecutionPermission.VIEW_ACCOUNT)
        entry = self.registry.get(broker_id)
        return entry.gateway.get_positions(account_id, now) if entry else []

    def orders(self, caller: Caller, broker_id: Optional[str] = None,
               account_id: Optional[str] = None,
               open_only: bool = False) -> List[ExecutionOrder]:
        self._require(caller, ExecutionPermission.VIEW_ORDERS)
        found = [
            o for o in self.orchestrator.orders.values()
            if (broker_id is None or o.broker_id == broker_id)
            and (account_id is None or o.account_id == account_id)
            and (not open_only or o.state.is_working)]
        return sorted(found, key=lambda o: o.intent_at or datetime.min.replace(
            tzinfo=None) if o.intent_at is None else o.intent_at, reverse=True)

    def fills(self, caller: Caller, broker_id: Optional[str] = None,
              order_id: Optional[str] = None) -> List[ExecutionFill]:
        self._require(caller, ExecutionPermission.VIEW_ORDERS)
        return [f for f in self.orchestrator.fills
                if (broker_id is None or f.broker_id == broker_id)
                and (order_id is None or f.order_id == order_id)]

    def trace(self, caller: Caller, order_id: str) -> Dict[str, Any]:
        """The full causal chain for one order (spec §35, §49, §52)."""
        self._require(caller, ExecutionPermission.VIEW_ORDERS)
        return self.orchestrator.trace(order_id)

    # ---------------- validation and dry run ----------------

    def validate(self, caller: Caller,
                 request: IntentRequest) -> ValidationResult:
        """Run every pre-trade check without building an order."""
        self._require(caller, ExecutionPermission.DRY_RUN_EXECUTION)
        return self.orchestrator.dry_run(request).validation

    def dry_run(self, caller: Caller, request: IntentRequest) -> DryRunResult:
        self._require(caller, ExecutionPermission.DRY_RUN_EXECUTION)
        return self.orchestrator.dry_run(request)

    # ---------------- the guarded write ----------------

    def submit(self, caller: Caller, request: IntentRequest) -> ExecutionResult:
        """
        Place an order, in a non-live environment only (spec §46).

        The environment is resolved from the registered broker, not
        from the request, so a caller cannot name an environment it is
        not entitled to. Then the permission for THAT environment is
        required — and the live one can never be satisfied.
        """
        entry = self.registry.get(request.broker_id)
        if entry is None:
            result = ExecutionResult(
                correlation_id=request.correlation_id or "",
                intent_id=request.intent_id, at=request.now)
            result.validation.checks_performed += 1
            result.validation.fail(ExecutionRejectCode.UNKNOWN_BROKER,
                                   f"Broker {request.broker_id}.",
                                   at=request.now)
            return result

        environment = entry.broker.environment
        # Belt and braces: the domain types already refuse to construct
        # a live broker, so reaching this line at all would mean an
        # invariant had been broken elsewhere.
        self.safety.assert_not_real_money(environment)
        self._require(caller, self.safety.permission_for(environment))

        result = self.orchestrator.execute(request)
        self._persist(result)
        return result

    def cancel(self, caller: Caller, broker_id: str, order_id: str,
               now: datetime) -> Dict[str, Any]:
        self._require(caller, ExecutionPermission.PAPER_EXECUTION)
        entry = self.registry.get(broker_id)
        order = self.orchestrator.orders.get(order_id)
        if entry is None or order is None:
            return {"cancelled": False, "reason": "unknown broker or order"}
        ack = entry.gateway.cancel_order(
            order.broker_order_id or order.order_id, now)
        if ack.accepted:
            self.orchestrator.machine.apply(
                order, ExecutionOrderState.CANCELLED, at=now,
                reason="cancelled via the execution service", strict=False)
        return {"cancelled": ack.accepted, "state": order.state.value,
                "reason": ack.detail}

    def modify(self, caller: Caller, broker_id: str, order_id: str,
               now: datetime, **changes) -> Dict[str, Any]:
        """
        Amend a working order, where the venue supports it.

        The paper adapter does not, and says so through the default
        `NOT_IMPLEMENTED` rather than pretending.
        """
        self._require(caller, ExecutionPermission.PAPER_EXECUTION)
        entry = self.registry.get(broker_id)
        order = self.orchestrator.orders.get(order_id)
        if entry is None or order is None:
            return {"modified": False, "reason": "unknown broker or order"}
        if not entry.gateway.get_capabilities().supports_order_modification:
            return {"modified": False,
                    "reason": explain(ExecutionRejectCode.NOT_IMPLEMENTED,
                                      f"{broker_id} cannot amend orders.")}
        ack = entry.gateway.modify_order(
            order.broker_order_id or order.order_id, now, **changes)
        return {"modified": ack.accepted, "reason": ack.detail}

    # ---------------- events and reconciliation ----------------

    def events(self, caller: Caller, limit: int = 100) -> List[ExecutionEvent]:
        self._require(caller, ExecutionPermission.VIEW_EXECUTION)
        return self.orchestrator.event_log[-limit:]

    def errors(self, caller: Caller, limit: int = 50) -> List[ExecutionError]:
        self._require(caller, ExecutionPermission.VIEW_EXECUTION)
        return self.orchestrator.errors[-limit:]

    def reconcile(self, caller: Caller, broker_id: str, account_id: str,
                  now: datetime, internal_positions=None,
                  internal_cash=None) -> Optional[ReconciliationRecord]:
        self._require(caller, ExecutionPermission.VIEW_EXECUTION)
        record = self.orchestrator.reconcile(
            broker_id, account_id, now,
            internal_positions=internal_positions,
            internal_cash=internal_cash)
        if record is not None and self.repository is not None:
            self.repository.save_reconciliation(record)
        return record

    def resolve_unknown(self, caller: Caller, broker_id: str,
                        now: datetime) -> List[Dict[str, Any]]:
        """
        Ask the broker about unknown orders (spec §24).

        Requires an execution permission rather than a read permission:
        it changes recorded order state, even though it sends nothing.
        """
        self._require(caller, ExecutionPermission.PAPER_EXECUTION)
        return [{
            "order_id": r.order_id, "resolved": r.resolved,
            "state": r.state.value, "detail": r.detail,
            "broker_order_id": r.broker_order_id,
        } for r in self.orchestrator.resolve_unknown_orders(broker_id, now)]

    # ---------------- safety surface ----------------

    def safety_state(self, caller: Caller) -> Dict[str, Any]:
        self._require(caller, ExecutionPermission.VIEW_EXECUTION)
        state = self.safety.state()
        state["environments"] = {
            e.value: {"implemented": e.is_implemented,
                      "real_money": e.is_real_money}
            for e in ExecutionEnvironment}
        return state

    def activate_kill_switch(self, caller: Caller, reason: str,
                             now: datetime) -> Dict[str, Any]:
        """
        Stop all new orders.

        Requires only a paper-execution permission on purpose. Stopping
        is always safer than continuing, so the bar to stop should be
        lower than the bar to trade — never the reverse.
        """
        self._require(caller, ExecutionPermission.PAPER_EXECUTION)
        event = self.safety.activate_kill_switch(reason, now, caller.name)
        return {"active": True, "reason": reason, "audit_id": event.audit_id}

    def release_kill_switch(self, caller: Caller, reason: str,
                            now: datetime) -> Dict[str, Any]:
        self._require(caller, ExecutionPermission.PAPER_EXECUTION)
        event = self.safety.release_kill_switch(reason, now, caller.name)
        return {"active": False, "reason": reason, "audit_id": event.audit_id}

    # ---------------- persistence ----------------

    def _persist(self, result: ExecutionResult) -> None:
        if self.repository is None or result.order is None:
            return
        order = result.order
        self.repository.save_execution(
            order,
            transitions=self.orchestrator.machine.transitions_for(order.order_id),
            fills=[f for f in self.orchestrator.fills
                   if f.order_id == order.order_id],
            events=[e for e in self.orchestrator.event_log
                    if e.order_id == order.order_id],
            errors=[result.error] if result.error else (),
            audit=self.orchestrator.audit[-8:])

    def persist_all(self) -> int:
        """Flush the whole in-memory book. Used by the CLI after a run."""
        if self.repository is None:
            return 0
        count = 0
        for order in self.orchestrator.orders.values():
            self.repository.save_execution(
                order,
                transitions=self.orchestrator.machine.transitions_for(order.order_id),
                fills=[f for f in self.orchestrator.fills
                       if f.order_id == order.order_id],
                events=[e for e in self.orchestrator.event_log
                        if e.order_id == order.order_id])
            count += 1
        for error in self.orchestrator.errors:
            self.repository._save_errors([error])
        self.repository._save_audit(self.orchestrator.audit)
        self.repository.conn.commit()
        return count
