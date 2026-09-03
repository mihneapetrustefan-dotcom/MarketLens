"""
src/execution/outcomes.py
------------------------------
Trade outcome lineage, post-mortem fields, missed trades and the
execution journal (Phase 16, spec §20, §21, §22, §52, §66, §67, §68).

WHAT THIS IS FOR, AND WHAT IT IS NOT
----------------------------------------
It is the data a future learning system will need. It is **not** a
learning system, and spec §78 forbids building one here.

The distinction matters in the code as well as in the prose:
`TradePostMortem` carries fields like `sizing_correct` and they are
`Optional[bool]` defaulting to `None`. Nothing in this module sets
them. They exist so that a later phase — or a human — can record a
judgement, and so that the absence of a judgement is visible rather
than implied.

WHY A LOSING TRADE IS NOT AN ERROR
--------------------------------------
Spec §68 says so explicitly and it is the easiest thing to get wrong.
A correct prediction, correctly sized, correctly executed, can lose
money; that is what risk means. `classify_errors` therefore records
what can be measured mechanically — was the direction right, was the
fill worse than the decision price, did risk block it — and refuses
to infer intent from P&L. `TradeOutcome.was_profitable` exists and is
deliberately NOT one of the error fields.

MISSED TRADES ARE HALF THE EVIDENCE
---------------------------------------
Spec §22. A system that records only what it did cannot distinguish a
bad signal from a good signal that risk prevented. Both are
`MissedTrade` records with a reason, so a later analysis can ask
whether the risk engine was protecting the portfolio or strangling it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.domain.broker_models import (
    CanonicalOrderSide, ExecutionFill, ExecutionOrder, ExecutionRejectCode,
    finite_or_none,
)


def _require_utc(value: Optional[datetime], name: str) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be in UTC")
    return value


class MissReason(str, Enum):
    """Why a signal did not become a completed trade (spec §22)."""
    RISK_REJECTED = "risk_rejected"
    LIMIT_BLOCKED = "limit_blocked"
    SAFETY_BLOCKED = "safety_blocked"
    BROKER_REJECTED = "broker_rejected"
    VALIDATION_FAILED = "validation_failed"
    MARKET_CLOSED = "market_closed"
    STALE_DATA = "stale_data"
    NO_MAPPING = "no_mapping"
    SIGNAL_EXPIRED = "signal_expired"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_EXPIRED = "order_expired"
    NEVER_FILLED = "never_filled"
    DUPLICATE = "duplicate"

    @property
    def was_prevented(self) -> bool:
        """
        Whether the system stopped it, as opposed to the market.

        The distinction a later analysis most needs: a signal risk
        refused is evidence about the risk engine, while one that
        expired unfilled is evidence about the price.
        """
        return self in (MissReason.RISK_REJECTED, MissReason.LIMIT_BLOCKED,
                        MissReason.SAFETY_BLOCKED, MissReason.VALIDATION_FAILED,
                        MissReason.DUPLICATE)


class ExitReason(str, Enum):
    SIGNAL_EXIT = "signal_exit"
    SIGNAL_EXPIRED = "signal_expired"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    RISK_REDUCTION = "risk_reduction"
    SESSION_CLOSE = "session_close"
    MANUAL = "manual"
    UNKNOWN = "unknown"


@dataclass
class TradeLineage:
    """
    The causal chain (spec §20, §66).

    Every field is an id rather than an object reference, so the chain
    survives a restart, a database dump and six months. That is the
    whole point: a future learning system must be able to reconstruct
    why a trade happened without the process that made it.
    """
    correlation_id: str = ""
    model_id: Optional[str] = None
    model_version: Optional[str] = None
    prediction_id: Optional[str] = None
    feature_version: Optional[str] = None
    signal_id: Optional[str] = None
    signal_version: Optional[str] = None
    strategy_id: Optional[str] = None
    strategy_version: Optional[str] = None
    portfolio_id: Optional[str] = None
    decision_id: Optional[str] = None
    risk_config_version: Optional[str] = None
    intent_id: Optional[str] = None
    order_id: Optional[str] = None
    client_order_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    execution_ids: Tuple[str, ...] = ()
    fill_ids: Tuple[str, ...] = ()
    session_id: Optional[str] = None
    execution_config_version: Optional[str] = None
    code_version: Optional[str] = None

    @property
    def is_complete(self) -> bool:
        """
        Whether the chain can actually be walked end to end.

        Checked rather than assumed: a lineage missing its model or
        signal cannot answer "why did this happen", and a learning
        system fed incomplete chains would learn from the subset that
        happened to be well-instrumented.
        """
        return all([self.correlation_id, self.signal_id, self.strategy_id,
                    self.intent_id, self.order_id])

    @property
    def missing_links(self) -> List[str]:
        links = {
            "correlation_id": self.correlation_id, "model_version": self.model_version,
            "prediction_id": self.prediction_id, "signal_id": self.signal_id,
            "strategy_id": self.strategy_id, "portfolio_id": self.portfolio_id,
            "decision_id": self.decision_id, "intent_id": self.intent_id,
            "order_id": self.order_id, "broker_order_id": self.broker_order_id,
            "session_id": self.session_id,
        }
        return [name for name, value in links.items() if not value]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "correlation_id": self.correlation_id,
            "model_id": self.model_id, "model_version": self.model_version,
            "prediction_id": self.prediction_id,
            "feature_version": self.feature_version,
            "signal_id": self.signal_id, "signal_version": self.signal_version,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "portfolio_id": self.portfolio_id, "decision_id": self.decision_id,
            "risk_config_version": self.risk_config_version,
            "intent_id": self.intent_id, "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "execution_ids": list(self.execution_ids),
            "fill_ids": list(self.fill_ids),
            "session_id": self.session_id,
            "execution_config_version": self.execution_config_version,
            "code_version": self.code_version,
            "complete": self.is_complete,
            "missing": self.missing_links,
        }


@dataclass
class ExecutionQuality:
    """
    How well the order executed (spec §19, §66).

    `decision_price` is what the strategy saw when it decided;
    `submitted_price` is what we sent; `fill_price` is what happened.
    The three differ for different reasons — the first gap is decision
    latency, the second is market impact and spread — and collapsing
    them would make the causes indistinguishable.
    """
    decision_price: Optional[float] = None
    reference_price: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    submitted_price: Optional[float] = None
    fill_price: Optional[float] = None
    side: Optional[CanonicalOrderSide] = None
    commission: float = 0.0
    fees: float = 0.0
    submit_latency_ms: Optional[float] = None
    ack_latency_ms: Optional[float] = None
    fill_latency_ms: Optional[float] = None

    @property
    def spread(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return finite_or_none(self.ask - self.bid)

    @property
    def slippage(self) -> Optional[float]:
        """
        Signed cost against the decision price, in price units.

        Positive means worse than the decision expected, for either
        side — the sign is normalised by direction so a buy and a sell
        are comparable.
        """
        if self.fill_price is None or self.decision_price is None or self.side is None:
            return None
        return finite_or_none(
            (self.fill_price - self.decision_price) * self.side.sign)

    @property
    def slippage_bps(self) -> Optional[float]:
        raw = self.slippage
        if raw is None or not self.decision_price:
            return None
        return finite_or_none(raw / abs(self.decision_price) * 10_000.0)

    @property
    def decision_to_submit_drift(self) -> Optional[float]:
        """How far the market moved between deciding and submitting."""
        if self.submitted_price is None or self.decision_price is None or self.side is None:
            return None
        return finite_or_none(
            (self.submitted_price - self.decision_price) * self.side.sign)

    @property
    def total_cost(self) -> float:
        return self.commission + self.fees

    def as_dict(self) -> Dict[str, Any]:
        return {
            "decision_price": self.decision_price,
            "reference_price": self.reference_price,
            "bid": self.bid, "ask": self.ask, "spread": self.spread,
            "submitted_price": self.submitted_price,
            "fill_price": self.fill_price,
            "slippage": self.slippage, "slippage_bps": self.slippage_bps,
            "decision_to_submit_drift": self.decision_to_submit_drift,
            "commission": self.commission, "fees": self.fees,
            "total_cost": self.total_cost,
            "submit_latency_ms": self.submit_latency_ms,
            "ack_latency_ms": self.ack_latency_ms,
            "fill_latency_ms": self.fill_latency_ms,
        }


@dataclass
class TradePostMortem:
    """
    Judgement fields for a future analysis (spec §21, §68).

    Every field defaults to None and NOTHING in this module sets them.
    They are slots for a judgement a later phase or a human will make,
    and the absence of a judgement must be visible rather than implied
    by a default of False.

    `classify_errors` fills only the ones that can be established
    mechanically, and leaves the rest alone.
    """
    prediction_correct: Optional[bool] = None
    direction_correct: Optional[bool] = None
    signal_correct: Optional[bool] = None
    timing_correct: Optional[bool] = None
    sizing_correct: Optional[bool] = None
    risk_correct: Optional[bool] = None
    execution_correct: Optional[bool] = None
    regime_expected: Optional[str] = None
    regime_actual: Optional[str] = None

    # Structured error slots (spec §68)
    prediction_error: Optional[float] = None
    direction_error: Optional[bool] = None
    timing_error: Optional[float] = None
    sizing_error: Optional[float] = None
    risk_error: Optional[str] = None
    execution_error: Optional[float] = None
    regime_error: Optional[str] = None
    data_error: Optional[str] = None

    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    note: str = ""

    def __post_init__(self):
        self.reviewed_at = _require_utc(self.reviewed_at, "reviewed_at")

    @property
    def is_reviewed(self) -> bool:
        return self.reviewed_by is not None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "prediction_correct": self.prediction_correct,
            "direction_correct": self.direction_correct,
            "signal_correct": self.signal_correct,
            "timing_correct": self.timing_correct,
            "sizing_correct": self.sizing_correct,
            "risk_correct": self.risk_correct,
            "execution_correct": self.execution_correct,
            "regime_expected": self.regime_expected,
            "regime_actual": self.regime_actual,
            "prediction_error": self.prediction_error,
            "direction_error": self.direction_error,
            "timing_error": self.timing_error,
            "sizing_error": self.sizing_error,
            "risk_error": self.risk_error,
            "execution_error": self.execution_error,
            "regime_error": self.regime_error,
            "data_error": self.data_error,
            "reviewed": self.is_reviewed, "reviewed_by": self.reviewed_by,
            "note": self.note,
        }


@dataclass
class TradeOutcome:
    """
    One completed round trip, with everything needed to explain it.

    Queryable by strategy, model, signal, instrument, regime, entry,
    exit, holding period, P&L, slippage, fees, risk and execution
    quality (spec §67) — which is the list a future learning system
    would ask for.
    """
    outcome_id: str
    instrument_id: str
    side: CanonicalOrderSide
    quantity: float
    lineage: TradeLineage = field(default_factory=TradeLineage)
    quality: ExecutionQuality = field(default_factory=ExecutionQuality)
    post_mortem: TradePostMortem = field(default_factory=TradePostMortem)

    entry_at: Optional[datetime] = None
    exit_at: Optional[datetime] = None
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    gross_pnl: Optional[float] = None
    fees: float = 0.0
    exit_reason: ExitReason = ExitReason.UNKNOWN
    market_regime: Optional[str] = None
    event_context: Tuple[str, ...] = ()
    environment: str = "paper"
    is_open: bool = False

    def __post_init__(self):
        for name in ("entry_at", "exit_at"):
            setattr(self, name, _require_utc(getattr(self, name), name))

    @property
    def net_pnl(self) -> Optional[float]:
        if self.gross_pnl is None:
            return None
        return finite_or_none(self.gross_pnl - self.fees)

    @property
    def holding_period(self) -> Optional[timedelta]:
        if self.entry_at is None or self.exit_at is None:
            return None
        return self.exit_at - self.entry_at

    @property
    def holding_days(self) -> Optional[float]:
        period = self.holding_period
        return period.total_seconds() / 86_400.0 if period else None

    @property
    def return_pct(self) -> Optional[float]:
        if (self.entry_price is None or self.exit_price is None
                or not self.entry_price):
            return None
        direction = self.side.sign
        return finite_or_none(
            (self.exit_price - self.entry_price) / self.entry_price * direction)

    @property
    def was_profitable(self) -> Optional[bool]:
        """
        Deliberately NOT one of the post-mortem error fields.

        A correct prediction, correctly sized and correctly executed,
        can lose money — that is what risk means. Treating a loss as
        an error is the mistake spec §68 exists to prevent.
        """
        net = self.net_pnl
        return None if net is None else net > 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "outcome_id": self.outcome_id,
            "instrument_id": self.instrument_id,
            "side": self.side.value, "quantity": self.quantity,
            "entry_at": self.entry_at.isoformat() if self.entry_at else None,
            "exit_at": self.exit_at.isoformat() if self.exit_at else None,
            "entry_price": self.entry_price, "exit_price": self.exit_price,
            "gross_pnl": self.gross_pnl, "fees": self.fees,
            "net_pnl": self.net_pnl, "return_pct": self.return_pct,
            "holding_days": self.holding_days,
            "exit_reason": self.exit_reason.value,
            "market_regime": self.market_regime,
            "event_context": list(self.event_context),
            "environment": self.environment, "is_open": self.is_open,
            "profitable": self.was_profitable,
            "lineage": self.lineage.as_dict(),
            "quality": self.quality.as_dict(),
            "post_mortem": self.post_mortem.as_dict(),
        }


@dataclass
class MissedTrade:
    """
    A signal that did not become a trade, and why (spec §22).

    Half the evidence. A system recording only what it did cannot tell
    a bad signal from a good one that risk prevented.
    """
    missed_id: str
    at: Optional[datetime]
    instrument_id: str
    reason: MissReason
    detail: str = ""
    side: Optional[CanonicalOrderSide] = None
    intended_quantity: Optional[float] = None
    reference_price: Optional[float] = None
    reject_code: Optional[ExecutionRejectCode] = None
    lineage: TradeLineage = field(default_factory=TradeLineage)
    session_id: Optional[str] = None
    #: Filled in later, if anyone measures what the price did next.
    forward_return: Optional[float] = None

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")

    @property
    def was_prevented(self) -> bool:
        return self.reason.was_prevented

    def as_dict(self) -> Dict[str, Any]:
        return {
            "missed_id": self.missed_id,
            "at": self.at.isoformat() if self.at else None,
            "instrument_id": self.instrument_id,
            "reason": self.reason.value, "detail": self.detail,
            "side": self.side.value if self.side else None,
            "intended_quantity": self.intended_quantity,
            "reference_price": self.reference_price,
            "reject_code": self.reject_code.value if self.reject_code else None,
            "prevented_by_system": self.was_prevented,
            "forward_return": self.forward_return,
            "session_id": self.session_id,
            "lineage": self.lineage.as_dict(),
        }


def lineage_from_order(order: ExecutionOrder,
                       session_id: Optional[str] = None,
                       fills: Sequence[ExecutionFill] = (),
                       **versions: Any) -> TradeLineage:
    """Build a lineage from an order and its fills."""
    return TradeLineage(
        correlation_id=order.correlation_id,
        model_version=order.model_version,
        prediction_id=order.prediction_id,
        signal_id=order.signal_id,
        strategy_id=order.strategy_id,
        portfolio_id=order.portfolio_id,
        decision_id=order.decision_id,
        intent_id=order.intent_id,
        order_id=order.order_id,
        client_order_id=order.client_order_id,
        broker_order_id=order.broker_order_id,
        execution_ids=tuple(f.execution_id for f in fills if f.execution_id),
        fill_ids=tuple(f.fill_id for f in fills),
        session_id=session_id,
        model_id=versions.get("model_id"),
        feature_version=versions.get("feature_version"),
        signal_version=versions.get("signal_version"),
        strategy_version=versions.get("strategy_version"),
        risk_config_version=versions.get("risk_config_version"),
        execution_config_version=versions.get("execution_config_version"),
        code_version=versions.get("code_version"))


def quality_from_order(order: ExecutionOrder,
                       fills: Sequence[ExecutionFill] = ()) -> ExecutionQuality:
    """Build the execution-quality record from an order and its fills."""
    commission = sum(f.commission for f in fills) or order.commission
    fees = sum(f.fees + f.exchange_fees + f.taxes for f in fills) or order.fees
    fill_latency = None
    if fills and order.submitted_at:
        first = min((f.filled_at for f in fills if f.filled_at), default=None)
        if first is not None:
            delta = (first - order.submitted_at).total_seconds()
            fill_latency = delta * 1000.0 if delta >= 0 else None

    ack_latency = order.execution_latency_seconds
    return ExecutionQuality(
        decision_price=order.decision_price,
        reference_price=order.reference_price,
        bid=order.bid, ask=order.ask,
        submitted_price=order.submitted_price,
        fill_price=order.average_fill_price,
        side=order.side,
        commission=commission, fees=fees,
        ack_latency_ms=ack_latency * 1000.0 if ack_latency is not None else None,
        fill_latency_ms=fill_latency)


def classify_errors(outcome: TradeOutcome,
                    slippage_budget_bps: float = 25.0) -> TradePostMortem:
    """
    Fill only the post-mortem fields that can be established
    mechanically (spec §68).

    Three, and no more:

      direction_correct — did the price move the way the side implied
      execution_correct — was the fill within the slippage budget
      data_error        — was a required input missing

    Everything else needs a judgement about intent, and a function
    that guessed at those would be labelling losing trades as errors,
    which is precisely what §68 forbids.
    """
    post_mortem = outcome.post_mortem

    if outcome.entry_price is not None and outcome.exit_price is not None:
        moved = outcome.return_pct
        if moved is not None:
            post_mortem.direction_correct = moved > 0
            post_mortem.direction_error = moved <= 0

    slippage = outcome.quality.slippage_bps
    if slippage is not None:
        post_mortem.execution_correct = slippage <= slippage_budget_bps
        post_mortem.execution_error = max(0.0, slippage - slippage_budget_bps)

    missing = outcome.lineage.missing_links
    if missing:
        post_mortem.data_error = "incomplete lineage: " + ", ".join(missing)

    return post_mortem


@dataclass
class JournalEntry:
    """One recorded event in the execution journal (spec §52)."""
    entry_id: str
    at: Optional[datetime]
    session_id: Optional[str]
    kind: str
    summary: str
    detail: Dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")


class ExecutionJournal:
    """
    The day's record: orders, fills, rejections, risk blocks,
    reconciliations, alerts, outcomes and misses (spec §52, §85, §86).

    Append-only. There is deliberately no method that edits an entry —
    a journal that could be rewritten is not a journal.
    """

    def __init__(self, session_id: Optional[str] = None):
        self.session_id = session_id
        self.entries: List[JournalEntry] = []
        self.outcomes: List[TradeOutcome] = []
        self.missed: List[MissedTrade] = []

    def record(self, kind: str, summary: str, at: Optional[datetime] = None,
               correlation_id: str = "", **detail: Any) -> JournalEntry:
        entry = JournalEntry(
            entry_id=f"j-{uuid.uuid4().hex[:16]}", at=at,
            session_id=self.session_id, kind=kind, summary=summary,
            detail=detail, correlation_id=correlation_id)
        self.entries.append(entry)
        return entry

    def add_outcome(self, outcome: TradeOutcome) -> TradeOutcome:
        self.outcomes.append(outcome)
        self.record("trade_outcome",
                    f"{outcome.side.value} {outcome.quantity:g} "
                    f"{outcome.instrument_id}",
                    at=outcome.exit_at, correlation_id=outcome.lineage.correlation_id,
                    net_pnl=outcome.net_pnl, outcome_id=outcome.outcome_id)
        return outcome

    def add_missed(self, missed: MissedTrade) -> MissedTrade:
        self.missed.append(missed)
        self.record("missed_trade",
                    f"{missed.instrument_id}: {missed.reason.value}",
                    at=missed.at, correlation_id=missed.lineage.correlation_id,
                    detail=missed.detail, missed_id=missed.missed_id)
        return missed

    def of_kind(self, kind: str) -> List[JournalEntry]:
        return [e for e in self.entries if e.kind == kind]

    # ---------------- reporting ----------------

    def daily_report(self, at: datetime) -> Dict[str, Any]:
        """
        The operator-facing daily summary (spec §85).

        Reports what is known and says `None` for what is not. A daily
        report that filled gaps with zeros would be most confident
        exactly when instrumentation had failed.
        """
        closed = [o for o in self.outcomes if not o.is_open]
        realized = [o.net_pnl for o in closed if o.net_pnl is not None]
        slippages = [o.quality.slippage_bps for o in closed
                     if o.quality.slippage_bps is not None]
        wins = [p for p in realized if p > 0]
        losses = [p for p in realized if p <= 0]
        prevented = [m for m in self.missed if m.was_prevented]

        return {
            "at": at.isoformat(),
            "session_id": self.session_id,
            "trades_closed": len(closed),
            "trades_open": sum(1 for o in self.outcomes if o.is_open),
            "net_pnl": sum(realized) if realized else None,
            "fees": sum(o.fees for o in closed) if closed else 0.0,
            "wins": len(wins), "losses": len(losses),
            "win_rate": (len(wins) / len(realized)) if realized else None,
            "average_win": (sum(wins) / len(wins)) if wins else None,
            "average_loss": (sum(losses) / len(losses)) if losses else None,
            "profit_factor": (
                sum(wins) / abs(sum(losses)) if losses and sum(losses) else None),
            "median_slippage_bps": (
                sorted(slippages)[len(slippages) // 2] if slippages else None),
            "missed": len(self.missed),
            "missed_prevented_by_system": len(prevented),
            "missed_by_reason": {
                reason.value: sum(1 for m in self.missed if m.reason is reason)
                for reason in MissReason
                if any(m.reason is reason for m in self.missed)},
            "journal_entries": len(self.entries),
            "lineage_complete": sum(1 for o in self.outcomes
                                    if o.lineage.is_complete),
            "lineage_incomplete": sum(1 for o in self.outcomes
                                      if not o.lineage.is_complete),
            "caveat": ("a day is not a sample; these figures describe what "
                       "happened and establish nothing about the strategy"),
        }
