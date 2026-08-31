"""
src/domain/portfolio_models.py
-----------------------------------
Portfolio and risk domain models (Phase 11).

WHAT THIS LAYER IS FOR
--------------------------
Phase 10 produces Signals: claims about instruments, carrying no
quantity and no action. Phase 11 is the layer that decides whether a
claim may become EXPOSURE, and how much — subject to what the
portfolio already holds and what the risk configuration allows.

The five concepts below are deliberately different types, because
collapsing any two of them removes a place where the system is
supposed to be able to say no:

    Signal              "NVDA looks likely to rise"      (Phase 10)
    Position            "we hold 40 shares of NVDA"      (a fact)
    AllocationProposal  "take NVDA from 12% to 18%"      (a request)
    RiskDecision        "no — sector cap would break"    (a judgement)
    OrderIntent         "buy 22 shares, if ever executed" (an instruction)

A single object carrying all five would make the risk judgement
optional, and an optional risk layer is one that eventually gets
skipped. That is the same structural argument Phase 9 made about
predictions and Phase 10 made about signals, extended one more layer.

NOTHING HERE TOUCHES A BROKER
---------------------------------
There is no account number, no credential, no venue, no order id, no
fill, no execution callback. OrderIntent is the last object this phase
produces and it is inert by construction: it describes what WOULD be
instructed, and no code in this phase can send it anywhere. Execution
belongs to a later phase, behind an explicit boundary.

MISSING DATA IS A STATE, NEVER A ZERO
-----------------------------------------
Every quantity that can be unknown is Optional and defaults to None.
A position whose price cannot be found is not worth 0 — it is
UNVALUED, and a portfolio containing one cannot honestly report its
equity. That distinction is carried in ValuationStatus rather than
hidden, because a risk engine that treats missing data as zero risk is
worse than one that refuses to answer.

FLOATS, AND WHY
-------------------
Money is float here, consistent with every other numeric column in
this database (SQLite REAL). This is a research and analytics system,
not a ledger: no cash is moved, no balance is reconciled to the cent,
and introducing Decimal at this layer alone would only create
conversion boundaries with the price cache and the schema. If a future
phase settles real cash, that phase should revisit this — it is a
deliberate, documented choice, not an oversight.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from typing import Any, Dict, List, Optional


# ============================================================
# Guards
# ============================================================

def _require_utc(value: Optional[datetime], name: str) -> Optional[datetime]:
    """Same contract as the Phase 9/10 models: timestamps are UTC-aware or absent."""
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (got a naive datetime)")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be in UTC (got offset {value.utcoffset()})")
    return value


def finite_or_none(value: Optional[float]) -> Optional[float]:
    """
    Collapse NaN and +/-Infinity to None.

    Spec §51 is explicit that the system must never present NaN or
    Infinity as a valid risk number. The cheapest way to guarantee that
    is to funnel every computed metric through one place that turns a
    non-finite result back into "unknown" — which the rest of the
    system already knows how to handle, because None means unknown
    everywhere in this codebase.
    """
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if isfinite(numeric) else None


def safe_ratio(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    """
    Divide, or return None.

    Spec §8: "do not silently divide by zero". A weight computed
    against zero equity is not 0.0 and not infinity — it is undefined,
    and saying so is the only honest answer.
    """
    if numerator is None or denominator is None:
        return None
    if denominator == 0:
        return None
    return finite_or_none(numerator / denominator)


# ============================================================
# Taxonomy
# ============================================================

class PositionStatus(str, Enum):
    """Lifecycle of a position. CLOSED rows are kept — history is evidence."""
    OPEN = "open"
    CLOSED = "closed"


class PositionSource(str, Enum):
    """
    Where a position came from.

    This exists so a simulated holding can never be silently presented
    as a real one. There is deliberately no BROKER member: this phase
    cannot produce broker-sourced positions, and an enum value the
    system cannot generate is a promise it cannot keep.
    """
    DECLARED = "declared"        # entered by hand / config
    PAPER = "paper"              # produced by a future paper-trading layer
    SIMULATED = "simulated"      # produced by a backtest or replay


class ExposureDimension(str, Enum):
    """The axes exposure can be broken down along, given the data that exists."""
    INSTRUMENT = "instrument"
    SECTOR = "sector"
    ASSET_CLASS = "asset_class"
    CURRENCY = "currency"


class ValuationStatus(str, Enum):
    """
    Whether a position could be valued at the anchor, and if not, why.

    STALE_PRICE is separate from MISSING_PRICE on purpose: "we have a
    price but it is three weeks old" and "we have never had a price"
    call for different responses, and merging them would hide which one
    is happening.
    """
    VALUED = "valued"
    MISSING_PRICE = "missing_price"
    STALE_PRICE = "stale_price"


class ConstraintScope(str, Enum):
    """What a constraint measures. One scope per rule, so a breach names its own axis."""
    POSITION_WEIGHT = "position_weight"
    SECTOR_WEIGHT = "sector_weight"
    ASSET_CLASS_WEIGHT = "asset_class_weight"
    GROSS_EXPOSURE = "gross_exposure"
    NET_EXPOSURE = "net_exposure"
    LEVERAGE = "leverage"
    POSITION_COUNT = "position_count"
    CONCENTRATION_HHI = "concentration_hhi"
    PORTFOLIO_VOLATILITY = "portfolio_volatility"
    DRAWDOWN = "drawdown"
    MIN_SIGNAL_CONFIDENCE = "min_signal_confidence"
    MIN_LIQUIDITY = "min_liquidity"


class ConstraintSeverity(str, Enum):
    """
    HARD may never be violated; SOFT may be, visibly and on the record.

    A soft breach does not silently pass: it still produces a
    RiskViolation and downgrades the decision to REQUIRES_REVIEW. The
    difference is that a hard breach REJECTS outright, while a soft one
    escalates to a human. Without this distinction every threshold
    becomes a hard stop, and in practice that gets solved by quietly
    loosening the thresholds — which is worse.
    """
    HARD = "hard"
    SOFT = "soft"


class RiskDecisionState(str, Enum):
    """
    The outcome of a risk evaluation.

    INSUFFICIENT_DATA is not a failure mode of the code — it is a
    legitimate verdict, and per spec §56 it is what the engine returns
    whenever it cannot establish that a proposal is safe. Approval must
    be earned; it is never the fallback.
    """
    APPROVED = "approved"
    REDUCED = "reduced"                    # allowed, but at a smaller size
    REJECTED = "rejected"
    REQUIRES_REVIEW = "requires_review"     # soft breach, or a judgement call
    INSUFFICIENT_DATA = "insufficient_data"


class TradingState(str, Enum):
    """
    Global safety switch (spec §39).

    Deliberately NOT wired to any broker — nothing in this phase can
    execute, so this gate currently governs whether the risk engine will
    approve an INCREASE in exposure at all. It exists now so that the
    later execution phase inherits a switch that was designed in, rather
    than bolted on once orders are already flowing.
    """
    ENABLED = "enabled"
    PAUSED = "paused"
    REDUCE_ONLY = "reduce_only"
    EMERGENCY_STOP = "emergency_stop"


# ============================================================
# Positions
# ============================================================

@dataclass
class Position:
    """
    A holding. A FACT about what is held — not a view about what should
    be held.

    Sign convention: `quantity` is negative for a short. One signed
    field rather than a quantity plus a separate direction flag, because
    two fields that must agree eventually disagree — the same reasoning
    Phase 10 applied to probability_up/probability_down.
    """
    position_id: str
    portfolio_id: str
    instrument_id: str
    quantity: float
    average_entry_price: Optional[float] = None
    currency: str = "USD"
    status: PositionStatus = PositionStatus.OPEN
    source: PositionSource = PositionSource.DECLARED
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    realized_pnl: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        for name in ("opened_at", "closed_at"):
            setattr(self, name, _require_utc(getattr(self, name), name))
        if self.quantity is None or not isfinite(float(self.quantity)):
            raise ValueError(f"position quantity must be a finite number (got {self.quantity})")

    @property
    def is_short(self) -> bool:
        return self.quantity < 0

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def cost_basis(self) -> Optional[float]:
        """Signed cost of the position, or None when no entry price was recorded."""
        if self.average_entry_price is None:
            return None
        return finite_or_none(self.quantity * self.average_entry_price)


@dataclass
class PositionValuation:
    """
    A position priced at one moment — a DERIVED view, never stored as
    the position itself.

    Kept separate because a position's market value is not intrinsic to
    it: it depends on the anchor time and on whether a price could be
    found at all. Storing value on the Position would force a single
    answer for a question whose answer changes with `as_of`, which is
    exactly what breaks historical replay.
    """
    position: Position
    as_of: datetime
    price: Optional[float] = None
    price_timestamp: Optional[datetime] = None
    status: ValuationStatus = ValuationStatus.MISSING_PRICE
    #: Age of the price used, in days, when one was found.
    price_age_days: Optional[float] = None

    def __post_init__(self):
        _require_utc(self.as_of, "as_of")
        _require_utc(self.price_timestamp, "price_timestamp")

    @property
    def is_valued(self) -> bool:
        return self.status == ValuationStatus.VALUED and self.price is not None

    @property
    def market_value(self) -> Optional[float]:
        """Signed market value: negative for a short. None when unpriced."""
        if self.price is None:
            return None
        return finite_or_none(self.position.quantity * self.price)

    @property
    def exposure(self) -> Optional[float]:
        """
        Absolute market value — how much of the market this position
        touches, regardless of direction.

        Gross exposure sums THIS, not market_value, which is why a short
        adds to gross risk instead of cancelling it out.
        """
        value = self.market_value
        return None if value is None else abs(value)

    @property
    def unrealized_pnl(self) -> Optional[float]:
        """
        Mark-to-market gain/loss. Correct for shorts by construction:
        (price - entry) * quantity is negative when a short moves up.
        """
        if self.price is None or self.position.average_entry_price is None:
            return None
        return finite_or_none(
            (self.price - self.position.average_entry_price) * self.position.quantity)


# ============================================================
# Exposure
# ============================================================

@dataclass
class ExposureBucket:
    """One slice of exposure — a sector, an asset class, a currency, an instrument."""
    key: str
    label: str
    exposure: float = 0.0
    long_exposure: float = 0.0
    short_exposure: float = 0.0
    position_count: int = 0
    weight: Optional[float] = None       # exposure / equity; None when equity is unusable

    @property
    def net_exposure(self) -> float:
        return self.long_exposure - self.short_exposure


@dataclass
class ExposureBreakdown:
    """
    Exposure along one dimension, plus what could not be classified.

    `unclassified_exposure` is a first-class field rather than a
    silently-dropped remainder: 150 of this project's 389 instruments
    have no cached price, and a sector breakdown that quietly omitted
    them would look complete while describing only part of the book.
    """
    dimension: ExposureDimension
    buckets: List[ExposureBucket] = field(default_factory=list)
    unclassified_exposure: float = 0.0
    unclassified_count: int = 0

    def bucket_for(self, key: str) -> Optional[ExposureBucket]:
        return next((b for b in self.buckets if b.key == key), None)

    @property
    def total_exposure(self) -> float:
        return sum(b.exposure for b in self.buckets) + self.unclassified_exposure

    @property
    def is_complete(self) -> bool:
        """True when every position was classifiable along this dimension."""
        return self.unclassified_count == 0


# ============================================================
# Risk metrics
# ============================================================

@dataclass
class ConcentrationMetrics:
    """
    How much of the portfolio sits in how few places.

    HHI (Herfindahl-Hirschman Index) is the sum of squared weights on
    the 0..1 scale. It is computed on ABSOLUTE exposure weights, so a
    long and an equal short both count as concentration rather than
    cancelling — a book that is 50% long NVDA and 50% short AMD is
    concentrated in semiconductors, not diversified.

    TWO MEASURES, BECAUSE CASH MAKES THEM DIFFERENT QUESTIONS
    -------------------------------------------------------------
    `hhi` is measured against EQUITY, so holding cash genuinely lowers
    it — which is correct for a risk limit, since an uninvested book
    really is less exposed.

    `effective_positions` is 1/HHI measured against the INVESTED
    portion only. The reciprocal of HHI is the standard "effective
    number of holdings", but it is only interpretable when the weights
    it squares sum to 1. Computed against equity on a book that is 46%
    cash, it reports 6.5 effective positions for a portfolio holding
    two — arithmetically true and practically nonsense. Normalizing
    first keeps the headline number meaning what its name says, and
    `invested_weight` shows how much of equity is deployed so the two
    can be reconciled.
    """
    largest_weight: Optional[float] = None
    largest_instrument_id: Optional[str] = None
    top_5_weight: Optional[float] = None
    top_10_weight: Optional[float] = None
    #: Sum of squared weights over EQUITY. What the constraint checks.
    hhi: Optional[float] = None
    #: 1/HHI over the INVESTED portion. Never exceeds position_count.
    effective_positions: Optional[float] = None
    #: Share of equity actually deployed into positions.
    invested_weight: Optional[float] = None
    position_count: int = 0


@dataclass
class VolatilityEstimate:
    """
    A measured volatility, with the method that produced it attached.

    Spec §11 requires the methodology to be stated rather than implied.
    A number labelled "volatility" with no lookback, frequency or
    annualization convention is not reproducible and cannot be compared
    to anything.
    """
    value: Optional[float] = None                # annualized, as a fraction (0.24 = 24%)
    method: str = "historical"
    lookback_days: Optional[int] = None
    observations: Optional[int] = None
    return_frequency: str = "daily"
    annualization_factor: Optional[float] = None
    insufficient_data: bool = False
    note: str = ""


@dataclass
class DrawdownMetrics:
    """Peak-to-trough decline of an equity curve. Requires history; absent without it."""
    current_drawdown: Optional[float] = None      # negative fraction, e.g. -0.12
    max_drawdown: Optional[float] = None
    peak_equity: Optional[float] = None
    peak_at: Optional[datetime] = None
    trough_equity: Optional[float] = None
    trough_at: Optional[datetime] = None
    observations: int = 0
    insufficient_data: bool = False

    def __post_init__(self):
        for name in ("peak_at", "trough_at"):
            setattr(self, name, _require_utc(getattr(self, name), name))


@dataclass
class ValueAtRisk:
    """
    An ESTIMATE of loss at a confidence level over a horizon — not a
    worst case and not a guarantee (spec §13).

    Historical method only. Parametric VaR would require assuming a
    return distribution this project has not tested for, and Monte
    Carlo would require a generating model it does not have; either
    would produce a more precise-looking number backed by less
    evidence.
    """
    value: Optional[float] = None                 # positive fraction of equity at risk
    expected_shortfall: Optional[float] = None    # mean loss beyond the VaR threshold
    confidence_level: float = 0.95
    horizon_days: int = 1
    method: str = "historical"
    observations: Optional[int] = None
    insufficient_data: bool = False
    note: str = ""


@dataclass
class CorrelationSummary:
    """
    Portfolio-level correlation, summarized rather than dumped.

    The full matrix is available separately; what a risk decision needs
    is the aggregate picture plus the specific pairs that are dangerous.
    `insufficient_pairs` records how many pairs could not be computed at
    all, so a reassuring average over three of forty pairs is visibly
    thin rather than quietly convincing.
    """
    average_correlation: Optional[float] = None
    max_correlation: Optional[float] = None
    max_pair: Optional[tuple] = None
    highly_correlated_pairs: List[tuple] = field(default_factory=list)
    computed_pairs: int = 0
    insufficient_pairs: int = 0
    min_observations_used: Optional[int] = None


@dataclass
class RiskMetrics:
    """Everything measured about a portfolio at one anchor."""
    as_of: Optional[datetime] = None
    volatility: VolatilityEstimate = field(default_factory=VolatilityEstimate)
    drawdown: DrawdownMetrics = field(default_factory=DrawdownMetrics)
    value_at_risk: ValueAtRisk = field(default_factory=ValueAtRisk)
    concentration: ConcentrationMetrics = field(default_factory=ConcentrationMetrics)
    correlation: CorrelationSummary = field(default_factory=CorrelationSummary)
    #: Metrics that could not be computed, and why — surfaced rather
    #: than left as a silent None the reader has to interpret.
    unavailable: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        _require_utc(self.as_of, "as_of")

    def mark_unavailable(self, metric: str, reason: str) -> None:
        self.unavailable[metric] = reason


# ============================================================
# Snapshot
# ============================================================

@dataclass
class PortfolioSnapshot:
    """
    The state of a portfolio at one moment — reproducible from stored
    inputs, never from live data.

    `is_complete` is the field that keeps this honest. A snapshot in
    which some position could not be priced is still produced (hiding it
    would be worse), but it is flagged, and the risk engine refuses to
    approve against an incomplete snapshot.
    """
    portfolio_id: str
    as_of: datetime
    base_currency: str = "USD"
    cash: float = 0.0

    #: Sum of absolute market values of priced positions.
    gross_exposure: float = 0.0
    long_exposure: float = 0.0
    short_exposure: float = 0.0

    unrealized_pnl: Optional[float] = None
    realized_pnl: Optional[float] = None

    #: Positions that carry a usable price — VALUED or STALE_PRICE.
    #: A stale price is still a price: dropping it would understate
    #: equity, which inflates every weight computed against it. It is
    #: counted here and flagged separately via `has_stale_prices`.
    valuations: List[PositionValuation] = field(default_factory=list)
    #: Positions with no price at all at the anchor. These contribute
    #: nothing and make the snapshot incomplete.
    unvalued_positions: List[PositionValuation] = field(default_factory=list)

    #: Currencies seen across positions. More than one means the sums
    #: below mix units and must not be read as a single figure.
    currencies: List[str] = field(default_factory=list)

    def __post_init__(self):
        _require_utc(self.as_of, "as_of")

    @property
    def net_exposure(self) -> float:
        return self.long_exposure - self.short_exposure

    @property
    def positions_value(self) -> float:
        """Signed market value of all priced positions (shorts subtract)."""
        return self.net_exposure

    @property
    def equity(self) -> float:
        """
        Cash plus the signed value of priced positions.

        This is the denominator for every weight in the system, which is
        why `is_complete` matters so much: an equity figure computed
        while some position is unpriced is too small, making every
        weight too large, making the book look more concentrated than it
        is. The engine checks completeness before trusting it.
        """
        return self.cash + self.positions_value

    @property
    def leverage(self) -> Optional[float]:
        """Gross exposure over equity. None at zero or negative equity, where it is undefined."""
        if self.equity <= 0:
            return None
        return safe_ratio(self.gross_exposure, self.equity)

    @property
    def net_leverage(self) -> Optional[float]:
        if self.equity <= 0:
            return None
        return safe_ratio(self.net_exposure, self.equity)

    @property
    def is_complete(self) -> bool:
        """True when every open position could be priced at the anchor."""
        return not self.unvalued_positions

    @property
    def stale_valuations(self) -> List[PositionValuation]:
        return [v for v in self.valuations if v.status == ValuationStatus.STALE_PRICE]

    @property
    def has_stale_prices(self) -> bool:
        """
        True when some position was priced only from an old candle.

        Kept separate from `is_complete` because the two call for
        different responses: a missing price means the portfolio cannot
        be measured at all, while a stale price means it was measured
        from information that may no longer hold. Both block approval
        (spec §40), but conflating them would hide which is happening.
        """
        return any(v.status == ValuationStatus.STALE_PRICE for v in self.valuations)

    @property
    def is_empty(self) -> bool:
        return not self.valuations and not self.unvalued_positions

    @property
    def is_multi_currency(self) -> bool:
        """
        True when positions span more than one currency.

        No FX conversion happens anywhere in this phase (spec §32: no
        invented rates), so a multi-currency portfolio's totals are
        sums of mixed units. The flag exists so that fact is visible
        instead of silently wrong.
        """
        return len(set(self.currencies)) > 1

    def weight_of(self, instrument_id: str) -> Optional[float]:
        """Absolute-exposure weight of one instrument, or None when equity is unusable."""
        if self.equity <= 0:
            return None
        total = sum(v.exposure or 0.0
                    for v in self.valuations
                    if v.position.instrument_id == instrument_id)
        return safe_ratio(total, self.equity)


# ============================================================
# Constraints and violations
# ============================================================

@dataclass
class RiskConstraint:
    """
    One versioned limit.

    Bounds are expressed as a maximum and/or a minimum so a single type
    covers "no position above 20%" and "confidence at least 0.4"
    without a second concept. `applies_to` narrows a rule to one key
    (a sector id, an asset class) — absent, the rule is portfolio-wide.
    """
    constraint_id: str
    scope: ConstraintScope
    severity: ConstraintSeverity = ConstraintSeverity.HARD
    max_value: Optional[float] = None
    min_value: Optional[float] = None
    applies_to: Optional[str] = None
    description: str = ""
    enabled: bool = True

    def __post_init__(self):
        if self.max_value is None and self.min_value is None:
            raise ValueError(
                f"constraint {self.constraint_id} sets neither max_value nor min_value")
        if (self.max_value is not None and self.min_value is not None
                and self.min_value > self.max_value):
            raise ValueError(
                f"constraint {self.constraint_id} has min_value above max_value")

    def evaluate(self, observed: Optional[float]) -> Optional[str]:
        """
        Check one measurement. Returns a breach description, or None
        when the constraint holds.

        An observed value of None returns None — "not measured" is NOT
        "not breached", and the caller must handle the gap. The engine
        does exactly that: an unmeasurable constraint contributes an
        INSUFFICIENT_DATA outcome rather than a silent pass, which is
        why this method must not guess on its own.
        """
        if observed is None:
            return None
        value = finite_or_none(observed)
        if value is None:
            return None
        if self.max_value is not None and value > self.max_value:
            return f"{value:.4f} exceeds maximum {self.max_value:.4f}"
        if self.min_value is not None and value < self.min_value:
            return f"{value:.4f} is below minimum {self.min_value:.4f}"
        return None


@dataclass
class ConstraintSet:
    """
    A versioned collection of constraints.

    The version is recorded on every RiskDecision. Changing a threshold
    therefore does not retroactively reinterpret past decisions: an old
    decision still names the configuration that produced it, and a
    replay under that version reproduces it (spec §42, §45).
    """
    version: str = "v1"
    name: str = "default"
    trading_state: TradingState = TradingState.ENABLED
    constraints: List[RiskConstraint] = field(default_factory=list)

    def enabled_constraints(self) -> List[RiskConstraint]:
        return [c for c in self.constraints if c.enabled]

    def by_scope(self, scope: ConstraintScope) -> List[RiskConstraint]:
        return [c for c in self.enabled_constraints() if c.scope == scope]

    def first(self, scope: ConstraintScope, applies_to: Optional[str] = None
              ) -> Optional[RiskConstraint]:
        """
        The most specific enabled constraint for a scope.

        A rule naming this exact key wins over a portfolio-wide rule of
        the same scope, so "technology may reach 45%" can override a
        generic 40% sector cap without deleting it.
        """
        specific = [c for c in self.by_scope(scope) if c.applies_to == applies_to]
        if specific:
            return specific[0]
        general = [c for c in self.by_scope(scope) if c.applies_to is None]
        return general[0] if general else None


@dataclass
class RiskViolation:
    """
    One breached constraint, with the numbers that produced it.

    Current, proposed and limit are all carried because an explanation
    that says only "sector limit breached" cannot be checked. Spec §22
    requires the arithmetic to be visible: 38% -> 46% against a 40%
    cap is a statement a reader can verify.
    """
    constraint_id: str
    scope: ConstraintScope
    severity: ConstraintSeverity
    message: str
    observed_value: Optional[float] = None
    current_value: Optional[float] = None
    limit_value: Optional[float] = None
    applies_to: Optional[str] = None
    #: True when the engine already resolved this breach — by trimming
    #: the offending change back inside the limit. The violation is
    #: still recorded, because "how often does the position cap bind?"
    #: is exactly the question these rows exist to answer, but a breach
    #: that has been fixed must not also reject the proposal it was
    #: fixed in.
    remediated: bool = False

    @property
    def is_hard(self) -> bool:
        return self.severity == ConstraintSeverity.HARD

    @property
    def is_blocking(self) -> bool:
        """A hard breach that was NOT remediated — the only kind that rejects."""
        return self.is_hard and not self.remediated


# ============================================================
# Proposals and decisions
# ============================================================

@dataclass
class AllocationChange:
    """One instrument's move from current weight to target weight."""
    instrument_id: str
    current_weight: Optional[float] = None
    target_weight: Optional[float] = None
    current_quantity: Optional[float] = None
    target_quantity: Optional[float] = None
    signal_id: Optional[str] = None
    reason: str = ""

    @property
    def weight_delta(self) -> Optional[float]:
        if self.current_weight is None or self.target_weight is None:
            return None
        return finite_or_none(self.target_weight - self.current_weight)

    @property
    def is_increase(self) -> bool:
        delta = self.weight_delta
        return delta is not None and delta > 0

    @property
    def is_reduction(self) -> bool:
        delta = self.weight_delta
        return delta is not None and delta < 0


