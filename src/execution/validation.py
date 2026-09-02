"""
src/execution/validation.py
--------------------------------
Pre-trade validation (Phase 14, spec §13, §34, §40).

WHY EVERY CHECK RUNS
------------------------
The gate collects all findings rather than returning at the first
failure. One rejection tells an operator what to fix; the whole list
tells them whether fixing it will help. An order that is both
unmappable and outside market hours should say so once, not across two
attempts an hour apart.

The exception is ordering: checks run cheapest-and-broadest first, so
the FIRST finding — the one surfaced in a one-line summary — is the
most actionable one. Safety before routing, routing before capability,
capability before market, market before arithmetic, arithmetic before
the account.

WHAT THIS LAYER DOES NOT DECIDE
-----------------------------------
It does not run the risk engine. Phase 11 owns that, and re-deriving
any part of it here would create a second opinion about exposure that
could disagree with the first. The orchestrator calls the real risk
engine and passes the verdict in; this module only records it.

Nor does it price anything. Whether a fill is achievable at a price is
the executor's judgement, made against a bar this module cannot see.

MARKET STATUS IS ASKED, NOT COMPUTED
----------------------------------------
Session state comes from the gateway, which derives it from the Phase
12 calendar built out of real cached bars. There is deliberately no
`weekday < 5` fallback anywhere in this file: a holiday is a weekday,
and an instrument with no data is not the same as a closed market.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from src.domain.broker_models import (
    AccountSnapshot, BrokerAccount, BrokerCapability, BrokerConnectionState,
    CanonicalOrderSide, CanonicalOrderType, CanonicalTimeInForce,
    ExecutionEnvironment, ExecutionRejectCode, MarketStatus,
    PositionSnapshot, ValidationResult, finite_or_none,
)
from src.execution.instruments import InstrumentRegistry, MappingResolution
from src.execution.safety import ExecutionSafety


@dataclass
class ValidationRequest:
    """
    Everything the gate needs to judge one prospective order.

    Assembled by the orchestrator. Passing a struct rather than fifteen
    arguments means a new check can read a field that already exists
    instead of changing every call site.
    """
    broker_id: str
    account_id: str
    instrument_id: str
    side: CanonicalOrderSide
    quantity: float
    order_type: CanonicalOrderType = CanonicalOrderType.MARKET
    time_in_force: CanonicalTimeInForce = CanonicalTimeInForce.DAY
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    environment: ExecutionEnvironment = ExecutionEnvironment.PAPER
    reference_price: Optional[float] = None
    now: Optional[datetime] = None
    correlation_id: str = ""
    strategy_id: Optional[str] = None
    portfolio_id: Optional[str] = None
    #: The Phase 11 verdict, already obtained. None means "not consulted",
    #: which is treated as a failure rather than as approval.
    risk_approved: Optional[bool] = None
    risk_detail: str = ""
    #: True when the data behind the decision is too old to trade on.
    data_is_stale: bool = False
    freshness_detail: str = ""
    #: Set when this intent already produced an order.
    duplicate_of: Optional[str] = None
    #: Maximum absolute position size, when the caller enforces one.
    position_limit: Optional[float] = None


class PreTradeValidator:
    """
    Runs every pre-trade check and reports all of them.

    Holds no state between calls. Two validations of the same order
    must produce the same findings, or the audit trail becomes a
    record of when we happened to look rather than of what was true.
    """

    def __init__(self, registry: InstrumentRegistry, safety: ExecutionSafety):
        self.registry = registry
        self.safety = safety

    def validate(self, request: ValidationRequest,
                 capability: Optional[BrokerCapability] = None,
                 account: Optional[BrokerAccount] = None,
                 snapshot: Optional[AccountSnapshot] = None,
                 market_status: MarketStatus = MarketStatus.UNKNOWN,
                 connection: BrokerConnectionState = BrokerConnectionState.CONNECTED,
                 current_position: Optional[PositionSnapshot] = None
                 ) -> ValidationResult:
        result = ValidationResult(market_status=market_status)
        at = request.now
        cid = request.correlation_id

        # --- 1. safety, outermost first -------------------------------
        self.safety.apply_to(
            result, request.environment, at=at, correlation_id=cid,
            broker_id=request.broker_id, account_id=request.account_id,
            strategy_id=request.strategy_id, portfolio_id=request.portfolio_id)

        # --- 2. duplication -------------------------------------------
        result.checks_performed += 1
        if request.duplicate_of:
            result.fail(ExecutionRejectCode.DUPLICATE_INTENT,
                        f"Already produced order {request.duplicate_of}.",
                        at=at, correlation_id=cid,
                        existing_order_id=request.duplicate_of)

        # --- 3. account routing ---------------------------------------
        result.checks_performed += 1
        if account is None:
            result.fail(ExecutionRejectCode.UNKNOWN_ACCOUNT,
                        f"Account {request.account_id}.", at=at,
                        correlation_id=cid)
        elif not account.can_trade:
            result.fail(ExecutionRejectCode.ACCOUNT_DISABLED,
                        f"Account {request.account_id}.", at=at,
                        correlation_id=cid)

        # --- 4. connection --------------------------------------------
        result.checks_performed += 1
        if not connection.can_submit:
            result.fail(ExecutionRejectCode.BROKER_DISCONNECTED,
                        f"Connection is {connection.value}.", at=at,
                        correlation_id=cid, connection=connection.value)

        # --- 5. instrument mapping ------------------------------------
        result.checks_performed += 1
        resolution: MappingResolution = self.registry.resolve(
            request.broker_id, request.instrument_id)
        mapping = resolution.mapping
        if not resolution.ok:
            result.fail(resolution.code, resolution.detail, at=at,
                        correlation_id=cid, instrument_id=request.instrument_id)
        if mapping is not None:
            result.broker_symbol = mapping.broker_symbol

        # --- 6. broker capability -------------------------------------
        result.checks_performed += 1
        if capability is None:
            result.fail(ExecutionRejectCode.UNKNOWN_BROKER,
                        f"Broker {request.broker_id} declares no capabilities.",
                        at=at, correlation_id=cid)
        else:
            if not capability.supports_order_type(request.order_type):
                result.fail(ExecutionRejectCode.UNSUPPORTED_ORDER_TYPE,
                            f"{request.order_type.value} at {request.broker_id}.",
                            at=at, correlation_id=cid)
            if not capability.supports_time_in_force(request.time_in_force):
                result.fail(ExecutionRejectCode.UNSUPPORTED_TIME_IN_FORCE,
                            f"{request.time_in_force.value} at {request.broker_id}.",
                            at=at, correlation_id=cid)
            if mapping is not None and not capability.supports_asset_class(
                    mapping.asset_class):
                result.fail(ExecutionRejectCode.UNSUPPORTED_ASSET_CLASS,
                            f"{mapping.asset_class} at {request.broker_id}.",
                            at=at, correlation_id=cid)

        # --- 7. market session ----------------------------------------
        result.checks_performed += 1
        if market_status is MarketStatus.HALTED:
            result.fail(ExecutionRejectCode.INSTRUMENT_HALTED,
                        request.instrument_id, at=at, correlation_id=cid)
        elif market_status in (MarketStatus.CLOSED, MarketStatus.HOLIDAY):
            result.fail(ExecutionRejectCode.MARKET_CLOSED,
                        f"{request.instrument_id} ({market_status.value}).",
                        at=at, correlation_id=cid)
        elif market_status in (MarketStatus.PRE_MARKET, MarketStatus.AFTER_HOURS):
            # Only permitted when the venue says it can trade there.
            if capability is None or not capability.supports_extended_hours:
                result.fail(ExecutionRejectCode.SESSION_NOT_PERMITTED,
                            f"{market_status.value} trading is not supported here.",
                            at=at, correlation_id=cid)
        elif market_status is MarketStatus.UNKNOWN:
            result.fail(ExecutionRejectCode.MARKET_CLOSED,
                        "The session state for this instrument could not be "
                        "determined, so it is not assumed open.",
                        at=at, correlation_id=cid)

        # --- 8. data freshness ----------------------------------------
        result.checks_performed += 1
        if request.data_is_stale:
            result.fail(ExecutionRejectCode.STALE_DATA,
                        request.freshness_detail, at=at, correlation_id=cid)

        # --- 9. order arithmetic --------------------------------------
        result.checks_performed += 1
        quantity = finite_or_none(request.quantity)
        if quantity is None or quantity <= 0:
            result.fail(ExecutionRejectCode.INVALID_QUANTITY,
                        f"Got {request.quantity!r}.", at=at, correlation_id=cid)
        elif mapping is not None:
            normalized, code = mapping.normalize_quantity(quantity)
            result.normalized_quantity = normalized
            if code is not None:
                result.fail(code,
                            f"{quantity:g} against increment "
                            f"{mapping.quantity_increment} / minimum "
                            f"{mapping.minimum_quantity}.",
                            at=at, correlation_id=cid)
            elif (capability is not None
                  and not capability.supports_fractional_quantity
                  and abs(normalized - round(normalized)) > 1e-9):
                result.fail(ExecutionRejectCode.FRACTIONAL_NOT_SUPPORTED,
                            f"{normalized:g} units.", at=at, correlation_id=cid)
        else:
            result.normalized_quantity = quantity

        result.checks_performed += 1
        if request.order_type.requires_limit_price and request.limit_price is None:
            result.fail(ExecutionRejectCode.MISSING_LIMIT_PRICE,
                        request.order_type.value, at=at, correlation_id=cid)
        if request.order_type.requires_stop_price and request.stop_price is None:
            result.fail(ExecutionRejectCode.MISSING_STOP_PRICE,
                        request.order_type.value, at=at, correlation_id=cid)

        for label, raw in (("limit", request.limit_price),
                           ("stop", request.stop_price)):
            if raw is None:
                continue
            value = finite_or_none(raw)
            if value is None or value <= 0:
                result.fail(ExecutionRejectCode.INVALID_PRICE,
                            f"{label} price {raw!r}.", at=at, correlation_id=cid)
                continue
            normalized = mapping.normalize_price(value) if mapping else value
            if label == "limit":
                result.normalized_limit_price = normalized
            else:
                result.normalized_stop_price = normalized

        # --- 10. shorting ---------------------------------------------
        result.checks_performed += 1
        if request.side is CanonicalOrderSide.SELL and capability is not None:
            held = current_position.quantity if current_position else 0.0
            would_short = held - (result.normalized_quantity or 0.0) < -1e-9
            if would_short and not capability.supports_shorting:
                result.fail(ExecutionRejectCode.SHORTING_NOT_SUPPORTED,
                            f"Holding {held:g}, selling "
                            f"{result.normalized_quantity or 0:g}.",
                            at=at, correlation_id=cid)

        # --- 11. position limit ---------------------------------------
        result.checks_performed += 1
        if request.position_limit is not None:
            held = current_position.quantity if current_position else 0.0
            projected = held + (result.normalized_quantity or 0.0) * request.side.sign
            if abs(projected) > request.position_limit + 1e-9:
                result.fail(ExecutionRejectCode.POSITION_LIMIT,
                            f"Projected {projected:g} exceeds "
                            f"{request.position_limit:g}.",
                            at=at, correlation_id=cid)

        # --- 12. buying power -----------------------------------------
        result.checks_performed += 1
        if (request.side is CanonicalOrderSide.BUY and snapshot is not None
                and result.normalized_quantity):
            price = finite_or_none(
                result.normalized_limit_price or request.reference_price)
            if price is not None:
                multiplier = mapping.contract_multiplier if mapping else 1.0
                needed = result.normalized_quantity * price * multiplier
                if needed > snapshot.spendable + 1e-6:
                    result.fail(
                        ExecutionRejectCode.INSUFFICIENT_BUYING_POWER,
                        f"Needs {needed:,.2f}, has {snapshot.spendable:,.2f}.",
                        at=at, correlation_id=cid,
                        needed=round(needed, 6),
                        available=round(snapshot.spendable, 6))

        # --- 13. risk -------------------------------------------------
        result.checks_performed += 1
        if request.risk_approved is None:
            # Not consulted is not approval. A risk engine that could
            # not be reached must stop the order, never wave it past.
            result.fail(ExecutionRejectCode.RISK_UNAVAILABLE,
                        request.risk_detail or
                        "The risk engine was not consulted for this order.",
                        at=at, correlation_id=cid)
        elif not request.risk_approved:
            result.fail(ExecutionRejectCode.RISK_REJECTED,
                        request.risk_detail, at=at, correlation_id=cid)

        return result
