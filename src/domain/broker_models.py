"""
src/domain/broker_models.py
--------------------------------
Broker-neutral execution domain (Phase 14).

WHAT THIS PHASE IS
----------------------
An architectural boundary, not a broker. Phase 14 builds the layer that
future MT5 (Phase 15) and IBKR (Phase 16) adapters plug into, so that
adding a venue never requires touching strategy, signal, portfolio or
risk code.

The rule the whole phase exists to enforce: nothing above the adapter
may know which broker it is talking to. No SDK type, no broker symbol,
no broker status string and no broker order id crosses that line. What
crosses is the types in this module.

NO REAL-MONEY EXECUTION EXISTS HERE
---------------------------------------
There is no MetaTrader 5 integration, no Interactive Brokers
integration, no credential, no network call and no live order path.
`ExecutionEnvironment.LIVE` is a value this module can NAME, and
naming it is the point — the safety layer has to be able to refer to
the thing it refuses. `ExecutionSafety` denies it unconditionally in
this phase, and the denial is a property of the code rather than a
setting anyone can flip.

WHY A SECOND ORDER STATE ENUM
---------------------------------
Phase 13's `PaperOrderState` is not extended, because a paper executor
genuinely cannot reach several of the states a real broker produces:
there is no submission that can time out, no acknowledgement that can
arrive late, and no state that can be unknown. `ExecutionOrderState`
is the canonical superset with those states added, and
`from_paper_state` maps between the two explicitly rather than by
coincidence of spelling.

UNKNOWN IS A FIRST-CLASS STATE
----------------------------------
The single most dangerous moment in live execution is a submission
that times out: the broker may or may not have accepted the order.
Guessing either way is how duplicate positions happen. `UNKNOWN`
exists so the system can record "we do not know" and route to
reconciliation instead of resubmitting.

WHAT IS REUSED RATHER THAN REDEFINED
----------------------------------------
`OrderIntent`, `RiskDecision` and `Position` come from Phase 11
unchanged. The cost and slippage models, the ledger and the market
calendar come from Phase 12 unchanged. `PaperOrder`, `PaperFill` and
the paper executor come from Phase 13 unchanged. This module adds the
broker-facing concepts those phases had no need for — capabilities,
instrument mappings, connections, canonical accounts — and nothing
else.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


# ============================================================
# Guards (same contract as Phases 9-13)
# ============================================================

def _require_utc(value: Optional[datetime], name: str) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (got a naive datetime)")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be in UTC (got offset {value.utcoffset()})")
    return value


def finite_or_none(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if isfinite(numeric) else None


# ============================================================
# Execution environment and safety
# ============================================================

class ExecutionEnvironment(str, Enum):
    """
    Where an order would go.

    Separate from the safety flags on purpose. An environment says what
    KIND of execution is being described; the safety layer says whether
    it is permitted. Collapsing the two produces the failure this whole
    phase exists to prevent: a configuration change that silently turns
    a description into a live order.
    """
    SIMULATION = "simulation"   # Phase 12, historical bars
    PAPER = "paper"             # Phase 13, simulated fills on current bars
    DEMO = "demo"               # a broker's practice endpoint; no adapter exists
    LIVE = "live"               # real money; permanently refused in Phase 14

    @property
    def is_real_money(self) -> bool:
        return self is ExecutionEnvironment.LIVE

    @property
    def is_implemented(self) -> bool:
        """
        Whether an adapter for this environment exists in the repository.

        DEMO and LIVE are named but unimplemented. Saying so here keeps
        the UI and the CLI from presenting an environment as available
        merely because the enum can spell it.
        """
        return self in (ExecutionEnvironment.SIMULATION, ExecutionEnvironment.PAPER)


class BrokerConnectionState(str, Enum):
    """Connection lifecycle (spec §26)."""
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    DISCONNECTED = "disconnected"
    AUTH_FAILED = "auth_failed"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"
    RECONNECTING = "reconnecting"

    @property
    def can_submit(self) -> bool:
        """
        Only a fully healthy connection may accept a new order.

        DEGRADED deliberately does NOT qualify. A degraded link can
        still carry a submission whose acknowledgement never arrives,
        which is the exact path to an UNKNOWN order — so new exposure
        waits, while queries and reconciliation continue.
        """
        return self is BrokerConnectionState.CONNECTED

    @property
    def is_terminal_failure(self) -> bool:
        return self in (BrokerConnectionState.AUTH_FAILED,
                        BrokerConnectionState.ERROR)


class ExecutionOrderState(str, Enum):
    """
    The canonical order lifecycle (spec §9).

    Every broker's own status vocabulary is translated into this one by
    its adapter. A raw broker status string must never reach the
    orchestrator, the database or the UI, because the moment it does,
    every consumer downstream has to learn that broker's dialect.
    """
    CREATED = "created"
    VALIDATING = "validating"
    APPROVED = "approved"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    WORKING = "working"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"
    UNKNOWN = "unknown"
    RECONCILIATION_REQUIRED = "reconciliation_required"

    @property
    def is_terminal(self) -> bool:
        """
        States from which no further broker event is expected.

        UNKNOWN and RECONCILIATION_REQUIRED are deliberately NOT
        terminal: they are open questions, and treating an open
        question as settled is how a real position goes unrecorded.
        """
        return self in (ExecutionOrderState.FILLED, ExecutionOrderState.CANCELLED,
                        ExecutionOrderState.REJECTED, ExecutionOrderState.EXPIRED,
                        ExecutionOrderState.FAILED)

    @property
    def is_working(self) -> bool:
        """Live at the broker and still able to fill."""
        return self in (ExecutionOrderState.SUBMITTED,
                        ExecutionOrderState.ACKNOWLEDGED,
                        ExecutionOrderState.WORKING,
                        ExecutionOrderState.PARTIALLY_FILLED,
                        ExecutionOrderState.CANCEL_REQUESTED)

    @property
    def needs_reconciliation(self) -> bool:
        return self in (ExecutionOrderState.UNKNOWN,
                        ExecutionOrderState.RECONCILIATION_REQUIRED)

    @property
    def is_in_flight(self) -> bool:
        """
        Handed to the broker layer, outcome not yet known.

        A crash here is the dangerous one: the order may exist at the
        broker with nothing local recording it, so recovery treats
        anything in flight as UNKNOWN rather than as never-sent.
        """
        return self in (ExecutionOrderState.SUBMITTING,
                        ExecutionOrderState.SUBMITTED)


#: Legal transitions. Anything absent is refused by `OrderStateMachine`,
#: so an adapter cannot invent a path — for example straight from
#: CREATED to FILLED, skipping the validation and risk gates.
ORDER_TRANSITIONS: Dict[ExecutionOrderState, Set[ExecutionOrderState]] = {
    ExecutionOrderState.CREATED: {
        ExecutionOrderState.VALIDATING, ExecutionOrderState.REJECTED,
        ExecutionOrderState.FAILED},
    ExecutionOrderState.VALIDATING: {
        ExecutionOrderState.APPROVED, ExecutionOrderState.REJECTED,
        ExecutionOrderState.FAILED},
    ExecutionOrderState.APPROVED: {
        ExecutionOrderState.SUBMITTING, ExecutionOrderState.CANCELLED,
        ExecutionOrderState.REJECTED, ExecutionOrderState.EXPIRED,
        ExecutionOrderState.FAILED},
    ExecutionOrderState.SUBMITTING: {
        ExecutionOrderState.SUBMITTED, ExecutionOrderState.REJECTED,
        ExecutionOrderState.FAILED, ExecutionOrderState.UNKNOWN},
    ExecutionOrderState.SUBMITTED: {
        ExecutionOrderState.ACKNOWLEDGED, ExecutionOrderState.WORKING,
        ExecutionOrderState.PARTIALLY_FILLED, ExecutionOrderState.FILLED,
        ExecutionOrderState.REJECTED, ExecutionOrderState.CANCELLED,
        ExecutionOrderState.EXPIRED, ExecutionOrderState.UNKNOWN,
        ExecutionOrderState.FAILED},
    ExecutionOrderState.ACKNOWLEDGED: {
        ExecutionOrderState.WORKING, ExecutionOrderState.PARTIALLY_FILLED,
        ExecutionOrderState.FILLED, ExecutionOrderState.CANCEL_REQUESTED,
        ExecutionOrderState.CANCELLED, ExecutionOrderState.REJECTED,
        ExecutionOrderState.EXPIRED, ExecutionOrderState.UNKNOWN},
    ExecutionOrderState.WORKING: {
        ExecutionOrderState.PARTIALLY_FILLED, ExecutionOrderState.FILLED,
        ExecutionOrderState.CANCEL_REQUESTED, ExecutionOrderState.CANCELLED,
        ExecutionOrderState.EXPIRED, ExecutionOrderState.REJECTED,
        ExecutionOrderState.UNKNOWN},
    ExecutionOrderState.PARTIALLY_FILLED: {
        ExecutionOrderState.PARTIALLY_FILLED, ExecutionOrderState.FILLED,
        ExecutionOrderState.CANCEL_REQUESTED, ExecutionOrderState.CANCELLED,
        ExecutionOrderState.EXPIRED, ExecutionOrderState.UNKNOWN},
    ExecutionOrderState.CANCEL_REQUESTED: {
        # A cancel that has been asked for can still lose the race.
        ExecutionOrderState.CANCELLED, ExecutionOrderState.FILLED,
        ExecutionOrderState.PARTIALLY_FILLED, ExecutionOrderState.WORKING,
        ExecutionOrderState.EXPIRED, ExecutionOrderState.UNKNOWN},
    # Terminal states go nowhere, except back into question when
    # reconciliation finds the broker disagrees.
    ExecutionOrderState.FILLED: {ExecutionOrderState.RECONCILIATION_REQUIRED},
    ExecutionOrderState.CANCELLED: {ExecutionOrderState.RECONCILIATION_REQUIRED},
    ExecutionOrderState.REJECTED: {ExecutionOrderState.RECONCILIATION_REQUIRED},
    ExecutionOrderState.EXPIRED: {ExecutionOrderState.RECONCILIATION_REQUIRED},
    ExecutionOrderState.FAILED: {ExecutionOrderState.RECONCILIATION_REQUIRED},
    # An unknown order is resolved BY asking the broker, so every
    # observable answer is reachable from here.
    ExecutionOrderState.UNKNOWN: {
        ExecutionOrderState.RECONCILIATION_REQUIRED,
        ExecutionOrderState.WORKING, ExecutionOrderState.ACKNOWLEDGED,
        ExecutionOrderState.PARTIALLY_FILLED, ExecutionOrderState.FILLED,
        ExecutionOrderState.CANCELLED, ExecutionOrderState.REJECTED,
        ExecutionOrderState.EXPIRED, ExecutionOrderState.FAILED},
    ExecutionOrderState.RECONCILIATION_REQUIRED: {
        ExecutionOrderState.WORKING, ExecutionOrderState.ACKNOWLEDGED,
        ExecutionOrderState.PARTIALLY_FILLED, ExecutionOrderState.FILLED,
        ExecutionOrderState.CANCELLED, ExecutionOrderState.REJECTED,
        ExecutionOrderState.EXPIRED, ExecutionOrderState.FAILED,
        ExecutionOrderState.UNKNOWN},
}

# A disagreement with the broker can be discovered at ANY point in an
# order life, not only once it has reached a terminal state — a venue
# reporting a fill our records cannot account for is exactly that, and
# it arrives while the order is still working. So every state can reach
# RECONCILIATION_REQUIRED. Added here rather than repeated in each set
# above, so nobody can add a state and forget it.
for _state, _allowed in ORDER_TRANSITIONS.items():
    if _state is not ExecutionOrderState.RECONCILIATION_REQUIRED:
        _allowed.add(ExecutionOrderState.RECONCILIATION_REQUIRED)
del _state, _allowed


class CanonicalOrderType(str, Enum):
    """
    Order types the canonical layer can express (spec §18).

    The last three are declared but no adapter supports them, and
    `BrokerCapability` is what decides. Naming a type is not claiming a
    broker can execute it.
    """
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"
    TRAILING_STOP = "trailing_stop"
    BRACKET = "bracket"
    OCO = "oco"

    @property
    def requires_limit_price(self) -> bool:
        return self in (CanonicalOrderType.LIMIT, CanonicalOrderType.STOP_LIMIT)

    @property
    def requires_stop_price(self) -> bool:
        return self in (CanonicalOrderType.STOP, CanonicalOrderType.STOP_LIMIT,
                        CanonicalOrderType.TRAILING_STOP)


class CanonicalTimeInForce(str, Enum):
    """
    Time in force (spec §19).

    Deliberately a small set. Brokers disagree about the finer
    semantics of everything beyond these four — a GTC that expires
    after 90 days at one venue and never at another is the same word
    for two behaviours — so the canonical layer carries only what can
    be mapped without lying, and each adapter documents its own
    interpretation.
    """
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


class CanonicalOrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is CanonicalOrderSide.BUY else -1


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"

    @staticmethod
    def of(quantity: float) -> "PositionSide":
        if quantity > 1e-12:
            return PositionSide.LONG
        if quantity < -1e-12:
            return PositionSide.SHORT
        return PositionSide.FLAT


class PositionAccounting(str, Enum):
    """
    How a venue represents holdings (spec §17).

    NETTING collapses everything in an instrument into one signed
    position; HEDGING keeps separately-opened lots distinct, so a
    long and a short in the same instrument can coexist. MT5 supports
    both and IBKR nets, so the canonical model has to admit the
    difference rather than assume one.
    """
    NETTING = "netting"
    HEDGING = "hedging"


class ExecutionRejectCode(str, Enum):
    """
    Machine-readable rejection reasons (spec §13, §34).

    Enumerated so rejections are countable and alertable. Every one
    pairs with a human sentence in `REJECT_EXPLANATIONS`.
    """
    # environment and safety
    EXECUTION_DISABLED = "execution_disabled"
    ENVIRONMENT_DISABLED = "environment_disabled"
    REAL_MONEY_BLOCKED = "real_money_blocked"
    EMERGENCY_STOP = "emergency_stop"
    BROKER_DISABLED = "broker_disabled"
    ACCOUNT_DISABLED = "account_disabled"
    STRATEGY_DISABLED = "strategy_disabled"
    PORTFOLIO_DISABLED = "portfolio_disabled"
    NOT_PERMITTED = "not_permitted"
    # routing
    UNKNOWN_BROKER = "unknown_broker"
    UNKNOWN_ACCOUNT = "unknown_account"
    NO_INSTRUMENT_MAPPING = "no_instrument_mapping"
    INSTRUMENT_NOT_TRADABLE = "instrument_not_tradable"
    # capability
    UNSUPPORTED_ORDER_TYPE = "unsupported_order_type"
    UNSUPPORTED_TIME_IN_FORCE = "unsupported_time_in_force"
    UNSUPPORTED_ASSET_CLASS = "unsupported_asset_class"
    SHORTING_NOT_SUPPORTED = "shorting_not_supported"
    FRACTIONAL_NOT_SUPPORTED = "fractional_not_supported"
    # market
    MARKET_CLOSED = "market_closed"
    INSTRUMENT_HALTED = "instrument_halted"
    SESSION_NOT_PERMITTED = "session_not_permitted"
    STALE_DATA = "stale_data"
    NO_PRICE = "no_price"
    # order shape
    INVALID_QUANTITY = "invalid_quantity"
    INVALID_PRICE = "invalid_price"
    QUANTITY_INCREMENT = "quantity_increment"
    BELOW_MINIMUM_QUANTITY = "below_minimum_quantity"
    MISSING_LIMIT_PRICE = "missing_limit_price"
    MISSING_STOP_PRICE = "missing_stop_price"
    # account
    INSUFFICIENT_BUYING_POWER = "insufficient_buying_power"
    INSUFFICIENT_MARGIN = "insufficient_margin"
    POSITION_LIMIT = "position_limit"
    # risk
    RISK_REJECTED = "risk_rejected"
    RISK_UNAVAILABLE = "risk_unavailable"
    PORTFOLIO_CONSTRAINT = "portfolio_constraint"
    # plumbing
    DUPLICATE_INTENT = "duplicate_intent"
    BROKER_DISCONNECTED = "broker_disconnected"
    RATE_LIMITED = "rate_limited"
    ADAPTER_ERROR = "adapter_error"
    NOT_IMPLEMENTED = "not_implemented"


#: Human sentences for each code (spec §34). Kept beside the enum so a
#: new code cannot be added without someone writing the sentence a
#: person will actually read.
REJECT_EXPLANATIONS: Dict[ExecutionRejectCode, str] = {
    ExecutionRejectCode.EXECUTION_DISABLED: "Execution is switched off for this system.",
    ExecutionRejectCode.ENVIRONMENT_DISABLED: "Execution is switched off for this environment.",
    ExecutionRejectCode.REAL_MONEY_BLOCKED: "Real-money execution is not implemented and cannot be enabled.",
    ExecutionRejectCode.EMERGENCY_STOP: "Emergency stop is active. No new orders are accepted.",
    ExecutionRejectCode.BROKER_DISABLED: "This broker is disabled.",
    ExecutionRejectCode.ACCOUNT_DISABLED: "This account is disabled for trading.",
    ExecutionRejectCode.STRATEGY_DISABLED: "This strategy is not permitted to trade.",
    ExecutionRejectCode.PORTFOLIO_DISABLED: "This portfolio is not permitted to trade.",
    ExecutionRejectCode.NOT_PERMITTED: "The caller does not hold the permission this action requires.",
    ExecutionRejectCode.UNKNOWN_BROKER: "No broker is registered under that id.",
    ExecutionRejectCode.UNKNOWN_ACCOUNT: "No account is registered under that id.",
    ExecutionRejectCode.NO_INSTRUMENT_MAPPING: "This instrument has no mapping for this broker.",
    ExecutionRejectCode.INSTRUMENT_NOT_TRADABLE: "This instrument is not tradable at this broker.",
    ExecutionRejectCode.UNSUPPORTED_ORDER_TYPE: "This broker does not support that order type.",
    ExecutionRejectCode.UNSUPPORTED_TIME_IN_FORCE: "This broker does not support that time in force.",
    ExecutionRejectCode.UNSUPPORTED_ASSET_CLASS: "This broker does not trade that asset class.",
    ExecutionRejectCode.SHORTING_NOT_SUPPORTED: "Shorting is not available on this account.",
    ExecutionRejectCode.FRACTIONAL_NOT_SUPPORTED: "This broker requires whole units.",
    ExecutionRejectCode.MARKET_CLOSED: "The market for this instrument is closed.",
    ExecutionRejectCode.INSTRUMENT_HALTED: "Trading in this instrument is halted.",
    ExecutionRejectCode.SESSION_NOT_PERMITTED: "This account may not trade in the current session.",
    ExecutionRejectCode.STALE_DATA: "The market data behind this decision is too old to trade on.",
    ExecutionRejectCode.NO_PRICE: "No usable price is available for this instrument.",
    ExecutionRejectCode.INVALID_QUANTITY: "The order quantity is not a usable positive number.",
    ExecutionRejectCode.INVALID_PRICE: "The order price is not a usable positive number.",
    ExecutionRejectCode.QUANTITY_INCREMENT: "The quantity is not a whole multiple of this instrument's increment.",
    ExecutionRejectCode.BELOW_MINIMUM_QUANTITY: "The quantity is below this instrument's minimum.",
    ExecutionRejectCode.MISSING_LIMIT_PRICE: "This order type requires a limit price.",
    ExecutionRejectCode.MISSING_STOP_PRICE: "This order type requires a stop price.",
    ExecutionRejectCode.INSUFFICIENT_BUYING_POWER: "Insufficient buying power for this order.",
    ExecutionRejectCode.INSUFFICIENT_MARGIN: "Insufficient margin for this order.",
    ExecutionRejectCode.POSITION_LIMIT: "This order would breach a position limit.",
    ExecutionRejectCode.RISK_REJECTED: "The risk engine did not approve this order.",
    ExecutionRejectCode.RISK_UNAVAILABLE: "The risk engine could not be consulted, so the order was not sent.",
    ExecutionRejectCode.PORTFOLIO_CONSTRAINT: "A portfolio exposure limit would be breached.",
    ExecutionRejectCode.DUPLICATE_INTENT: "This intent has already produced an order.",
    ExecutionRejectCode.BROKER_DISCONNECTED: "The broker connection is not available.",
    ExecutionRejectCode.RATE_LIMITED: "The broker request budget for this window is exhausted.",
    ExecutionRejectCode.ADAPTER_ERROR: "The broker adapter failed while handling this order.",
    ExecutionRejectCode.NOT_IMPLEMENTED: "This capability is declared but not implemented in this phase.",
}


def explain(code: ExecutionRejectCode, detail: str = "") -> str:
    """A human sentence for a rejection, optionally with specifics."""
    base = REJECT_EXPLANATIONS.get(code, code.value.replace("_", " "))
    return f"{base} {detail}".strip()


class ExecutionEventType(str, Enum):
    """Broker-neutral event vocabulary (spec §21)."""
    BROKER_CONNECTED = "broker_connected"
    BROKER_DISCONNECTED = "broker_disconnected"
    BROKER_DEGRADED = "broker_degraded"

    ORDER_SUBMITTED = "order_submitted"
    ORDER_ACKNOWLEDGED = "order_acknowledged"
    ORDER_REJECTED = "order_rejected"
    ORDER_UPDATED = "order_updated"
    ORDER_PARTIALLY_FILLED = "order_partially_filled"
    ORDER_FILLED = "order_filled"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_EXPIRED = "order_expired"
    ORDER_UNKNOWN = "order_unknown"

    EXECUTION_ERROR = "execution_error"

    POSITION_UPDATED = "position_updated"
    BALANCE_UPDATED = "balance_updated"
    ACCOUNT_UPDATED = "account_updated"

    RECONCILIATION_STARTED = "reconciliation_started"
    RECONCILIATION_COMPLETED = "reconciliation_completed"
    RECONCILIATION_MISMATCH = "reconciliation_mismatch"

    KILL_SWITCH_ACTIVATED = "kill_switch_activated"
    KILL_SWITCH_RELEASED = "kill_switch_released"


class MismatchKind(str, Enum):
    """What reconciliation found (spec §23)."""
    MISSING_INTERNAL_ORDER = "missing_internal_order"
    UNKNOWN_BROKER_ORDER = "unknown_broker_order"
    MISSING_FILL = "missing_fill"
    DUPLICATE_FILL = "duplicate_fill"
    POSITION_MISMATCH = "position_mismatch"
    CASH_MISMATCH = "cash_mismatch"
    QUANTITY_MISMATCH = "quantity_mismatch"
    PRICE_MISMATCH = "price_mismatch"
    STATUS_MISMATCH = "status_mismatch"
    UNKNOWN_STATE = "unknown_state"


class MarketStatus(str, Enum):
    """
    Session status for an instrument (spec §40).

    Derived from the Phase 12 calendar, which is built from cached
    daily bars. That calendar can distinguish a session day from a
    non-session day and nothing finer, so PRE_MARKET, AFTER_HOURS and
    HALTED are declared for future adapters that can report them and
    are never guessed here. UNKNOWN is returned when the data cannot
    answer, which is different from CLOSED.
    """
    OPEN = "open"
    CLOSED = "closed"
    PRE_MARKET = "pre_market"
    AFTER_HOURS = "after_hours"
    HOLIDAY = "holiday"
    HALTED = "halted"
    UNKNOWN = "unknown"

    @property
    def permits_regular_trading(self) -> bool:
        return self is MarketStatus.OPEN


class ExecutionPermission(str, Enum):
    """
    What a caller is allowed to do (spec §47).

    This project has no user accounts and no HTTP layer, so these are
    enforced at the service facade rather than by a web framework. The
    distinction that matters is preserved: reading execution state and
    causing execution are different permissions, and the live one
    cannot be granted at all.
    """
    VIEW_EXECUTION = "view_execution"
    VIEW_ACCOUNT = "view_account"
    VIEW_ORDERS = "view_orders"
    DRY_RUN_EXECUTION = "dry_run_execution"
    PAPER_EXECUTION = "paper_execution"
    DEMO_EXECUTION = "demo_execution"
    LIVE_EXECUTION_ADMIN = "live_execution_admin"


#: The read-only set, which is what every ordinary caller gets.
READ_ONLY_PERMISSIONS: Tuple[ExecutionPermission, ...] = (
    ExecutionPermission.VIEW_EXECUTION,
    ExecutionPermission.VIEW_ACCOUNT,
    ExecutionPermission.VIEW_ORDERS,
)


# ============================================================
# Broker, account, connection
# ============================================================

@dataclass
class BrokerCapability:
    """
    What a broker can actually do (spec §14).

    Every flag defaults to False. A capability has to be claimed
    explicitly by an adapter that implements it, so the failure mode of
    a half-written adapter is "refuses to trade" rather than "silently
    sends an order the venue rejects".
    """
    broker_id: str

    supports_market_orders: bool = False
    supports_limit_orders: bool = False
    supports_stop_orders: bool = False
    supports_stop_limit_orders: bool = False
    supports_trailing_stop: bool = False
    supports_bracket_orders: bool = False
    supports_oco: bool = False

    supports_order_modification: bool = False
    supports_partial_fills: bool = False
    supports_fractional_quantity: bool = False
    supports_shorting: bool = False
    supports_margin: bool = False
    supports_extended_hours: bool = False
    supports_streaming: bool = False
    supports_realtime_quotes: bool = False

    #: Asset classes this adapter will accept, by canonical name.
    asset_classes: Tuple[str, ...] = ()
    #: Times in force this adapter will accept.
    times_in_force: Tuple[CanonicalTimeInForce, ...] = ()
    #: How the venue represents holdings.
    position_accounting: PositionAccounting = PositionAccounting.NETTING
    #: Requests per minute the adapter will issue, when it knows.
    rate_limit_per_minute: Optional[int] = None
    notes: str = ""

    def supports_order_type(self, order_type: CanonicalOrderType) -> bool:
        return {
            CanonicalOrderType.MARKET: self.supports_market_orders,
            CanonicalOrderType.LIMIT: self.supports_limit_orders,
            CanonicalOrderType.STOP: self.supports_stop_orders,
            CanonicalOrderType.STOP_LIMIT: self.supports_stop_limit_orders,
            CanonicalOrderType.TRAILING_STOP: self.supports_trailing_stop,
            CanonicalOrderType.BRACKET: self.supports_bracket_orders,
            CanonicalOrderType.OCO: self.supports_oco,
        }.get(order_type, False)

    def supports_time_in_force(self, tif: CanonicalTimeInForce) -> bool:
        return tif in self.times_in_force

    def supports_asset_class(self, asset_class: Optional[str]) -> bool:
        if not self.asset_classes:
            return False
        return (asset_class or "").lower() in {a.lower() for a in self.asset_classes}

    def as_dict(self) -> Dict[str, Any]:
        return {
            "broker_id": self.broker_id,
            "order_types": [t.value for t in CanonicalOrderType
                            if self.supports_order_type(t)],
            "times_in_force": [t.value for t in self.times_in_force],
            "asset_classes": list(self.asset_classes),
            "position_accounting": self.position_accounting.value,
            "modification": self.supports_order_modification,
            "partial_fills": self.supports_partial_fills,
            "fractional": self.supports_fractional_quantity,
            "shorting": self.supports_shorting,
            "margin": self.supports_margin,
            "extended_hours": self.supports_extended_hours,
            "streaming": self.supports_streaming,
            "realtime_quotes": self.supports_realtime_quotes,
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "notes": self.notes,
        }


@dataclass
class Broker:
    """
    A registered venue, real or otherwise.

    Carries no credential and no endpoint. Both belong to the adapter
    that connects, and an adapter reads them from the environment at
    connect time — putting either here would mean a database row, an
    export or a dashboard payload could carry a secret.
    """
    broker_id: str
    name: str
    environment: ExecutionEnvironment = ExecutionEnvironment.PAPER
    adapter: str = ""
    enabled: bool = True
    #: False when the adapter is a declared shape with no implementation.
    implemented: bool = True
    created_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.created_at = _require_utc(self.created_at, "created_at")
        if self.environment.is_real_money and self.implemented:
            raise ValueError(
                "a live-environment broker cannot be marked implemented in "
                "Phase 14 — no real-money adapter exists")

    @property
    def can_trade(self) -> bool:
        return self.enabled and self.implemented and not self.environment.is_real_money


@dataclass
class BrokerAccount:
    """
    One account at one broker (spec §37).

    An order intent never assumes a global account: routing is always
    explicit, because a system that defaults to "the account" is one
    configuration change away from trading the wrong book.
    """
    account_id: str
    broker_id: str
    name: str
    environment: ExecutionEnvironment = ExecutionEnvironment.PAPER
    base_currency: str = "USD"
    enabled: bool = True
    position_accounting: PositionAccounting = PositionAccounting.NETTING
    #: What this account is allowed to do, independent of the caller.
    permissions: Tuple[ExecutionPermission, ...] = READ_ONLY_PERMISSIONS
    #: Free-form link back to the paper session or portfolio it stands for.
    linked_reference: Optional[str] = None
    created_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.created_at = _require_utc(self.created_at, "created_at")
        if self.environment.is_real_money:
            raise ValueError(
                "a live-environment account cannot be created in Phase 14")

    @property
    def can_trade(self) -> bool:
        return self.enabled and not self.environment.is_real_money


@dataclass
class BrokerConnection:
    """Connection state and its history of attempts (spec §26)."""
    broker_id: str
    state: BrokerConnectionState = BrokerConnectionState.DISCONNECTED
    since: Optional[datetime] = None
    last_heartbeat_at: Optional[datetime] = None
    attempts: int = 0
    #: Bounded, never infinite. A retry loop with no ceiling is how a
    #: broken adapter turns into a rate-limit ban.
    max_attempts: int = 5
    backoff_seconds: float = 1.0
    max_backoff_seconds: float = 60.0
    detail: str = ""

    def __post_init__(self):
        self.since = _require_utc(self.since, "since")
        self.last_heartbeat_at = _require_utc(self.last_heartbeat_at,
                                              "last_heartbeat_at")

    @property
    def exhausted(self) -> bool:
        return self.attempts >= self.max_attempts

    def next_backoff(self) -> float:
        """Exponential, capped. Returns the delay for the attempt just made."""
        delay = self.backoff_seconds * (2 ** max(0, self.attempts - 1))
        return float(min(delay, self.max_backoff_seconds))


@dataclass
class BrokerHealth:
    """A point-in-time health reading for one broker."""
    broker_id: str
    at: Optional[datetime] = None
    state: BrokerConnectionState = BrokerConnectionState.DISCONNECTED
    latency_ms: Optional[float] = None
    detail: str = ""
    consecutive_failures: int = 0

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")

    @property
    def is_usable(self) -> bool:
        return self.state.can_submit


# ============================================================
# Instruments
# ============================================================

@dataclass
class BrokerInstrumentMapping:
    """
    Canonical instrument to broker symbol (spec §15).

    The core never learns that one venue calls it `EURUSD.a` and
    another `EUR.USD`. Everything venue-shaped — suffixes, contract
    multipliers, lot sizes, tick sizes — lives on this row and is
    applied by the adapter.

    `quantity_increment` and `minimum_quantity` are here rather than on
    the instrument because they are venue facts, not security facts:
    the same share is fractional at one broker and whole-lot at
    another.
    """
    canonical_instrument_id: str
    broker_id: str
    broker_symbol: str
    venue: str = ""
    asset_class: str = "stock"
    currency: str = "USD"
    tick_size: Optional[float] = None
    lot_size: Optional[float] = None
    minimum_quantity: Optional[float] = None
    quantity_increment: Optional[float] = None
    price_precision: Optional[int] = None
    contract_multiplier: float = 1.0
    timezone_name: str = "UTC"
    trading_hours: str = ""
    tradable: bool = True
    #: Whatever the adapter needs and the core must not interpret.
    broker_payload: Dict[str, Any] = field(default_factory=dict)

    def normalize_quantity(self, quantity: float) -> Tuple[float, Optional[ExecutionRejectCode]]:
        """
        Round a quantity down to what the venue accepts.

        Rounds DOWN rather than to nearest, deliberately: rounding up
        would submit more exposure than the risk engine sized, and the
        risk engine is the authority on size.

        A negative quantity is REFUSED rather than made positive.
        Direction lives in `side`, so a negative here means the caller
        has a sign error somewhere — and quietly flipping it would
        submit an order in a direction nobody asked for.
        """
        try:
            value = float(quantity)
        except (TypeError, ValueError):
            return 0.0, ExecutionRejectCode.INVALID_QUANTITY
        if not isfinite(value) or value <= 0:
            return 0.0, ExecutionRejectCode.INVALID_QUANTITY

        increment = self.quantity_increment
        if increment and increment > 0:
            steps = int(value / increment + 1e-9)
            value = steps * increment
            if steps == 0:
                return 0.0, ExecutionRejectCode.QUANTITY_INCREMENT

        minimum = self.minimum_quantity
        if minimum and value + 1e-12 < minimum:
            return value, ExecutionRejectCode.BELOW_MINIMUM_QUANTITY

        return value, None

    def normalize_price(self, price: Optional[float]) -> Optional[float]:
        """Round a price to the venue's tick, when it declares one."""
        value = finite_or_none(price)
        if value is None:
            return None
        if self.tick_size and self.tick_size > 0:
            value = round(value / self.tick_size) * self.tick_size
        if self.price_precision is not None:
            value = round(value, self.price_precision)
        return value