@dataclass
class AllocationProposal:
    """
    A REQUEST to change exposure. Not an order, and not yet permitted.

    Produced by a sizing strategy from signals plus portfolio context;
    consumed by the risk engine, which is the only thing that can
    approve it.
    """
    proposal_id: str
    portfolio_id: str
    as_of: datetime
    changes: List[AllocationChange] = field(default_factory=list)
    sizing_strategy_id: str = ""
    sizing_version: str = "v1"
    source_signal_ids: List[str] = field(default_factory=list)
    note: str = ""

    def __post_init__(self):
        _require_utc(self.as_of, "as_of")

    @property
    def increases(self) -> List[AllocationChange]:
        return [c for c in self.changes if c.is_increase]

    @property
    def is_empty(self) -> bool:
        return not self.changes


@dataclass
class RiskProvenance:
    """
    Everything needed to reproduce a risk decision (spec §42).

    Without these versions a decision is an opinion with no date on it.
    With them, a replay can assert that the same inputs under the same
    configuration still produce the same verdict.
    """
    risk_engine_version: str = "v1"
    constraint_set_version: str = "v1"
    sizing_version: Optional[str] = None
    portfolio_snapshot_as_of: Optional[datetime] = None
    information_cutoff: Optional[datetime] = None
    price_data_as_of: Optional[datetime] = None
    inputs: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        for name in ("portfolio_snapshot_as_of", "information_cutoff", "price_data_as_of"):
            setattr(self, name, _require_utc(getattr(self, name), name))


