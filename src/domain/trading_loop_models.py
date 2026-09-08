"""
src/domain/trading_loop_models.py
---------------------------------------
The vocabulary of the Phase 25 paper-trading operating loop.

WHAT PHASE 25 ADDS THAT DID NOT EXIST
-----------------------------------------
Every layer the loop passes through was already built and tested:
Phase 11 sizes and decides, Phase 14-16 validates and submits, Phase 15
speaks to IBKR, Phase 19-21 measures and remembers. What did not exist
was a CYCLE — one bounded, restart-safe advance that carries a signal
all the way to a reconciled position and back into memory.

`src/execution/intake.from_decision()` was written in Phase 17 to close
the joint between risk and execution, and until this phase it had
**zero callers**. That single fact is most of what Phase 25 is.

THE THREE STATES THAT MUST NEVER MERGE (spec §4, §16)
---------------------------------------------------------
    what we WANT       ->  TargetPosition     (portfolio's answer)
    what we ASKED FOR  ->  OrderIntent/Order  (execution's record)
    what we HOLD       ->  ActualPosition     (reconciled from broker)

A system that keeps one number for these reports its intentions as its
holdings. `PositionDelta` exists precisely so the gap between the first
and the third is a value with a name, rather than an assumption.

FAIL CLOSED IS A TYPE, NOT A HABIT (spec §25)
-------------------------------------------------
`TradingMode.resolve()` returns OFF for anything it does not
positively recognise: absent, blank, misspelled, corrupted, ambiguous,
or LIVE. There is no branch in this module where an unrecognised input
results in permission to trade, and `test_fail_closed.py` enumerates
them.

LIVE IS DECLARED AND UNREACHABLE (spec §9, §39)
---------------------------------------------------
`TradingMode.LIVE` exists as a member so that code reading a stored
value can NAME what it is refusing. It is never permitted:
`is_permitted` is False, `resolve()` maps it to OFF with a reason, and
`assert_can_trade` raises. Deleting the member would not make live
trading harder — it would make a database row saying "live" resolve
through the generic unknown path instead of the specific refusal, and
the log would stop saying which boundary was hit.

NO OVERALL QUALITY SCORE (spec §41)
---------------------------------------
`PaperValidation` keeps model, signal, portfolio, risk, execution,
strategy and operational quality in seven named fields with no total.
The same reasoning as Phase 24's scorecard: a single number gets
sorted, and a strategy with excellent predictions and unusable
execution must not average into "fine".
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Bumped when the loop's semantics change. Written onto every cycle so
#: two methodologies produce rows beside each other rather than one
#: silently replacing the other -- the convention since Phase 19.
LOOP_METHOD_VERSION = "phase25-v1"

#: Quantum of the decision clock. Every cycle anchors to a boundary of
#: this size, and that is what makes a re-run idempotent: Phase 11
#: derives `decision_id` from `as_of`, so a wall-clock anchor would
#: mint a new decision -- and therefore a new order -- on every
#: invocation. See `cycle_anchor`.
DEFAULT_CYCLE_SECONDS = 900          # 15 minutes


def require_utc(value: Optional[datetime], name: str = "moment"
                ) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (UTC)")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be in UTC (got offset {value.utcoffset()})")
    return value


def finite_or_none(value: Any) -> Optional[float]:
    """A float, or None -- never a NaN or an infinity travelling as a number."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def cycle_anchor(now: datetime, seconds: int = DEFAULT_CYCLE_SECONDS) -> datetime:
    """
    Floor `now` to the cycle boundary.

    THE IDEMPOTENCY FOUNDATION. Phase 11's `RiskEngine._decision_id` is
    a hash over `(engine_version, constraint_version, portfolio_id,
    as_of, proposal_id)`. Feed it `datetime.now()` and a workflow that
    retries produces a second decision id, a second intent id and a
    second order for the same intention. Feed it an anchor and the
    retry recomputes the identical id, the orchestrator's idempotency
    index recognises it, and nothing doubles.

    Spec §10 lists seven ways the system must survive being run twice.
    All seven reduce to this.
    """
    require_utc(now, "now")
    if seconds <= 0:
        raise ValueError("cycle length must be positive")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    elapsed = int((now - epoch).total_seconds())
    return epoch + timedelta(seconds=(elapsed // seconds) * seconds)


# ======================================================================
# Trading mode (§9)
# ======================================================================

class TradingMode(str, Enum):
    """
    What the system is permitted to do.

    LIVE is a member and is never permitted -- see the module
    docstring. OFF is the value every failure resolves to.
    """
    OFF = "off"
    PAPER = "paper"
    LIVE = "live"

    @property
    def is_permitted(self) -> bool:
        """PAPER is the only mode Phase 25 allows to trade."""
        return self is TradingMode.PAPER

    @property
    def is_real_money(self) -> bool:
        return self is TradingMode.LIVE

    @classmethod
    def resolve(cls, raw: Any) -> Tuple["TradingMode", str]:
        """
        Interpret a stored or configured value, failing closed.

        Returns `(mode, reason)`. The reason is always populated for a
        non-PAPER answer, because an operator looking at a blocked loop
        needs to know WHICH refusal fired -- "live is blocked in this
        phase" and "the mode column is empty" are different problems
        with different fixes.
        """
        if raw is None:
            return cls.OFF, "no trading mode is configured"
        if isinstance(raw, cls):
            candidate = raw
        else:
            text = str(raw).strip().lower()
            if not text:
                return cls.OFF, "the configured trading mode is blank"
            try:
                candidate = cls(text)
            except ValueError:
                return cls.OFF, (f"{text!r} is not a trading mode this system "
                                 f"recognises")
        if candidate is cls.LIVE:
            return cls.OFF, ("live trading is blocked in this phase and there "
                             "is no configuration that enables it")
        if candidate is cls.OFF:
            return cls.OFF, "trading is switched off"
        return cls.PAPER, ""


class ModeSource(str, Enum):
    """Where a mode came from. A stored mode outranks a default."""
    STORED = "stored"
    ENVIRONMENT = "environment"
    DEFAULT = "default"
    OVERRIDE = "override"


@dataclass(frozen=True)
class ModeResolution:
    """
    The mode, why it is that, and where it came from.

    Frozen: a resolution is evidence about one moment. Re-resolving is
    cheap; mutating one in place would let a later stage of the same
    cycle see a mode the earlier stages did not.
    """
    mode: TradingMode
    source: ModeSource
    reason: str = ""
    resolved_at: Optional[datetime] = None
    stored_raw: Optional[str] = None

    @property
    def may_trade(self) -> bool:
        return self.mode.is_permitted

    def as_dict(self) -> Dict[str, Any]:
        return {"mode": self.mode.value, "source": self.source.value,
                "reason": self.reason, "stored_raw": self.stored_raw,
                "resolved_at": (self.resolved_at.isoformat()
                                if self.resolved_at else None)}


class TradingModeRefused(Exception):
    """
    Raised when something asks to trade in a mode that is not permitted.

    An exception rather than a False: spec §39 says the boundary should
    be hard to remove by accident, and a caller cannot forget to check
    a raise.
    """


def assert_can_trade(resolution: ModeResolution) -> None:
    if not resolution.may_trade:
        raise TradingModeRefused(
            f"trading is not permitted: mode is {resolution.mode.value.upper()}"
            + (f" -- {resolution.reason}" if resolution.reason else ""))


# ======================================================================
# Why a cycle refuses to trade (§25)
# ======================================================================

class BlockReason(str, Enum):
    """
    Every condition under which the loop declines to create orders.

    One member per line of spec §25, plus the ones the code found.
    They are separate members rather than one GENERIC_BLOCK because the
    operator response differs: stale data waits, an unhealthy broker
    needs a gateway restart, a kill switch needs a person.
    """
    MODE_NOT_PERMITTED = "mode_not_permitted"
    MODE_UNKNOWN = "mode_unknown"
    KILL_SWITCH = "kill_switch"
    NO_MARKET_DATA = "no_market_data"
    STALE_MARKET_DATA = "stale_market_data"
    STALE_SIGNAL = "stale_signal"
    MODEL_NOT_DEPLOYABLE = "model_not_deployable"
    PORTFOLIO_STATE_UNKNOWN = "portfolio_state_unknown"
    RISK_STATE_UNKNOWN = "risk_state_unknown"
    BROKER_UNHEALTHY = "broker_unhealthy"
    BROKER_DISCONNECTED = "broker_disconnected"
    RECONCILIATION_FAILED = "reconciliation_failed"
    RECONCILIATION_UNRESOLVED = "reconciliation_unresolved"
    DUPLICATE_ORDER = "duplicate_order"
    INSTRUMENT_UNRESOLVED = "instrument_unresolved"
    ACCOUNT_STATE_UNKNOWN = "account_state_unknown"
    SESSION_NOT_OPEN = "session_not_open"
    CYCLE_ALREADY_RUNNING = "cycle_already_running"
    CONFIGURATION_CHANGED = "configuration_changed"

    @property
    def needs_a_person(self) -> bool:
        """Blocks that will not clear on their own."""
        return self in (BlockReason.KILL_SWITCH,
                        BlockReason.MODE_NOT_PERMITTED,
                        BlockReason.RECONCILIATION_UNRESOLVED,
                        BlockReason.CONFIGURATION_CHANGED)


@dataclass(frozen=True)
class Block:
    """One refusal, with enough detail to act on."""
    reason: BlockReason
    detail: str = ""
    subject: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"reason": self.reason.value, "detail": self.detail,
                "subject": self.subject}


# ======================================================================
# Signal eligibility (§14)
# ======================================================================

class EligibilityCode(str, Enum):
    """
    Why a signal did or did not become a candidate for a trade.

    ELIGIBLE is the only member that permits a decision. Spec §14 is
    explicit that no signal may be silently discarded, so every signal
    the loop sees gets exactly one of these written to
    `signal_eligibility`, including the ones that passed.
    """
    ELIGIBLE = "eligible"

    NOT_ACTIVE = "not_active"
    NO_DIRECTION = "no_direction"
    EXPIRED = "expired"
    NOT_YET_VALID = "not_yet_valid"
    STALE = "stale"
    SUPPRESSED = "suppressed"
    INSTRUMENT_UNSUPPORTED = "instrument_unsupported"
    INSTRUMENT_UNRESOLVED = "instrument_unresolved"
    NO_PRICE = "no_price"
    STALE_PRICE = "stale_price"
    MODEL_NOT_DEPLOYABLE = "model_not_deployable"
    STRATEGY_DISABLED = "strategy_disabled"
    BELOW_CONFIDENCE_POLICY = "below_confidence_policy"
    BELOW_STRENGTH_POLICY = "below_strength_policy"
    CONFLICTING_OPEN_ORDER = "conflicting_open_order"
    DUPLICATE_INTENT = "duplicate_intent"
    INSTRUMENT_PAUSED = "instrument_paused"

    @property
    def is_eligible(self) -> bool:
        return self is EligibilityCode.ELIGIBLE


@dataclass
class SignalEligibility:
    """
    One signal, one verdict, one reason -- recorded either way.

    `checks_performed` is here for the same reason Phase 14's
    ValidationResult carries it: an eligibility that ran two checks and
    one that ran twelve both read "eligible", and the difference is the
    whole question of how much the verdict is worth.
    """
    cycle_id: str
    signal_id: str
    instrument_id: str
    code: EligibilityCode
    detail: str = ""
    checks_performed: int = 0
    evaluated_at: Optional[datetime] = None
    method_version: str = LOOP_METHOD_VERSION
    #: Model governance context, carried so §37 is answerable per signal.
    trained_model_id: Optional[str] = None
    model_status: Optional[str] = None
    strategy_id: Optional[str] = None
    experimental: bool = False

    def __post_init__(self):
        require_utc(self.evaluated_at, "evaluated_at")

    @property
    def is_eligible(self) -> bool:
        return self.code.is_eligible

    def as_dict(self) -> Dict[str, Any]:
        return {"cycle_id": self.cycle_id, "signal_id": self.signal_id,
                "instrument_id": self.instrument_id, "code": self.code.value,
                "detail": self.detail, "checks_performed": self.checks_performed,
                "experimental": self.experimental,
                "model_status": self.model_status}


# ======================================================================
# Target vs actual (§5, §7, §16)
# ======================================================================

class PositionOrigin(str, Enum):
    """
    Where a position number came from. Never inferred.

    BROKER_RECONCILED is the only origin that may be reported as a
    holding. LOCAL_PROJECTION is what we believe our fills add up to,
    and it is kept so the two can be COMPARED -- which is the entire
    point of reconciliation.
    """
    BROKER_RECONCILED = "broker_reconciled"
    LOCAL_PROJECTION = "local_projection"
    UNKNOWN = "unknown"


@dataclass
class TargetPosition:
    """
    What the portfolio layer says we should hold.

    An intention. It is never a holding, and nothing in this module
    lets one become the other without passing through a broker.
    """
    cycle_id: str
    instrument_id: str
    target_quantity: Optional[float] = None
    target_weight: Optional[float] = None
    reference_price: Optional[float] = None
    signal_id: Optional[str] = None
    decision_id: Optional[str] = None
    portfolio_id: Optional[str] = None
    reason: str = ""
    decided_at: Optional[datetime] = None

    def __post_init__(self):
        require_utc(self.decided_at, "decided_at")
        self.target_quantity = finite_or_none(self.target_quantity)
        self.target_weight = finite_or_none(self.target_weight)
        self.reference_price = finite_or_none(self.reference_price)

    @property
    def notional(self) -> Optional[float]:
        if self.target_quantity is None or self.reference_price is None:
            return None
        return abs(self.target_quantity) * self.reference_price


@dataclass
class ActualPosition:
    """
    What the broker says we hold.

    `origin` is required to be meaningful: a row whose origin is
    LOCAL_PROJECTION must never be displayed as an actual holding, and
    `is_authoritative` is the predicate every reader should ask.
    """
    cycle_id: str
    instrument_id: str
    quantity: float = 0.0
    average_price: Optional[float] = None
    market_price: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    realized_pnl: Optional[float] = None
    origin: PositionOrigin = PositionOrigin.UNKNOWN
    account_id: str = ""
    broker_id: str = ""
    observed_at: Optional[datetime] = None

    def __post_init__(self):
        require_utc(self.observed_at, "observed_at")
        self.quantity = finite_or_none(self.quantity) or 0.0
        self.average_price = finite_or_none(self.average_price)
        self.market_price = finite_or_none(self.market_price)

    @property
    def is_authoritative(self) -> bool:
        return self.origin is PositionOrigin.BROKER_RECONCILED

    @property
    def market_value(self) -> Optional[float]:
        if self.market_price is None:
            return None
        return finite_or_none(self.quantity * self.market_price)


@dataclass
class PositionDelta:
    """
    The gap between target and actual, and what it implies.

    Spec §16's example: TARGET +100, BROKER +40, and the system must
    recognise the remaining +60 rather than reporting +100 as held.
    `outstanding` is that 60, and `is_satisfied` is False until it is
    closed.
    """
    instrument_id: str
    target_quantity: Optional[float]
    actual_quantity: float
    pending_quantity: float = 0.0
    tolerance: float = 1e-9
    #: The smallest quantity the venue will accept. A gap below it is
    #: not a small trade, it is NO trade -- and calling it actionable
    #: sends the validator a request it will refuse, three layers away
    #: from the arithmetic that produced it. Found exactly that way: a
    #: 10% target against a held position produced a SELL of 0.1255
    #: shares, rejected for QUANTITY_INCREMENT.
    min_quantity: float = 0.0

    @property
    def outstanding(self) -> Optional[float]:
        """
        Signed quantity still to be traded, after pending orders.

        None when there is no target -- which is not zero. "We have no
        opinion" and "we want exactly what we hold" are different
        states and the caller must be able to tell them apart.
        """
        if self.target_quantity is None:
            return None
        return self.target_quantity - self.actual_quantity - self.pending_quantity

    @property
    def is_satisfied(self) -> bool:
        gap = self.outstanding
        if gap is None:
            return False
        return abs(gap) <= self.tolerance or abs(gap) < self.min_quantity

    @property
    def below_minimum(self) -> bool:
        """A real gap the venue is too coarse to close."""
        gap = self.outstanding
        return (gap is not None and abs(gap) > self.tolerance
                and abs(gap) < self.min_quantity)

    @property
    def is_noop(self) -> bool:
        """No target, or a target already met."""
        return self.target_quantity is None or self.is_satisfied

    @property
    def side(self) -> Optional[str]:
        gap = self.outstanding
        if gap is None or self.is_satisfied:
            return None
        return "buy" if gap > 0 else "sell"

    @property
    def action(self) -> str:
        """
        What this delta does to the book, named (§7).

        Reported rather than derived at each call site, so "reversing"
        cannot be mistaken for "reducing" by an off-by-one in a sign.
        """
        gap = self.outstanding
        if gap is None:
            return "no_target"
        if abs(gap) <= self.tolerance:
            return "noop"
        if self.below_minimum:
            return "below_minimum"
        target = self.target_quantity or 0.0
        current = self.actual_quantity
        if abs(current) <= self.tolerance:
            return "open" if abs(target) > self.tolerance else "noop"
        if abs(target) <= self.tolerance:
            return "close"
        if (current > 0) != (target > 0):
            return "reverse"
        return "increase" if abs(target) > abs(current) else "reduce"

    def as_dict(self) -> Dict[str, Any]:
        return {"instrument_id": self.instrument_id,
                "target_quantity": self.target_quantity,
                "actual_quantity": self.actual_quantity,
                "pending_quantity": self.pending_quantity,
                "outstanding": self.outstanding, "action": self.action,
                "side": self.side, "min_quantity": self.min_quantity}


# ======================================================================
# Canonical account state (§4)
# ======================================================================

class AccountStateSource(str, Enum):
    BROKER = "broker"
    LOCAL = "local"
    UNAVAILABLE = "unavailable"


@dataclass
class CanonicalAccountState:
    """
    Account state, with its provenance attached (§4, §17).

    Every figure a person might read off a dashboard is here exactly
    once, and `source` says whether the broker reported it or we
    computed it. Spec §17: *do not mix estimated and authoritative
    numbers without labelling*, and the label is a required field
    rather than a convention.
    """
    cycle_id: str
    broker_id: str
    account_id: str
    source: AccountStateSource
    observed_at: Optional[datetime] = None
    base_currency: str = "USD"
    cash: Optional[float] = None
    equity: Optional[float] = None
    buying_power: Optional[float] = None
    available_funds: Optional[float] = None
    margin_used: Optional[float] = None
    margin_available: Optional[float] = None
    realized_pnl: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    open_positions: int = 0
    open_orders: int = 0
    pending_orders: int = 0
    connection_state: str = "unknown"
    synchronized_at: Optional[datetime] = None
    detail: str = ""

    def __post_init__(self):
        require_utc(self.observed_at, "observed_at")
        require_utc(self.synchronized_at, "synchronized_at")
        for name in ("cash", "equity", "buying_power", "available_funds",
                     "margin_used", "margin_available", "realized_pnl",
                     "unrealized_pnl"):
            setattr(self, name, finite_or_none(getattr(self, name)))

    @property
    def is_known(self) -> bool:
        """
        Whether this state may be relied on for a trading decision.

        A state assembled from a broker that answered is known. One
        marked UNAVAILABLE is not, and §25 says the loop must not trade
        on it.
        """
        return self.source is AccountStateSource.BROKER and self.equity is not None

    def age_seconds(self, now: datetime) -> Optional[float]:
        if self.observed_at is None:
            return None
        return (require_utc(now, "now") - self.observed_at).total_seconds()

    def as_dict(self) -> Dict[str, Any]:
        return {"broker_id": self.broker_id, "account_id": self.account_id,
                "source": self.source.value, "cash": self.cash,
                "equity": self.equity, "buying_power": self.buying_power,
                "realized_pnl": self.realized_pnl,
                "unrealized_pnl": self.unrealized_pnl,
                "open_positions": self.open_positions,
                "open_orders": self.open_orders,
                "connection_state": self.connection_state,
                "observed_at": (self.observed_at.isoformat()
                                if self.observed_at else None)}


# ======================================================================
# Operational health (§27)
# ======================================================================

class LoopHealth(str, Enum):
    """
    Overall operational state.

    Three values, and the middle one matters: DEGRADED means the loop
    may run but something is worse than it should be, while BLOCKED
    means it must not create orders. Collapsing them would either stop
    trading on a slow heartbeat or trade through a dead broker.
    """
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BLOCKED = "blocked"

    @property
    def allows_new_orders(self) -> bool:
        return self is not LoopHealth.BLOCKED


#: Components §27 requires a reading for. Named here so a component
#: that is never checked is visible as a gap rather than absent.
HEALTH_COMPONENTS = (
    "trading_mode",
    "kill_switch",
    "broker_connection",
    "account_state",
    "market_data",
    "signals",
    "portfolio",
    "risk",
    "execution",
    "reconciliation",
    "database",
    "scheduler",
)


@dataclass
class ComponentReading:
    """One component's contribution to health."""
    component: str
    state: LoopHealth
    detail: str = ""
    observed_at: Optional[datetime] = None
    age_seconds: Optional[float] = None

    def __post_init__(self):
        require_utc(self.observed_at, "observed_at")


@dataclass
class HealthReport:
    """
    Every component, and the overall verdict derived from them.

    `overall` is the WORST reading, not an average. A single blocked
    component blocks the loop; that is what fail-closed means when it
    is expressed as an aggregation rule.
    """
    cycle_id: str
    readings: List[ComponentReading] = field(default_factory=list)
    assessed_at: Optional[datetime] = None

    def __post_init__(self):
        require_utc(self.assessed_at, "assessed_at")

    def add(self, component: str, state: LoopHealth, detail: str = "",
            observed_at: Optional[datetime] = None,
            age_seconds: Optional[float] = None) -> ComponentReading:
        reading = ComponentReading(component=component, state=state,
                                   detail=detail, observed_at=observed_at,
                                   age_seconds=age_seconds)
        self.readings.append(reading)
        return reading

    @property
    def overall(self) -> LoopHealth:
        if any(r.state is LoopHealth.BLOCKED for r in self.readings):
            return LoopHealth.BLOCKED
        if any(r.state is LoopHealth.DEGRADED for r in self.readings):
            return LoopHealth.DEGRADED
        return LoopHealth.HEALTHY

    @property
    def blocking(self) -> List[ComponentReading]:
        return [r for r in self.readings if r.state is LoopHealth.BLOCKED]

    def unmeasured(self) -> List[str]:
        """
        Components §27 names that this report never read.

        The Phase 24 lesson, applied here: an unmeasured component is
        not a passing component, and a health report that silently
        omits one is worse than one that says it could not look.
        """
        seen = {r.component for r in self.readings}
        return [c for c in HEALTH_COMPONENTS if c not in seen]

    def as_dict(self) -> Dict[str, Any]:
        return {"overall": self.overall.value,
                "readings": [{"component": r.component, "state": r.state.value,
                              "detail": r.detail, "age_seconds": r.age_seconds}
                             for r in self.readings],
                "unmeasured": self.unmeasured()}


# ======================================================================
# The cycle (§13)
# ======================================================================

class LoopStage(str, Enum):
    """
    The stages of one cycle, in order (§13).

    Written down as an enum rather than left as the order of statements
    in a function, so that a stage which never ran is a MISSING ROW --
    findable by a query -- instead of an absence nobody notices.
    """
    MODE = "mode"
    HEALTH = "health"
    MARKET_DATA = "market_data"
    SIGNALS = "signals"
    ELIGIBILITY = "eligibility"
    PORTFOLIO = "portfolio"
    RISK = "risk"
    TARGETS = "targets"
    INTENTS = "intents"
    SUBMISSION = "submission"
    BROKER_POLL = "broker_poll"
    FILLS = "fills"
    POSITIONS = "positions"
    RECONCILIATION = "reconciliation"
    PNL = "pnl"
    OUTCOMES = "outcomes"
    PERSIST = "persist"


class StageOutcome(str, Enum):
    """
    What happened to a stage.

    BLOCKED and FAILED are different: blocked is the system correctly
    declining, failed is the system not working. A dashboard that shows
    them the same colour teaches operators to ignore both.
    """
    RAN = "ran"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass
class StageResult:
    """One stage of one cycle."""
    cycle_id: str
    stage: LoopStage
    outcome: StageOutcome
    detail: str = ""
    count: int = 0
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    block: Optional[Block] = None

    def __post_init__(self):
        require_utc(self.started_at, "started_at")
        require_utc(self.finished_at, "finished_at")

    @property
    def duration_ms(self) -> Optional[float]:
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds() * 1000.0

    def as_dict(self) -> Dict[str, Any]:
        return {"stage": self.stage.value, "outcome": self.outcome.value,
                "detail": self.detail, "count": self.count,
                "duration_ms": self.duration_ms,
                "block": self.block.as_dict() if self.block else None}


class CycleStatus(str, Enum):
    """
    Lifecycle of one cycle row.

    CLAIMED exists so that two workers cannot advance the same cycle:
    the claim is an atomic conditional UPDATE, the Phase 23.5 pattern.
    """
    CLAIMED = "claimed"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    ABANDONED = "abandoned"

    @property
    def is_terminal(self) -> bool:
        return self is not CycleStatus.CLAIMED


@dataclass
class LoopTimestamps:
    """
    The nine clocks of §24, kept apart.

    Spec §24: *do not use a single timestamp for everything*. The
    project has held that line since Phase 5 (four ingestion clocks)
    and Phase 14 (six execution clocks); this is the loop's set, and
    the fields are Optional because a cycle that blocked before
    submission genuinely has no submission time -- which is not the
    same as it having happened at the cycle's start.
    """
    event_time: Optional[datetime] = None
    observed_at: Optional[datetime] = None
    decision_time: Optional[datetime] = None
    risk_time: Optional[datetime] = None
    submission_time: Optional[datetime] = None
    broker_ack_time: Optional[datetime] = None
    fill_time: Optional[datetime] = None
    position_time: Optional[datetime] = None
    outcome_time: Optional[datetime] = None

    def __post_init__(self):
        for name in ("event_time", "observed_at", "decision_time", "risk_time",
                     "submission_time", "broker_ack_time", "fill_time",
                     "position_time", "outcome_time"):
            require_utc(getattr(self, name), name)

    def out_of_order(self) -> List[str]:
        """
        Pairs whose chronology is impossible.

        Spec §24 calls correct ordering mandatory for later research,
        and an ordering nobody checks is an ordering that drifts. This
        returns the violations rather than raising, because a cycle
        that produced them should be RECORDED with them attached -- a
        raise would discard the evidence.
        """
        order = [("observed_at", self.observed_at),
                 ("decision_time", self.decision_time),
                 ("risk_time", self.risk_time),
                 ("submission_time", self.submission_time),
                 ("broker_ack_time", self.broker_ack_time),
                 ("fill_time", self.fill_time),
                 ("position_time", self.position_time),
                 ("outcome_time", self.outcome_time)]
        present = [(name, value) for name, value in order if value is not None]
        problems: List[str] = []
        for (earlier_name, earlier), (later_name, later) in zip(present, present[1:]):
            if later < earlier:
                problems.append(f"{later_name} precedes {earlier_name}")
        return problems

    def as_dict(self) -> Dict[str, Any]:
        return {name: (value.isoformat() if value else None)
                for name, value in (
                    ("event_time", self.event_time),
                    ("observed_at", self.observed_at),
                    ("decision_time", self.decision_time),
                    ("risk_time", self.risk_time),
                    ("submission_time", self.submission_time),
                    ("broker_ack_time", self.broker_ack_time),
                    ("fill_time", self.fill_time),
                    ("position_time", self.position_time),
                    ("outcome_time", self.outcome_time))}


@dataclass
class CycleResult:
    """
    Everything one cycle did.

    Counts are separate fields rather than a dict because each one is
    a different question: `signals_seen` vs `signals_eligible` is the
    conversion §21 asks for, and `intents_created` vs `orders_submitted`
    is where risk and validation removed work.
    """
    cycle_id: str
    session_id: str
    anchor: datetime
    status: CycleStatus = CycleStatus.CLAIMED
    method_version: str = LOOP_METHOD_VERSION
    mode: TradingMode = TradingMode.OFF
    health: LoopHealth = LoopHealth.BLOCKED

    signals_seen: int = 0
    signals_eligible: int = 0
    targets_set: int = 0
    intents_created: int = 0
    intents_rejected: int = 0
    orders_submitted: int = 0
    orders_rejected: int = 0
    fills_recorded: int = 0
    positions_reconciled: int = 0
    discrepancies: int = 0
    outcomes_recorded: int = 0

    blocks: List[Block] = field(default_factory=list)
    stages: List[StageResult] = field(default_factory=list)
    timestamps: LoopTimestamps = field(default_factory=LoopTimestamps)
    detail: str = ""

    def __post_init__(self):
        require_utc(self.anchor, "anchor")

    @property
    def traded(self) -> bool:
        return self.orders_submitted > 0

    @property
    def blocked(self) -> bool:
        return bool(self.blocks)

    def block(self, reason: BlockReason, detail: str = "",
              subject: str = "") -> Block:
        entry = Block(reason=reason, detail=detail, subject=subject)
        self.blocks.append(entry)
        return entry

    def stage(self, stage: LoopStage) -> Optional[StageResult]:
        for result in self.stages:
            if result.stage is stage:
                return result
        return None

    def stages_not_reached(self) -> List[str]:
        """Which of §13's stages this cycle never got to."""
        seen = {r.stage for r in self.stages}
        return [s.value for s in LoopStage if s not in seen]

    @property
    def signal_to_intent_rate(self) -> Optional[float]:
        if not self.signals_seen:
            return None
        return self.intents_created / self.signals_seen

    def as_dict(self) -> Dict[str, Any]:
        return {
            "cycle_id": self.cycle_id, "session_id": self.session_id,
            "anchor": self.anchor.isoformat(), "status": self.status.value,
            "mode": self.mode.value, "health": self.health.value,
            "signals_seen": self.signals_seen,
            "signals_eligible": self.signals_eligible,
            "targets_set": self.targets_set,
            "intents_created": self.intents_created,
            "intents_rejected": self.intents_rejected,
            "orders_submitted": self.orders_submitted,
            "orders_rejected": self.orders_rejected,
            "fills_recorded": self.fills_recorded,
            "positions_reconciled": self.positions_reconciled,
            "discrepancies": self.discrepancies,
            "outcomes_recorded": self.outcomes_recorded,
            "blocks": [b.as_dict() for b in self.blocks],
            "stages": [s.as_dict() for s in self.stages],
            "timestamps": self.timestamps.as_dict(),
            "stages_not_reached": self.stages_not_reached(),
            "detail": self.detail,
        }


def cycle_id_for(session_id: str, anchor: datetime,
                 method_version: str = LOOP_METHOD_VERSION) -> str:
    """
    Deterministic cycle identity.

    Same session, same anchor, same methodology -> same id. A retried
    workflow therefore CLAIMS a row that already exists rather than
    creating a second one, and the claim tells it whether the work was
    already done.
    """
    require_utc(anchor, "anchor")
    raw = f"{method_version}|{session_id}|{anchor.isoformat()}"
    return "cyc-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ======================================================================
# Lineage (§10, §18)
# ======================================================================

#: The chain spec §10 requires to be traceable, in order. Written as
#: data so the integrity check and the documentation cannot disagree.
LINEAGE_CHAIN = (
    "signal_id",
    "decision_id",
    "intent_id",
    "order_id",
    "fill_id",
    "position_instrument_id",
    "outcome_id",
)

#: The provenance links, reported but NOT required for completeness.
#:
#: They are separate from the spine because a rule-based signal
#: genuinely has no model, and forcing one would make an honest chain
#: read as broken. They are reported because a chain that cannot name
#: its model cannot fully answer "why did this happen" (§26) -- and
#: because Phase 25 shipped with all three permanently empty while its
#: own completeness check, which did not look at them, read TRUE.
PROVENANCE_LINKS = (
    "trained_model_id",
    "model_version",
    "strategy_id",
)


@dataclass
class TradeLineage:
    """
    One trade's provenance, end to end.

    `missing_links` is the honest report: a chain that stops at
    `order_id` because the order is still working is not broken, and a
    chain that skips `decision_id` is. The distinction is `is_broken`
    vs `is_complete`, and the two are separate properties because a
    caller usually wants only one of them.
    """
    cycle_id: str
    signal_id: Optional[str] = None
    decision_id: Optional[str] = None
    intent_id: Optional[str] = None
    order_id: Optional[str] = None
    fill_id: Optional[str] = None
    position_instrument_id: Optional[str] = None
    outcome_id: Optional[str] = None
    trained_model_id: Optional[str] = None
    model_version: Optional[str] = None
    strategy_id: Optional[str] = None
    strategy_version: Optional[str] = None
    challenger_id: Optional[str] = None
    instrument_id: str = ""
    recorded_at: Optional[datetime] = None

    def __post_init__(self):
        require_utc(self.recorded_at, "recorded_at")

    def link(self, name: str) -> Optional[str]:
        return getattr(self, name, None)

    def missing_links(self) -> List[str]:
        """Spine links only -- what `is_complete` is about."""
        return [name for name in LINEAGE_CHAIN if not self.link(name)]

    def missing_provenance(self) -> List[str]:
        """
        Model, model version and strategy, when any of them is absent.

        Reported separately from `missing_links` so the two questions
        stay apart: "can this trade be traced" and "can it be
        explained" have different answers and different fixes.
        """
        return [name for name in PROVENANCE_LINKS if not self.link(name)]

    @property
    def has_provenance(self) -> bool:
        return not self.missing_provenance()

    @property
    def is_complete(self) -> bool:
        return not self.missing_links()

    @property
    def is_broken(self) -> bool:
        """
        A gap BEFORE a link that is present.

        A chain that simply has not reached the end yet is incomplete,
        not broken. A chain with a fill but no decision is broken, and
        that is the one that can never be repaired -- which is why
        Phase 17's intake refuses it at submission.
        """
        present = [bool(self.link(name)) for name in LINEAGE_CHAIN]
        last = -1
        for index, ok in enumerate(present):
            if ok:
                last = index
        return any(not present[i] for i in range(last + 1))

    def as_dict(self) -> Dict[str, Any]:
        return {name: self.link(name) for name in LINEAGE_CHAIN} | {
            "cycle_id": self.cycle_id, "instrument_id": self.instrument_id,
            "trained_model_id": self.trained_model_id,
            "model_version": self.model_version,
            "strategy_id": self.strategy_id,
            "challenger_id": self.challenger_id,
            "complete": self.is_complete, "broken": self.is_broken,
            "missing_provenance": self.missing_provenance()}


# ======================================================================
# Paper validation (§21, §22, §38, §41)
# ======================================================================

class PaperStrategyState(str, Enum):
    """
    The governance ladder of §38.

    Every transition beyond PAPER_EVALUATED needs a person. LIVE_ELIGIBLE
    is a member and unreachable from code: `advance()` refuses it, and
    there is no function anywhere in this package that sets it.
    """
    RESEARCH_CANDIDATE = "research_candidate"
    BACKTEST_VALIDATED = "backtest_validated"
    PAPER_ELIGIBLE = "paper_eligible"
    PAPER_RUNNING = "paper_running"
    PAPER_EVALUATED = "paper_evaluated"
    HUMAN_REVIEW = "human_review"
    LIVE_ELIGIBLE = "live_eligible"

    @property
    def is_beyond_paper(self) -> bool:
        return self is PaperStrategyState.LIVE_ELIGIBLE


#: Which transitions the code may make on its own. HUMAN_REVIEW ->
#: LIVE_ELIGIBLE is deliberately absent from every value here.
AUTOMATIC_TRANSITIONS: Dict[PaperStrategyState, Tuple[PaperStrategyState, ...]] = {
    PaperStrategyState.RESEARCH_CANDIDATE: (PaperStrategyState.BACKTEST_VALIDATED,),
    PaperStrategyState.BACKTEST_VALIDATED: (),
    PaperStrategyState.PAPER_ELIGIBLE: (PaperStrategyState.PAPER_RUNNING,),
    PaperStrategyState.PAPER_RUNNING: (PaperStrategyState.PAPER_EVALUATED,),
    PaperStrategyState.PAPER_EVALUATED: (),
    PaperStrategyState.HUMAN_REVIEW: (),
    PaperStrategyState.LIVE_ELIGIBLE: (),
}


class PromotionRefused(Exception):
    """Raised by any attempt to move a strategy past the paper boundary."""


def assert_transition(current: PaperStrategyState,
                      target: PaperStrategyState) -> None:
    """
    Guard for an automatic state change.

    BACKTEST_VALIDATED -> PAPER_ELIGIBLE and PAPER_EVALUATED ->
    HUMAN_REVIEW are absent from `AUTOMATIC_TRANSITIONS` on purpose:
    both are decisions a person makes, and they are made through the
    reviewed path in `src/trading/validation.py`, which records a named
    reviewer and a reason.
    """
    if target.is_beyond_paper:
        raise PromotionRefused(
            "LIVE_ELIGIBLE cannot be reached. Phase 25 does not promote "
            "anything past paper, and no configuration enables it.")
    allowed = AUTOMATIC_TRANSITIONS.get(current, ())
    if target not in allowed:
        raise PromotionRefused(
            f"{current.value} -> {target.value} is not an automatic "
            f"transition. Allowed: {[s.value for s in allowed] or 'none'}.")


class QualityDimension(str, Enum):
    """
    The seven things §41 forbids collapsing into one score.

    A strategy can predict well and execute badly; it can trade well
    and break operationally. Naming them separately is what lets a
    reader see which.
    """
    MODEL = "model"
    SIGNAL = "signal"
    PORTFOLIO = "portfolio"
    RISK = "risk"
    EXECUTION = "execution"
    STRATEGY = "strategy"
    OPERATIONAL = "operational"


@dataclass
class DimensionReading:
    """
    One quality dimension, measured or explicitly not.

    `measured` is False by default. Phase 24's hardest-won lesson: an
    unmeasured dimension is not a dimension that passed, and the only
    way to keep that true is to make "we did not measure this" the
    default state rather than something a caller has to remember to
    set.
    """
    dimension: QualityDimension
    measured: bool = False
    value: Optional[float] = None
    sample_size: int = 0
    detail: str = ""

    def __post_init__(self):
        self.value = finite_or_none(self.value)
        if self.measured and self.value is None:
            raise ValueError(
                f"{self.dimension.value} is marked measured but carries no "
                f"value -- that combination is how an unmeasured dimension "
                f"gets read as a passing one")


@dataclass
class PaperValidation:
    """
    What paper trading established about one strategy, and what it did not.

    NO TOTAL, NO RANK, NO ORDERING -- the Phase 24 scorecard rule
    (§37, §41), for the same mechanical reason: a sortable column gets
    sorted, and the top of a sorted list of strategies is where the
    noise collects.

    Spec §21 also says plainly that paper performance is evidence, not
    proof. `is_conclusive` therefore requires a minimum number of
    completed trades AND every dimension measured, and returns False
    far more often than it returns True.
    """
    validation_id: str
    strategy_id: str
    strategy_version: str
    session_id: str
    method_version: str = LOOP_METHOD_VERSION
    baseline_id: Optional[str] = None
    baseline_version: Optional[str] = None
    challenger_id: Optional[str] = None
    state: PaperStrategyState = PaperStrategyState.PAPER_ELIGIBLE

    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None

    decisions: int = 0
    orders: int = 0
    fills: int = 0
    rejected_orders: int = 0
    completed_trades: int = 0
    risk_violations: int = 0
    operational_failures: int = 0
    signals_seen: int = 0

    realized_pnl: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    max_drawdown: Optional[float] = None
    turnover: Optional[float] = None
    gross_exposure: Optional[float] = None

    dimensions: List[DimensionReading] = field(default_factory=list)
    configuration_fingerprint: str = ""
    notes: str = ""

    def __post_init__(self):
        require_utc(self.started_at, "started_at")
        require_utc(self.ended_at, "ended_at")

    # -- deliberately absent: __lt__, total, overall, score, rank --

    def reading(self, dimension: QualityDimension) -> Optional[DimensionReading]:
        for entry in self.dimensions:
            if entry.dimension is dimension:
                return entry
        return None

    def unmeasured(self) -> List[str]:
        measured = {r.dimension for r in self.dimensions if r.measured}
        return [d.value for d in QualityDimension if d not in measured]

    @property
    def signal_to_trade_rate(self) -> Optional[float]:
        if not self.signals_seen:
            return None
        return self.orders / self.signals_seen

    @property
    def trade_to_outcome_rate(self) -> Optional[float]:
        if not self.fills:
            return None
        return self.completed_trades / self.fills

    def is_conclusive(self, min_trades: int = 30) -> bool:
        """
        Whether this record can support a claim about the strategy.

        `min_trades` defaults to the same 30 every phase since Phase 9
        has used for an effective sample. Reusing it rather than
        picking a new number keeps one threshold in the project.
        """
        return (self.completed_trades >= min_trades
                and not self.unmeasured())

    def as_dict(self) -> Dict[str, Any]:
        return {
            "validation_id": self.validation_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "session_id": self.session_id, "state": self.state.value,
            "challenger_id": self.challenger_id,
            "baseline_id": self.baseline_id,
            "decisions": self.decisions, "orders": self.orders,
            "fills": self.fills, "rejected_orders": self.rejected_orders,
            "completed_trades": self.completed_trades,
            "risk_violations": self.risk_violations,
            "operational_failures": self.operational_failures,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "max_drawdown": self.max_drawdown, "turnover": self.turnover,
            "signal_to_trade_rate": self.signal_to_trade_rate,
            "trade_to_outcome_rate": self.trade_to_outcome_rate,
            "dimensions": [{"dimension": r.dimension.value,
                            "measured": r.measured, "value": r.value,
                            "sample_size": r.sample_size, "detail": r.detail}
                           for r in self.dimensions],
            "unmeasured": self.unmeasured(),
            "conclusive": self.is_conclusive(),
        }


# ======================================================================
# Configuration snapshots (§28, §29)
# ======================================================================

_FINGERPRINT_STRIP = re.compile(r"\s+")


def configuration_fingerprint(values: Dict[str, Any]) -> str:
    """
    A stable hash of the configuration a session ran under (§29).

    Sorted keys, normalized whitespace, so that a re-serialisation with
    different spacing does not read as a configuration change. A real
    change produces a different fingerprint, and `PaperSessionRecord`
    refuses to continue under one.
    """
    parts = []
    for key in sorted(values):
        raw = values[key]
        text = "" if raw is None else str(raw)
        parts.append(f"{key}={_FINGERPRINT_STRIP.sub(' ', text).strip()}")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:20]