# ============================================================
# Orders, fills, events
# ============================================================

@dataclass
class ExecutionOrder:
    """
    The canonical order (spec §10).

    Six identifiers, and they are genuinely six different things:

      intent_id        the portfolio decision this came from
      order_id         our record, stable across everything below
      client_order_id  what we told the broker to call it
      broker_order_id  what the broker calls it, learned on acceptance
      execution_id     per broker execution report
      fill_id          per individual fill

    Collapsing any pair breaks a real case. A submission that times out
    has an order_id and a client_order_id but no broker_order_id, and
    that gap is exactly what reconciliation searches on.
    """
    order_id: str
    intent_id: str
    broker_id: str
    account_id: str
    instrument_id: str
    side: CanonicalOrderSide
    quantity: float

    order_type: CanonicalOrderType = CanonicalOrderType.MARKET
    time_in_force: CanonicalTimeInForce = CanonicalTimeInForce.DAY
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None

    state: ExecutionOrderState = ExecutionOrderState.CREATED
    filled_quantity: float = 0.0
    average_fill_price: Optional[float] = None
    reject_code: Optional[ExecutionRejectCode] = None
    reject_detail: str = ""

    idempotency_key: str = ""
    client_order_id: str = ""
    broker_order_id: Optional[str] = None
    broker_symbol: str = ""

    # provenance (spec §35)
    correlation_id: str = ""
    signal_id: Optional[str] = None
    prediction_id: Optional[str] = None
    model_version: Optional[str] = None
    strategy_id: Optional[str] = None
    portfolio_id: Optional[str] = None
    decision_id: Optional[str] = None
    execution_policy: str = "market"
    environment: ExecutionEnvironment = ExecutionEnvironment.PAPER

    # timestamps (spec §41)
    intent_at: Optional[datetime] = None
    validated_at: Optional[datetime] = None
    submitted_at: Optional[datetime] = None
    acknowledged_at: Optional[datetime] = None
    terminal_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    # execution quality (spec §44)
    decision_price: Optional[float] = None
    reference_price: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    submitted_price: Optional[float] = None
    commission: float = 0.0
    fees: float = 0.0
    note: str = ""

    def __post_init__(self):
        for name in ("intent_at", "validated_at", "submitted_at",
                     "acknowledged_at", "terminal_at", "expires_at"):
            setattr(self, name, _require_utc(getattr(self, name), name))
        if self.quantity <= 0:
            raise ValueError("order quantity must be positive; direction is `side`")
        if self.order_type.requires_limit_price and self.limit_price is None:
            raise ValueError(f"{self.order_type.value} requires a limit_price")
        if self.order_type.requires_stop_price and self.stop_price is None:
            raise ValueError(f"{self.order_type.value} requires a stop_price")

    @property
    def remaining(self) -> float:
        return max(0.0, self.quantity - self.filled_quantity)

    @property
    def signed_filled(self) -> float:
        return self.filled_quantity * self.side.sign

    @property
    def is_overfilled(self) -> bool:
        """The invariant of spec §64: fills may never exceed the order."""
        return self.filled_quantity > self.quantity + 1e-9

    @property
    def slippage(self) -> Optional[float]:
        """Signed cost against the decision price, in price units."""
        if self.average_fill_price is None or self.decision_price is None:
            return None
        return finite_or_none(
            (self.average_fill_price - self.decision_price) * self.side.sign)

    @property
    def slippage_bps(self) -> Optional[float]:
        raw = self.slippage
        if raw is None or not self.decision_price:
            return None
        return finite_or_none(raw / abs(self.decision_price) * 10_000.0)

    @property
    def execution_latency_seconds(self) -> Optional[float]:
        if self.submitted_at is None or self.acknowledged_at is None:
            return None
        return (self.acknowledged_at - self.submitted_at).total_seconds()