@dataclass
class RiskDecision:
    """
    The verdict, with its reasoning attached.

    Every non-approval carries at least one violation or an explicit
    reason string — a rejection nobody can explain is indistinguishable
    from a bug. `evaluated_scopes` records which checks actually ran, so
    an approval that skipped half the waterfall for want of data is
    visible as such rather than reading like a clean pass.
    """
    decision_id: str
    portfolio_id: str
    state: RiskDecisionState
    as_of: datetime
    proposal_id: Optional[str] = None
    violations: List[RiskViolation] = field(default_factory=list)
    #: Populated for REDUCED: the scaled-back change set that would pass.
    approved_changes: List[AllocationChange] = field(default_factory=list)
    summary: str = ""
    reasons: List[str] = field(default_factory=list)
    evaluated_scopes: List[str] = field(default_factory=list)
    skipped_scopes: Dict[str, str] = field(default_factory=dict)
    provenance: RiskProvenance = field(default_factory=RiskProvenance)
    metrics: Optional[RiskMetrics] = None

    def __post_init__(self):
        _require_utc(self.as_of, "as_of")

    @property
    def is_approved(self) -> bool:
        """Only APPROVED and REDUCED permit exposure to change."""
        return self.state in (RiskDecisionState.APPROVED, RiskDecisionState.REDUCED)

    @property
    def hard_violations(self) -> List[RiskViolation]:
        """Every hard breach, remediated or not — the full record."""
        return [v for v in self.violations if v.is_hard]

    @property
    def blocking_violations(self) -> List[RiskViolation]:
        """Hard breaches that still stand. These, and only these, reject."""
        return [v for v in self.violations if v.is_blocking]

    @property
    def soft_violations(self) -> List[RiskViolation]:
        return [v for v in self.violations if not v.is_hard]

    def add_violation(self, violation: RiskViolation) -> None:
        self.violations.append(violation)

    def add_reason(self, reason: str) -> None:
        if reason and reason not in self.reasons:
            self.reasons.append(reason)