class ConfigurationChanged(Exception):
    """
    Raised when a session is resumed under a configuration it did not start with.

    Spec §29: a session whose configuration silently changed mid-run
    produces results that cannot be interpreted afterwards. Refusing is
    cheaper than discovering it later in a comparison.
    """


@dataclass
class PaperSessionRecord:
    """
    One paper-trading session (§28), with its configuration pinned.

    `mode` is a field and is validated to be PAPER on construction. A
    session that could carry LIVE would make every downstream reader
    responsible for checking, and that is the kind of distributed
    obligation this project has consistently refused.
    """
    session_id: str
    name: str
    mode: TradingMode = TradingMode.PAPER
    method_version: str = LOOP_METHOD_VERSION
    broker_id: str = ""
    account_id: str = ""
    strategy_id: Optional[str] = None
    strategy_version: Optional[str] = None
    challenger_id: Optional[str] = None
    trained_model_id: Optional[str] = None
    model_status: Optional[str] = None
    experimental: bool = False
    constraint_version: str = ""
    feature_set_version: str = ""
    dataset_version: str = ""
    configuration_fingerprint: str = ""
    configuration_json: str = ""
    status: str = "open"
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    cycle_seconds: int = DEFAULT_CYCLE_SECONDS

    def __post_init__(self):
        require_utc(self.started_at, "started_at")
        require_utc(self.ended_at, "ended_at")
        if self.mode is not TradingMode.PAPER:
            raise ValueError(
                f"a paper session cannot run in {self.mode.value.upper()} mode; "
                f"Phase 25 has no other venue")

    @property
    def is_open(self) -> bool:
        return self.status == "open" and self.ended_at is None

    def assert_configuration(self, fingerprint: str) -> None:
        if self.configuration_fingerprint and fingerprint != self.configuration_fingerprint:
            raise ConfigurationChanged(
                f"session {self.session_id} started under configuration "
                f"{self.configuration_fingerprint} and is being resumed under "
                f"{fingerprint}. Results across the change would not be "
                f"comparable (spec §29). Start a new session instead.")

    def as_dict(self) -> Dict[str, Any]:
        return {"session_id": self.session_id, "name": self.name,
                "mode": self.mode.value, "broker_id": self.broker_id,
                "account_id": self.account_id, "status": self.status,
                "strategy_id": self.strategy_id,
                "challenger_id": self.challenger_id,
                "trained_model_id": self.trained_model_id,
                "model_status": self.model_status,
                "experimental": self.experimental,
                "configuration_fingerprint": self.configuration_fingerprint,
                "started_at": (self.started_at.isoformat()
                               if self.started_at else None),
                "ended_at": (self.ended_at.isoformat()
                             if self.ended_at else None)}