@dataclass
class OrderStateTransition:
    """
    One step in an order's life (spec §54).

    Stored rather than derived, because current state alone cannot
    answer the questions this history exists for: how long the order
    worked before filling, whether it passed through UNKNOWN, which
    event caused each move.
    """
    order_id: str
    sequence: int
    from_state: Optional[ExecutionOrderState]
    to_state: ExecutionOrderState
    at: Optional[datetime] = None
    reason: str = ""
    event_id: Optional[str] = None
    correlation_id: str = ""

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")


@dataclass
class ExecutionFill:
    """
    One execution report (spec §43, §44).

    Broker cost structures do not agree, so `raw_broker_payload` keeps
    whatever the venue said alongside the normalized fields. Discarding
    it would lose information that only becomes interesting after
    something looks wrong.
    """
    fill_id: str
    order_id: str
    broker_id: str
    account_id: str
    instrument_id: str
    side: CanonicalOrderSide
    quantity: float
    price: float
    filled_at: Optional[datetime] = None

    execution_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    commission: float = 0.0
    fees: float = 0.0
    exchange_fees: float = 0.0
    financing: float = 0.0
    taxes: float = 0.0
    currency: str = "USD"
    reference_price: Optional[float] = None
    liquidity: str = ""
    idempotency_key: str = ""
    correlation_id: str = ""
    raw_broker_payload: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.filled_at = _require_utc(self.filled_at, "filled_at")
        if self.quantity <= 0:
            raise ValueError("fill quantity must be positive; direction is `side`")

    @property
    def notional(self) -> float:
        return abs(self.quantity) * self.price

    @property
    def total_cost(self) -> float:
        return (self.commission + self.fees + self.exchange_fees
                + self.financing + self.taxes)

    @property
    def signed_quantity(self) -> float:
        return self.quantity * self.side.sign