# ============================================================
# The execution boundary
# ============================================================

@dataclass
class OrderIntent:
    """
    What WOULD be instructed, if an execution layer existed (spec §36).

    THIS IS NOT AN ORDER. It has no venue, no account, no broker
    reference, no order id, no time-in-force and no submission method,
    and nothing in this phase can transmit it. It exists so the later
    execution phase inherits a well-formed hand-off shape instead of
    inventing one against whatever the risk engine happened to return.

    An intent may only be constructed from an approved decision —
    `require_approval` enforces that at the boundary rather than
    trusting every future caller to remember.
    """
    intent_id: str
    portfolio_id: str
    instrument_id: str
    side: str                                  # "buy" | "sell"
    target_weight: Optional[float] = None
    target_quantity: Optional[float] = None
    source_signal_id: Optional[str] = None
    decision_id: Optional[str] = None
    reason: str = ""
    created_at: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    #: Always False in Phase 11. No execution path exists.
    is_executable: bool = False

    def __post_init__(self):
        for name in ("created_at", "valid_until"):
            setattr(self, name, _require_utc(getattr(self, name), name))
        if self.side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell' (got {self.side!r})")

    @staticmethod
    def require_approval(decision: RiskDecision) -> None:
        """Raise unless the decision permits exposure to change."""
        if not decision.is_approved:
            raise ValueError(
                f"cannot build an order intent from a {decision.state.value} decision "
                f"({decision.decision_id})")
