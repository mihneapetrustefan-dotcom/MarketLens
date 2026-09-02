"""
src/execution/orchestrator.py
----------------------------------
The execution orchestrator (Phase 14, spec §12, §33, §35, §37, §38).

WHAT IT IS
--------------
The one place an `OrderIntent` becomes an order, and the one place that
knows how to route. Everything above it is broker-neutral by
construction — strategy, signals, portfolio, risk all end at the intent
— and everything below it is a specific venue behind `BrokerGateway`.

The orchestrator itself must stay neutral too, and it does: there is no
`if broker_id == "mt5"` anywhere in this file, and there cannot be, or
Phase 15 would begin by editing it.

THE ORDER OF OPERATIONS IS THE SAFETY MODEL
-----------------------------------------------
    safety            can we execute at all
    routing           which broker, which account
    idempotency       have we already done this
    mapping           does this instrument exist there
    capability        can that venue do this
    market session    is it open
    risk              did Phase 11 approve
    validation        everything else, all findings collected
    ---- the line ----
    submission        the only step that can reach a venue

Every check is above the line. Once a submission is issued the outcome
may be unknown, and the entire point of the ordering is to make sure
nothing reaches that state for a reason we could have caught first.

A DRY RUN STOPS AT THE LINE
-------------------------------
`dry_run` runs the identical pipeline and returns before submission.
Not a parallel implementation — the same validator, the same mapping,
the same capability check — because a dry run that exercised different
code would be testing something other than what runs.

TIMEOUTS ARE NOT FAILURES
-----------------------------
A submission that times out becomes UNKNOWN, never FAILED and never
retried. The broker may have accepted it. Only reconciliation, by
asking the venue, may resolve that — see `resolve_unknown_orders`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.domain.broker_models import (
    AccountSnapshot, AuditEvent, Broker, BrokerAccount, CanonicalOrderSide,
    CanonicalOrderType, CanonicalTimeInForce, DryRunResult, ExecutionEnvironment,
    ExecutionError, ExecutionEvent, ExecutionEventType, ExecutionFill,
    ExecutionOrder, ExecutionOrderState, ExecutionRejectCode, ExecutionResult,
    MarketStatus, PositionSnapshot, ReconciliationRecord, ValidationResult,
    explain,
)
from src.execution.events import EventProcessor, ProcessingReport
from src.execution.gateway import BrokerGateway, SubmissionAck
from src.execution.instruments import InstrumentRegistry
from src.execution.policy import (
    ExecutionPolicy, MarketPolicy, RateLimiter, client_order_id,
    get_policy, idempotency_key,
)
from src.execution.reconciliation import BrokerReconciler, UnknownResolution
from src.execution.safety import ExecutionSafety
from src.execution.states import OrderStateMachine
from src.execution.validation import PreTradeValidator, ValidationRequest


@dataclass
class IntentRequest:
    """
    A broker-neutral request to execute, assembled from an OrderIntent.

    Routing is explicit: `broker_id` and `account_id` are required.
    There is deliberately no default account, because a system that
    picks one when none is given will eventually pick the wrong one.
    """
    intent_id: str
    broker_id: str
    account_id: str
    instrument_id: str
    side: CanonicalOrderSide
    quantity: float
    now: datetime

    order_type: Optional[CanonicalOrderType] = None
    time_in_force: CanonicalTimeInForce = CanonicalTimeInForce.DAY
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    policy: str = "market"
    reference_price: Optional[float] = None
    decision_price: Optional[float] = None
    intent_version: int = 1
    expires_at: Optional[datetime] = None

    # provenance (spec §35)
    correlation_id: str = ""
    signal_id: Optional[str] = None
    prediction_id: Optional[str] = None
    model_version: Optional[str] = None
    strategy_id: Optional[str] = None
    portfolio_id: Optional[str] = None
    decision_id: Optional[str] = None

    # gates the caller has already evaluated
    risk_approved: Optional[bool] = None
    risk_detail: str = ""
    data_is_stale: bool = False
    freshness_detail: str = ""
    position_limit: Optional[float] = None


@dataclass
class RegisteredBroker:
    """A gateway plus the metadata routing needs."""
    broker: Broker
    gateway: BrokerGateway
    accounts: Dict[str, BrokerAccount] = field(default_factory=dict)
    rate_limiter: RateLimiter = field(default_factory=RateLimiter)


class BrokerRegistry:
    """
    Every broker this system knows about (spec §38).

    A registry rather than a singleton, deliberately. A single global
    gateway is the architecture that makes the second broker a rewrite,
    and this project intends to have at least three.
    """

    def __init__(self):
        self._brokers: Dict[str, RegisteredBroker] = {}

    def register(self, broker: Broker, gateway: BrokerGateway,
                 accounts: Sequence[BrokerAccount] = ()) -> RegisteredBroker:
        if broker.broker_id != gateway.broker_id:
            raise ValueError(
                f"broker id mismatch: record says {broker.broker_id!r}, "
                f"gateway says {gateway.broker_id!r}")
        capability = gateway.get_capabilities()
        entry = RegisteredBroker(
            broker=broker, gateway=gateway,
            accounts={a.account_id: a for a in accounts},
            rate_limiter=RateLimiter(capability.rate_limit_per_minute))
        self._brokers[broker.broker_id] = entry
        return entry

    def add_account(self, account: BrokerAccount) -> None:
        entry = self._brokers.get(account.broker_id)
        if entry is None:
            raise ValueError(f"no broker {account.broker_id!r} is registered")
        entry.accounts[account.account_id] = account

    def get(self, broker_id: str) -> Optional[RegisteredBroker]:
        return self._brokers.get(broker_id)

    def account(self, broker_id: str,
                account_id: str) -> Optional[BrokerAccount]:
        entry = self._brokers.get(broker_id)
        return entry.accounts.get(account_id) if entry else None

    def all(self) -> List[RegisteredBroker]:
        return list(self._brokers.values())

    def ids(self) -> List[str]:
        return sorted(self._brokers)

    def brokers_for_instrument(self, registry: InstrumentRegistry,
                               instrument_id: str) -> List[str]:
        """
        Which registered venues can trade this instrument.

        The extension point routing will grow from (spec §39). It
        returns candidates and deliberately does not choose: preferred
        venue, fallback and asset-class routing are future work, and a
        chooser here would have to invent a policy nobody has specified.
        """
        mapped = set(registry.brokers_for(instrument_id))
        return [b for b in self.ids() if b in mapped]


class ExecutionOrchestrator:
    """
    Turns intents into orders, and broker events into state.

    Broker-neutral throughout. Every venue-specific decision is either
    on the gateway or on the instrument mapping.
    """

    def __init__(self, registry: BrokerRegistry,
                 instruments: InstrumentRegistry,
                 safety: ExecutionSafety,
                 machine: Optional[OrderStateMachine] = None,
                 reconciler: Optional[BrokerReconciler] = None,
                 actor: str = "system"):
        self.registry = registry
        self.instruments = instruments
        self.safety = safety
        self.machine = machine or OrderStateMachine()
        self.validator = PreTradeValidator(instruments, safety)
        self.events = EventProcessor(self.machine)
        self.reconciler = reconciler or BrokerReconciler()
        self.actor = actor

        self.orders: Dict[str, ExecutionOrder] = {}
        self.fills: List[ExecutionFill] = []
        self.errors: List[ExecutionError] = []
        self.audit: List[AuditEvent] = []
        self.event_log: List[ExecutionEvent] = []
        #: idempotency key -> order id. The duplicate guard.
        self._by_key: Dict[str, str] = {}

    # ---------------- recovery ----------------

    def seed(self, orders: Sequence[ExecutionOrder] = (),
             fills: Sequence[ExecutionFill] = (),
             transitions: Sequence[Any] = (),
             event_keys: Sequence[str] = ()) -> None:
        """
        Restore state after a restart (spec §60).

        The idempotency index is rebuilt from the orders themselves
        rather than persisted separately, so it cannot drift out of
        step with the book it protects.
        """
        for order in orders:
            self.orders[order.order_id] = order
            if order.idempotency_key:
                self._by_key[order.idempotency_key] = order.order_id
        self.fills.extend(fills)
        if transitions:
            self.machine.seed(transitions)
        self.events.seed(
            event_keys=event_keys,
            fill_keys=[f.idempotency_key or f.execution_id or f.fill_id
                       for f in fills])

    def orders_in_flight(self) -> List[ExecutionOrder]:
        """
        Orders that were handed to a broker with no outcome recorded.

        After a crash these are the dangerous ones: the venue may hold
        an order nothing local knows the fate of. Recovery marks them
        UNKNOWN rather than assuming either way.
        """
        return [o for o in self.orders.values() if o.state.is_in_flight]

    def mark_in_flight_unknown(self, at: datetime,
                               reason: str = "process restarted while in flight"
                               ) -> List[ExecutionOrder]:
        moved = []
        for order in self.orders_in_flight():
            self.machine.apply(order, ExecutionOrderState.UNKNOWN, at=at,
                               reason=reason, strict=False)
            moved.append(order)
        return moved

    # ---------------- the pipeline, above the line ----------------

    def _prepare(self, request: IntentRequest) -> Tuple[
            Optional[RegisteredBroker], ValidationResult, Dict[str, Any]]:
        """
        Everything up to but not including submission.

        Shared by `execute` and `dry_run` so the two cannot diverge.
        Returns the broker entry, the validation result, and the
        context both callers need.
        """
        cid = request.correlation_id or f"cor-{uuid.uuid4().hex[:16]}"
        request.correlation_id = cid

        entry = self.registry.get(request.broker_id)
        context: Dict[str, Any] = {
            "correlation_id": cid,
            "environment": ExecutionEnvironment.PAPER,
            "market_status": MarketStatus.UNKNOWN,
            "policy_decision": None,
            "duplicate_of": None,
            "snapshot": None,
            "position": None,
        }

        if entry is None:
            result = ValidationResult()
            result.checks_performed += 1
            result.fail(ExecutionRejectCode.UNKNOWN_BROKER,
                        f"Broker {request.broker_id}.", at=request.now,
                        correlation_id=cid)
            return None, result, context

        environment = entry.broker.environment
        context["environment"] = environment
        # The hard stop, before anything else touches this request.
        self.safety.assert_not_real_money(environment)

        # --- execution policy decides the order shape ---------------
        policy = get_policy(request.policy)
        decision = policy.decide(request.side, request.reference_price)
        context["policy_decision"] = decision
        order_type = request.order_type or decision.order_type
        limit_price = (request.limit_price if request.limit_price is not None
                       else decision.limit_price)
        stop_price = (request.stop_price if request.stop_price is not None
                      else decision.stop_price)
        context["order_type"] = order_type
        context["limit_price"] = limit_price
        context["stop_price"] = stop_price

        # --- idempotency --------------------------------------------
        key = idempotency_key(
            account_id=request.account_id, instrument_id=request.instrument_id,
            side=request.side, quantity=request.quantity, order_type=order_type,
            time_in_force=request.time_in_force, limit_price=limit_price,
            stop_price=stop_price, intent_id=request.intent_id,
            intent_version=request.intent_version)
        context["idempotency_key"] = key
        context["duplicate_of"] = self._by_key.get(key)

        # --- market session, asked of the gateway -------------------
        try:
            market_status = entry.gateway.market_status(
                request.instrument_id, request.now)
        except Exception as error:                        # noqa: BLE001
            market_status = MarketStatus.UNKNOWN
            self._record_error(ExecutionRejectCode.ADAPTER_ERROR,
                               f"market status query failed: {error}",
                               request, cid)
        context["market_status"] = market_status

        # --- account state, for buying power ------------------------
        snapshot: Optional[AccountSnapshot] = None
        position: Optional[PositionSnapshot] = None
        try:
            snapshot = entry.gateway.get_account(request.account_id, request.now)
            for candidate in entry.gateway.get_positions(request.account_id,
                                                         request.now):
                if candidate.instrument_id == request.instrument_id:
                    position = candidate
                    break
        except Exception as error:                        # noqa: BLE001
            self._record_error(ExecutionRejectCode.ADAPTER_ERROR,
                               f"account query failed: {error}", request, cid)
        context["snapshot"] = snapshot
        context["position"] = position

        # --- rate limit ---------------------------------------------
        rate_code = entry.rate_limiter.check(request.now)

        # --- the full pre-trade gate --------------------------------
        validation_request = ValidationRequest(
            broker_id=request.broker_id, account_id=request.account_id,
            instrument_id=request.instrument_id, side=request.side,
            quantity=request.quantity, order_type=order_type,
            time_in_force=request.time_in_force, limit_price=limit_price,
            stop_price=stop_price, environment=environment,
            reference_price=request.reference_price, now=request.now,
            correlation_id=cid, strategy_id=request.strategy_id,
            portfolio_id=request.portfolio_id,
            risk_approved=request.risk_approved,
            risk_detail=request.risk_detail,
            data_is_stale=request.data_is_stale,
            freshness_detail=request.freshness_detail,
            duplicate_of=context["duplicate_of"],
            position_limit=request.position_limit)

        result = self.validator.validate(
            validation_request,
            capability=entry.gateway.get_capabilities(),
            account=self.registry.account(request.broker_id, request.account_id),
            snapshot=snapshot, market_status=market_status,
            connection=entry.gateway.connection_state(),
            current_position=position)

        if rate_code is not None:
            result.checks_performed += 1
            result.fail(rate_code,
                        f"{entry.rate_limiter.used(request.now)} requests in the "
                        f"last minute.", at=request.now, correlation_id=cid)

        return entry, result, context

    # ---------------- dry run ----------------

    def dry_run(self, request: IntentRequest) -> DryRunResult:
        """
        Validate everything and stop before submission (spec §33).

        Uses the same `_prepare` as the real path, so a passing dry run
        means the real attempt would reach submission — which is the
        only useful guarantee a dry run can offer.
        """
        entry, validation, context = self._prepare(request)
        capability_ok = not any(
            code in (ExecutionRejectCode.UNSUPPORTED_ORDER_TYPE,
                     ExecutionRejectCode.UNSUPPORTED_TIME_IN_FORCE,
                     ExecutionRejectCode.UNSUPPORTED_ASSET_CLASS,
                     ExecutionRejectCode.SHORTING_NOT_SUPPORTED,
                     ExecutionRejectCode.FRACTIONAL_NOT_SUPPORTED)
            for code in validation.codes)
        mapping_ok = not any(
            code in (ExecutionRejectCode.NO_INSTRUMENT_MAPPING,
                     ExecutionRejectCode.INSTRUMENT_NOT_TRADABLE)
            for code in validation.codes)
        risk_ok = not any(
            code in (ExecutionRejectCode.RISK_REJECTED,
                     ExecutionRejectCode.RISK_UNAVAILABLE)
            for code in validation.codes)

        broker_request: Dict[str, Any] = {}
        if entry is not None and validation.passed:
            # The request that WOULD have been sent, built by the same
            # code that would have sent it. A dry run that showed a
            # hand-written approximation would not be evidence.
            broker_request = {
                "broker": entry.broker.broker_id,
                "account": request.account_id,
                "symbol": validation.broker_symbol,
                "side": request.side.value,
                "quantity": validation.normalized_quantity,
                "order_type": context["order_type"].value,
                "time_in_force": request.time_in_force.value,
                "limit_price": validation.normalized_limit_price,
                "stop_price": validation.normalized_stop_price,
                "client_order_id": client_order_id(context.get("idempotency_key", "")),
            }

        result = DryRunResult(
            correlation_id=context["correlation_id"], intent_id=request.intent_id,
            broker_id=request.broker_id, account_id=request.account_id,
            instrument_id=request.instrument_id, side=request.side,
            quantity=validation.normalized_quantity,
            order_type=context.get("order_type", CanonicalOrderType.MARKET),
            time_in_force=request.time_in_force,
            limit_price=validation.normalized_limit_price,
            stop_price=validation.normalized_stop_price,
            broker_symbol=validation.broker_symbol,
            environment=context["environment"],
            validation=validation, risk_passed=risk_ok,
            capability_passed=capability_ok, mapping_passed=mapping_ok,
            market_status=context["market_status"],
            would_submit=validation.passed,
            broker_request=broker_request, at=request.now)

        self._audit("dry_run", request.now,
                    subject_id=request.intent_id,
                    correlation_id=result.correlation_id,
                    detail=f"would_submit={result.would_submit}")
        return result

    # ---------------- execute ----------------

    def execute(self, request: IntentRequest) -> ExecutionResult:
        """
        The full path: validate, submit, record.

        Submission is the last statement, and everything before it can
        stop the order without a venue ever being contacted.
        """
        entry, validation, context = self._prepare(request)
        cid = context["correlation_id"]

        result = ExecutionResult(
            correlation_id=cid, intent_id=request.intent_id,
            validation=validation, environment=context["environment"],
            at=request.now, duplicate_of=context.get("duplicate_of"))

        if context.get("duplicate_of"):
            # The order already exists. Return it rather than a
            # rejection: the caller asked for this to happen, and it
            # has happened.
            existing = self.orders.get(context["duplicate_of"])
            result.order = existing
            result.accepted = existing is not None
            self._audit("duplicate_intent", request.now,
                        subject_id=request.intent_id, correlation_id=cid,
                        detail=f"already produced {context['duplicate_of']}")
            return result

        if entry is None or not validation.passed:
            self._audit("order_rejected", request.now,
                        subject_id=request.intent_id, correlation_id=cid,
                        detail=validation.explanation)
            return result

        # --- build the canonical order ------------------------------
        order = ExecutionOrder(
            order_id=f"eo-{uuid.uuid4().hex[:20]}",
            intent_id=request.intent_id, broker_id=request.broker_id,
            account_id=request.account_id, instrument_id=request.instrument_id,
            side=request.side,
            quantity=validation.normalized_quantity or request.quantity,
            order_type=context["order_type"],
            time_in_force=request.time_in_force,
            limit_price=validation.normalized_limit_price,
            stop_price=validation.normalized_stop_price,
            idempotency_key=context["idempotency_key"],
            client_order_id=client_order_id(context["idempotency_key"]),
            broker_symbol=validation.broker_symbol,
            correlation_id=cid, signal_id=request.signal_id,
            prediction_id=request.prediction_id,
            model_version=request.model_version,
            strategy_id=request.strategy_id, portfolio_id=request.portfolio_id,
            decision_id=request.decision_id,
            execution_policy=request.policy,
            environment=context["environment"],
            intent_at=request.now, expires_at=request.expires_at,
            decision_price=request.decision_price or request.reference_price,
            reference_price=request.reference_price)

        self.orders[order.order_id] = order
        self._by_key[order.idempotency_key] = order.order_id
        result.order = order

        # Walk the lifecycle properly rather than jumping to SUBMITTING:
        # the transition history is the audit trail, and a gap in it is
        # a gap in the record of why the order was allowed.
        self.machine.apply(order, ExecutionOrderState.VALIDATING,
                           at=request.now, reason="pre-trade validation",
                           correlation_id=cid)
        order.validated_at = request.now
        self.machine.apply(order, ExecutionOrderState.APPROVED,
                           at=request.now, reason=validation.explanation,
                           correlation_id=cid)

        # --- the line ------------------------------------------------
        return self._submit(entry, order, request, result)

    def _submit(self, entry: RegisteredBroker, order: ExecutionOrder,
                request: IntentRequest,
                result: ExecutionResult) -> ExecutionResult:
        """The only method in this file that can reach a venue."""
        self.machine.apply(order, ExecutionOrderState.SUBMITTING,
                           at=request.now, reason="handing to the gateway",
                           correlation_id=order.correlation_id)
        order.submitted_at = request.now
        entry.rate_limiter.record(request.now)

        try:
            ack: SubmissionAck = entry.gateway.submit_order(order, request.now)
        except Exception as error:                        # noqa: BLE001
            # An adapter that raised may still have reached the venue.
            # UNKNOWN, never FAILED — the difference is whether we are
            # allowed to try again.
            self.machine.apply(order, ExecutionOrderState.UNKNOWN,
                               at=request.now,
                               reason=f"adapter raised: {error}",
                               correlation_id=order.correlation_id,
                               strict=False)
            result.error = self._record_error(
                ExecutionRejectCode.ADAPTER_ERROR,
                f"submission raised {type(error).__name__}: {error}",
                request, order.correlation_id, order_id=order.order_id)
            self._audit("submission_failed", request.now,
                        subject_id=order.order_id,
                        correlation_id=order.correlation_id, detail=str(error))
            return result

        if ack.timed_out:
            self.machine.apply(order, ExecutionOrderState.UNKNOWN,
                               at=request.now,
                               reason="submission timed out; the broker may "
                                      "hold this order",
                               correlation_id=order.correlation_id,
                               strict=False)
            order.broker_order_id = ack.broker_order_id
            result.error = self._record_error(
                ExecutionRejectCode.ADAPTER_ERROR,
                "submission timed out; resolution requires querying the broker",
                request, order.correlation_id, order_id=order.order_id,
                retryable=False)
            self._audit("submission_timeout", request.now,
                        subject_id=order.order_id,
                        correlation_id=order.correlation_id,
                        detail="routed to reconciliation, NOT resubmitted")
            return result

        if not ack.accepted:
            self.machine.apply(order, ExecutionOrderState.REJECTED,
                               at=request.now,
                               reason=ack.detail or "broker rejected",
                               correlation_id=order.correlation_id,
                               strict=False)
            order.reject_code = ack.reject_code
            order.reject_detail = ack.detail
            result.validation.fail(
                ack.reject_code or ExecutionRejectCode.ADAPTER_ERROR,
                ack.detail, at=request.now,
                correlation_id=order.correlation_id)
            self._audit("order_rejected_by_broker", request.now,
                        subject_id=order.order_id,
                        correlation_id=order.correlation_id, detail=ack.detail)
            return result

        order.broker_order_id = ack.broker_order_id
        self.machine.apply(order, ExecutionOrderState.SUBMITTED,
                           at=request.now, reason="broker accepted",
                           correlation_id=order.correlation_id, strict=False)
        if ack.state is ExecutionOrderState.ACKNOWLEDGED:
            self.machine.apply(order, ExecutionOrderState.ACKNOWLEDGED,
                               at=request.now, reason="broker acknowledged",
                               correlation_id=order.correlation_id, strict=False)
            order.acknowledged_at = request.now

        result.accepted = True
        self._audit("order_submitted", request.now, subject_id=order.order_id,
                    correlation_id=order.correlation_id,
                    detail=f"broker_order_id={ack.broker_order_id}")
        return result

    # ---------------- events ----------------

    def drain_events(self, broker_id: str, now: datetime,
                     fills_by_event: Optional[Dict[str, ExecutionFill]] = None
                     ) -> ProcessingReport:
        """Poll one gateway and fold what it reported into order state."""
        entry = self.registry.get(broker_id)
        if entry is None:
            return ProcessingReport()
        events = entry.gateway.poll_events(now)
        self.event_log.extend(events)
        report = self.events.process(events, self.orders, fills_by_event)
        self.fills.extend(report.fills)
        return report

    def record_fills(self, fills: Sequence[ExecutionFill]) -> int:
        """
        Attach fills produced outside the event path.

        The paper gateway fills synchronously, so its fills arrive with
        the events rather than after them. Routed through the same
        duplicate guard so the two paths cannot disagree about what has
        already been counted.
        """
        added = 0
        for fill in fills:
            key = fill.idempotency_key or fill.execution_id or fill.fill_id
            if key in self.events.seen_fill_keys:
                continue
            order = self.orders.get(fill.order_id)
            if order is None:
                continue
            self.events.seed(fill_keys=[key])
            self.fills.append(fill)
            added += 1
        return added

    # ---------------- reconciliation ----------------

    def reconcile(self, broker_id: str, account_id: str, now: datetime,
                  internal_positions: Optional[Dict[str, float]] = None,
                  internal_cash: Optional[float] = None
                  ) -> Optional[ReconciliationRecord]:
        entry = self.registry.get(broker_id)
        if entry is None:
            return None
        ours = [o for o in self.orders.values()
                if o.broker_id == broker_id and o.account_id == account_id]
        record = self.reconciler.reconcile(
            broker_id=broker_id, account_id=account_id, at=now,
            internal_orders=ours,
            broker_orders=entry.gateway.reconcile_orders(account_id),
            internal_fills=[f for f in self.fills if f.broker_id == broker_id],
            internal_positions=internal_positions,
            broker_positions=entry.gateway.reconcile_positions(account_id, now),
            internal_cash=internal_cash,
            broker_account=entry.gateway.reconcile_account(account_id, now))
        self._audit("reconciliation", now, subject_id=account_id,
                    detail=f"{len(record.mismatches)} mismatch(es)")
        return record

    def resolve_unknown_orders(self, broker_id: str,
                               now: datetime) -> List[UnknownResolution]:
        """Ask the broker about unknown orders (spec §24). Never resubmits."""
        entry = self.registry.get(broker_id)
        if entry is None:
            return []
        ours = [o for o in self.orders.values() if o.broker_id == broker_id]
        resolutions = self.reconciler.resolve_unknown_orders(
            entry.gateway, ours, self.machine, now)
        for resolution in resolutions:
            self._audit("unknown_resolved" if resolution.resolved
                        else "unknown_unresolved", now,
                        subject_id=resolution.order_id,
                        detail=resolution.detail)
        return resolutions

    # ---------------- bookkeeping ----------------

    def _record_error(self, code: ExecutionRejectCode, message: str,
                      request: IntentRequest, correlation_id: str,
                      order_id: Optional[str] = None,
                      retryable: bool = False) -> ExecutionError:
        error = ExecutionError(
            error_id=f"err-{uuid.uuid4().hex[:16]}", at=request.now, code=code,
            message=message, broker_id=request.broker_id,
            account_id=request.account_id, order_id=order_id,
            correlation_id=correlation_id, retryable=retryable,
            context={"instrument_id": request.instrument_id,
                     "intent_id": request.intent_id})
        self.errors.append(error)
        return error

    def _audit(self, action: str, at: datetime, subject_id: str = "",
               correlation_id: str = "", detail: str = "") -> AuditEvent:
        event = AuditEvent(
            audit_id=f"aud-{uuid.uuid4().hex[:16]}", at=at, action=action,
            actor=self.actor, subject_type="execution", subject_id=subject_id,
            correlation_id=correlation_id, detail=detail)
        self.audit.append(event)
        return event

    # ---------------- provenance ----------------

    def trace(self, order_id: str) -> Dict[str, Any]:
        """
        The full causal chain for one order (spec §35, §52).

        The question this exists to answer is "why did this order
        happen", and the answer has to survive a restart — so every
        link is a stored id rather than an in-memory reference.
        """
        order = self.orders.get(order_id)
        if order is None:
            return {}
        return {
            "correlation_id": order.correlation_id,
            "model_version": order.model_version,
            "prediction_id": order.prediction_id,
            "signal_id": order.signal_id,
            "strategy_id": order.strategy_id,
            "portfolio_id": order.portfolio_id,
            "decision_id": order.decision_id,
            "intent_id": order.intent_id,
            "order_id": order.order_id,
            "client_order_id": order.client_order_id,
            "broker_order_id": order.broker_order_id,
            "broker_id": order.broker_id,
            "account_id": order.account_id,
            "environment": order.environment.value,
            "execution_policy": order.execution_policy,
            "state": order.state.value,
            "states": [
                {"seq": t.sequence, "from": t.from_state.value if t.from_state else None,
                 "to": t.to_state.value,
                 "at": t.at.isoformat() if t.at else None, "reason": t.reason}
                for t in self.machine.transitions_for(order_id)],
            "fills": [
                {"fill_id": f.fill_id, "quantity": f.quantity, "price": f.price,
                 "at": f.filled_at.isoformat() if f.filled_at else None,
                 "commission": f.commission, "fees": f.fees}
                for f in self.fills if f.order_id == order_id],
            "average_fill_price": order.average_fill_price,
            "slippage_bps": order.slippage_bps,
            "decision_price": order.decision_price,
        }