@dataclass
class ExecutionEvent:
    """
    One broker-neutral event (spec §21, §22).

    `sequence` is the broker's own ordering when it provides one and
    None when it does not. `received_at` is when we saw it, which is
    frequently not when it happened — and the difference is what makes
    out-of-order handling necessary rather than theoretical.
    """
    event_id: str
    event_type: ExecutionEventType
    at: Optional[datetime] = None
    received_at: Optional[datetime] = None
    source: str = "system"
    broker_id: Optional[str] = None
    account_id: Optional[str] = None
    order_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    fill_id: Optional[str] = None
    instrument_id: Optional[str] = None
    correlation_id: str = ""
    idempotency_key: str = ""
    sequence: Optional[int] = None
    payload: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")
        self.received_at = _require_utc(self.received_at, "received_at")
        if not self.idempotency_key:
            # An event with no key of its own still needs one, or a
            # redelivery is indistinguishable from a new event.
            self.idempotency_key = event_key(self)


def event_key(event: "ExecutionEvent") -> str:
    """Deterministic identity for an event that carries no broker key."""
    raw = "|".join([
        event.event_type.value,
        event.broker_id or "", event.order_id or "",
        event.broker_order_id or "", event.fill_id or "",
        event.at.isoformat() if event.at else "",
        str(event.sequence if event.sequence is not None else ""),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


@dataclass
class ExecutionError:
    """A structured failure (spec §25). Never a bare exception string."""
    error_id: str
    at: Optional[datetime] = None
    code: ExecutionRejectCode = ExecutionRejectCode.ADAPTER_ERROR
    message: str = ""
    broker_id: Optional[str] = None
    account_id: Optional[str] = None
    order_id: Optional[str] = None
    correlation_id: str = ""
    retryable: bool = False
    context: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")

    @property
    def explanation(self) -> str:
        return explain(self.code, self.message)


# ============================================================
# Account and position snapshots
# ============================================================

@dataclass
class CashBalance:
    """One currency's balance. Accounts can hold several."""
    currency: str
    cash: float = 0.0
    settled: Optional[float] = None
    unsettled: Optional[float] = None


@dataclass
class MarginSnapshot:
    """
    Margin, where a venue reports it.

    Every field is Optional because a cash account has no margin at
    all, and defaulting those to zero would make "no margin concept"
    indistinguishable from "no margin used".
    """
    initial_margin: Optional[float] = None
    maintenance_margin: Optional[float] = None
    margin_used: Optional[float] = None
    margin_available: Optional[float] = None
    leverage: Optional[float] = None


@dataclass
class AccountSnapshot:
    """
    Normalized account state (spec §16).

    `raw_broker_payload` is kept for the same reason as on fills:
    normalization is lossy, and the lost part is often what explains a
    discrepancy.
    """
    account_id: str
    broker_id: str
    at: Optional[datetime] = None
    base_currency: str = "USD"
    cash: float = 0.0
    equity: float = 0.0
    available_funds: Optional[float] = None
    buying_power: Optional[float] = None
    portfolio_value: Optional[float] = None
    realized_pnl: float = 0.0
    unrealized_pnl: Optional[float] = None
    balances: List[CashBalance] = field(default_factory=list)
    margin: MarginSnapshot = field(default_factory=MarginSnapshot)
    environment: ExecutionEnvironment = ExecutionEnvironment.PAPER
    raw_broker_payload: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")

    @property
    def spendable(self) -> float:
        """
        What a buy may consume.

        Buying power when the venue reports it, otherwise available
        funds, otherwise cash — in that order, because each is a
        weaker statement than the one before and silently substituting
        a stronger one would overstate capacity.
        """
        for candidate in (self.buying_power, self.available_funds):
            value = finite_or_none(candidate)
            if value is not None:
                return value
        return finite_or_none(self.cash) or 0.0


@dataclass
class PositionSnapshot:
    """Normalized position (spec §17)."""
    account_id: str
    broker_id: str
    instrument_id: str
    quantity: float = 0.0
    average_price: float = 0.0
    at: Optional[datetime] = None
    market_price: Optional[float] = None
    realized_pnl: float = 0.0
    unrealized_pnl: Optional[float] = None
    currency: str = "USD"
    broker_symbol: str = ""
    #: Set only under HEDGING accounting, where lots stay distinct.
    lot_id: Optional[str] = None
    raw_broker_payload: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")

    @property
    def side(self) -> PositionSide:
        return PositionSide.of(self.quantity)

    @property
    def market_value(self) -> Optional[float]:
        if self.market_price is None:
            return None
        return finite_or_none(self.quantity * self.market_price)


# ============================================================
# Reconciliation
# ============================================================

@dataclass
class ReconciliationMismatch:
    """One disagreement between our books and the broker's."""
    kind: MismatchKind
    detail: str
    instrument_id: Optional[str] = None
    order_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    internal_value: Optional[Any] = None
    broker_value: Optional[Any] = None
    #: Always False in this phase. Nothing auto-repairs (spec §23).
    resolved: bool = False


@dataclass
class ReconciliationRecord:
    """
    The outcome of one reconciliation pass (spec §23).

    Append-only. There is deliberately no method anywhere that edits a
    stored record: a system that could rewrite its own reconciliation
    history would destroy the only evidence that something went wrong.
    """
    reconciliation_id: str
    broker_id: str
    account_id: str
    at: Optional[datetime] = None
    scope: str = "all"
    orders_compared: int = 0
    fills_compared: int = 0
    positions_compared: int = 0
    checks_performed: int = 0
    mismatches: List[ReconciliationMismatch] = field(default_factory=list)
    correlation_id: str = ""
    detail: str = ""

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")

    @property
    def is_clean(self) -> bool:
        return not self.mismatches

    def of_kind(self, kind: MismatchKind) -> List[ReconciliationMismatch]:
        return [m for m in self.mismatches if m.kind is kind]


# ============================================================
# Validation and dry run
# ============================================================

@dataclass
class ValidationFinding:
    """One pre-trade check that failed (spec §13)."""
    code: ExecutionRejectCode
    detail: str = ""
    at: Optional[datetime] = None
    context: Dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")

    @property
    def explanation(self) -> str:
        return explain(self.code, self.detail)


@dataclass
class ValidationResult:
    """
    Everything the pre-trade gate concluded.

    Collects ALL findings rather than stopping at the first. One
    rejection tells an operator what to fix; the full list tells them
    whether fixing it will help.
    """
    passed: bool = True
    findings: List[ValidationFinding] = field(default_factory=list)
    checks_performed: int = 0
    normalized_quantity: Optional[float] = None
    normalized_limit_price: Optional[float] = None
    normalized_stop_price: Optional[float] = None
    broker_symbol: str = ""
    market_status: MarketStatus = MarketStatus.UNKNOWN

    def fail(self, code: ExecutionRejectCode, detail: str = "",
             at: Optional[datetime] = None, correlation_id: str = "",
             **context) -> "ValidationResult":
        self.passed = False
        self.findings.append(ValidationFinding(
            code=code, detail=detail, at=at,
            correlation_id=correlation_id, context=context))
        return self

    @property
    def codes(self) -> List[ExecutionRejectCode]:
        return [f.code for f in self.findings]

    @property
    def first_code(self) -> Optional[ExecutionRejectCode]:
        return self.findings[0].code if self.findings else None

    @property
    def explanation(self) -> str:
        if self.passed:
            return "All pre-trade checks passed."
        return " ".join(f.explanation for f in self.findings)


@dataclass
class DryRunResult:
    """
    What would have been sent, and confirmation that it was not
    (spec §33).

    `actually_submitted` is a permanent False that is nevertheless
    reported on every result. A dry run whose output looked identical
    to a real submission would be exactly the wrong thing to hand an
    operator.
    """
    correlation_id: str
    intent_id: str
    broker_id: str
    account_id: str
    instrument_id: str
    side: CanonicalOrderSide
    quantity: Optional[float] = None
    order_type: CanonicalOrderType = CanonicalOrderType.MARKET
    time_in_force: CanonicalTimeInForce = CanonicalTimeInForce.DAY
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    broker_symbol: str = ""
    environment: ExecutionEnvironment = ExecutionEnvironment.PAPER

    validation: ValidationResult = field(default_factory=ValidationResult)
    risk_passed: Optional[bool] = None
    capability_passed: Optional[bool] = None
    mapping_passed: Optional[bool] = None
    market_status: MarketStatus = MarketStatus.UNKNOWN
    would_submit: bool = False
    #: Structurally always False. Nothing in a dry run can submit.
    actually_submitted: bool = False
    broker_request: Dict[str, Any] = field(default_factory=dict)
    at: Optional[datetime] = None

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")
        if self.actually_submitted:
            raise ValueError("a dry run can never report an actual submission")

    def render(self) -> str:
        """The operator-facing summary (spec §33)."""
        def mark(value: Optional[bool]) -> str:
            return "PASS" if value else ("n/a" if value is None else "FAIL")

        lines = [
            "DRY RUN RESULT",
            "",
            f"Instrument: {self.instrument_id}"
            + (f" ({self.broker_symbol})" if self.broker_symbol else ""),
            f"Side: {self.side.value.upper()}",
            f"Quantity: {'—' if self.quantity is None else f'{self.quantity:g}'}",
            f"Order Type: {self.order_type.value.upper()}",
        ]
        if self.limit_price is not None:
            lines.append(f"Price: {self.limit_price:g}")
        if self.stop_price is not None:
            lines.append(f"Stop: {self.stop_price:g}")
        lines += [
            "",
            f"Broker: {self.broker_id}",
            f"Account: {self.account_id}",
            f"Environment: {self.environment.value.upper()}",
            f"Market: {self.market_status.value.upper()}",
            "",
            f"Validation: {mark(self.validation.passed)}",
            f"Risk: {mark(self.risk_passed)}",
            f"Capability: {mark(self.capability_passed)}",
            f"Mapping: {mark(self.mapping_passed)}",
            "",
            f"Would Submit: {'YES' if self.would_submit else 'NO'}",
            f"Actually Submitted: NO",
        ]
        if not self.validation.passed:
            lines += ["", "Why not:"]
            lines += [f"  - {f.explanation}" for f in self.validation.findings]
        return "\n".join(lines)


@dataclass
class ExecutionResult:
    """
    What the orchestrator did with one intent.

    Carries the order when one was created and the validation result
    always — including on the rejection path, where the reason is the
    whole point of the return value.
    """
    correlation_id: str
    intent_id: str
    accepted: bool = False
    order: Optional[ExecutionOrder] = None
    validation: ValidationResult = field(default_factory=ValidationResult)
    events: List[ExecutionEvent] = field(default_factory=list)
    fills: List[ExecutionFill] = field(default_factory=list)
    error: Optional[ExecutionError] = None
    environment: ExecutionEnvironment = ExecutionEnvironment.PAPER
    duplicate_of: Optional[str] = None
    at: Optional[datetime] = None

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")

    @property
    def reject_code(self) -> Optional[ExecutionRejectCode]:
        if self.error is not None:
            return self.error.code
        return self.validation.first_code

    @property
    def explanation(self) -> str:
        if self.accepted:
            return "Order accepted by the execution layer."
        if self.error is not None:
            return self.error.explanation
        return self.validation.explanation


# ============================================================
# Audit
# ============================================================

@dataclass
class AuditEvent:
    """
    One recorded action with an actor attached (spec §36).

    Actor is required rather than defaulted, because "who caused this"
    with a silent default is the field that becomes useless first.
    """
    audit_id: str
    at: Optional[datetime]
    action: str
    actor: str
    subject_type: str = ""
    subject_id: str = ""
    correlation_id: str = ""
    detail: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")
        if not self.actor:
            raise ValueError("an audit event must name an actor")
