"""
src/domain/impact_models.py
--------------------------------
Market Impact Intelligence models (Phase 6).

THE ONE SENTENCE THAT GOVERNS THIS FILE (spec §32): Phase 6 measures
WHAT HAPPENED, never what will happen. There is deliberately no field
anywhere here that could hold a prediction, a signal, or a
recommendation — the absence is structural, so a later phase must add
its own type rather than quietly widening this one.

THE SECOND GOVERNING RULE (spec §6, §8, §20): an abnormal return is an
OBSERVED ASSOCIATION, not a demonstrated cause. Nothing in this module
is named `caused_by`, `effect`, or `attribution`. A 6% move on a day
the Fed surprised the market is recorded as a 6% move with a
confounding event attached — never as "the event caused 6%".
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from enum import Enum
from typing import Optional, List, Dict, Any


def _require_utc(value: Optional[datetime], name: str) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (got a naive datetime)")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be in UTC (got offset {value.utcoffset()})")
    return value


# ============================================================
# Windows
# ============================================================

class WindowKind(str, Enum):
    PRE_EVENT = "pre_event"
    EVENT = "event"
    POST_EVENT = "post_event"


class WindowUnit(str, Enum):
    """
    MINUTES is wall-clock; TRADING_DAYS counts sessions, skipping
    weekends and holidays. The distinction is not cosmetic: "+5 days"
    measured in wall-clock across a weekend silently shortens the
    actual market exposure by two sessions.
    """
    MINUTES = "minutes"
    TRADING_DAYS = "trading_days"


@dataclass(frozen=True)
class EventWindow:
    """
    One measurement window relative to the event's market-visibility
    moment.

    Offsets are signed: negative is before the event, positive after.
    Windows are DATA, declared in a list (see DEFAULT_WINDOWS), never
    hardcoded into calculation logic — adding "+30 trading days" must
    be a list entry, not a code change (spec §2).
    """
    name: str
    kind: WindowKind
    unit: WindowUnit
    start_offset: float
    end_offset: float

    def __post_init__(self):
        if self.end_offset < self.start_offset:
            raise ValueError(f"window '{self.name}': end_offset must not precede start_offset")

    @property
    def is_forward_looking(self) -> bool:
        """Whether this window extends past the event — i.e. measures OUTCOME rather than prior information."""
        return self.end_offset > 0


#: A starting set, not an exhaustive one. Callers may pass their own.
DEFAULT_WINDOWS: List[EventWindow] = [
    EventWindow("pre_60m", WindowKind.PRE_EVENT, WindowUnit.MINUTES, -60, 0),
    EventWindow("pre_30m", WindowKind.PRE_EVENT, WindowUnit.MINUTES, -30, 0),
    EventWindow("pre_5m", WindowKind.PRE_EVENT, WindowUnit.MINUTES, -5, 0),
    EventWindow("intraday_5m", WindowKind.POST_EVENT, WindowUnit.MINUTES, 0, 5),
    EventWindow("intraday_15m", WindowKind.POST_EVENT, WindowUnit.MINUTES, 0, 15),
    EventWindow("intraday_30m", WindowKind.POST_EVENT, WindowUnit.MINUTES, 0, 30),
    EventWindow("intraday_60m", WindowKind.POST_EVENT, WindowUnit.MINUTES, 0, 60),
    EventWindow("d1", WindowKind.POST_EVENT, WindowUnit.TRADING_DAYS, 0, 1),
    EventWindow("d3", WindowKind.POST_EVENT, WindowUnit.TRADING_DAYS, 0, 3),
    EventWindow("d5", WindowKind.POST_EVENT, WindowUnit.TRADING_DAYS, 0, 5),
    EventWindow("d10", WindowKind.POST_EVENT, WindowUnit.TRADING_DAYS, 0, 10),
    EventWindow("d20", WindowKind.POST_EVENT, WindowUnit.TRADING_DAYS, 0, 20),
]


# ============================================================
# Data quality
# ============================================================

class DataQualityIssue(str, Enum):
    """Spec §26 — every reason a calculation might be untrustworthy, named explicitly."""
    MISSING_CANDLES = "missing_candles"
    STALE_PRICES = "stale_prices"
    INSUFFICIENT_HISTORY = "insufficient_history"
    TRADING_HALT = "trading_halt"
    MARKET_CLOSED = "market_closed"
    UNADJUSTED_CORPORATE_ACTION = "unadjusted_corporate_action"
    SYMBOL_CHANGE = "symbol_change"
    BAD_TIMESTAMP = "bad_timestamp"
    NO_BENCHMARK_DATA = "no_benchmark_data"
    UNCERTAIN_EVENT_TIME = "uncertain_event_time"


class DataQualityLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNUSABLE = "unusable"


@dataclass
class DataQuality:
    """
    Quality assessment attached to every calculation (spec §26).

    `is_usable` is a GATE, not a label. The Phase 0 audit's lesson
    applies here: a result computed from incomplete data that is merely
    flagged will still be averaged into a statistic by someone
    downstream. UNUSABLE results must be excluded, not annotated.
    """
    level: DataQualityLevel = DataQualityLevel.HIGH
    issues: List[DataQualityIssue] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    observations_available: int = 0
    observations_expected: int = 0

    def add_issue(self, issue: DataQualityIssue, note: str = "") -> None:
        if issue not in self.issues:
            self.issues.append(issue)
        if note:
            self.notes.append(note)
        self._recompute_level()

    def _recompute_level(self) -> None:
        blocking = {
            DataQualityIssue.INSUFFICIENT_HISTORY,
            DataQualityIssue.NO_BENCHMARK_DATA,
            DataQualityIssue.BAD_TIMESTAMP,
        }
        if any(i in blocking for i in self.issues):
            self.level = DataQualityLevel.UNUSABLE
        elif len(self.issues) >= 2:
            self.level = DataQualityLevel.LOW
        elif self.issues:
            self.level = DataQualityLevel.MEDIUM
        else:
            self.level = DataQualityLevel.HIGH

    @property
    def is_usable(self) -> bool:
        return self.level != DataQualityLevel.UNUSABLE

    @property
    def completeness(self) -> Optional[float]:
        if not self.observations_expected:
            return None
        return round(self.observations_available / self.observations_expected, 4)


# ============================================================
# Returns & reactions
# ============================================================

class ReturnMethod(str, Enum):
    """
    SIMPLE is (p1-p0)/p0. LOG is ln(p1/p0).

    Both are provided because they answer different questions — log
    returns aggregate additively over time, simple returns are what a
    position actually earns — and mixing them across components is a
    classic silent error (spec §7). Every stored return records which
    was used.
    """
    SIMPLE = "simple"
    LOG = "log"


class BenchmarkModel(str, Enum):
    """
    How expected return is estimated (spec §8, §11).

    MARKET_ADJUSTED subtracts the benchmark return outright (beta
    assumed 1). MARKET_MODEL would estimate beta from an estimation
    window. Phase 6 implements MARKET_ADJUSTED and declares the others
    as unimplemented rather than pretending — an unestimated beta
    silently assumed to be 1.0 is a methodological claim, and it should
    be a visible one.
    """
    MARKET_ADJUSTED = "market_adjusted"
    MEAN_ADJUSTED = "mean_adjusted"
    MARKET_MODEL = "market_model"          # not implemented in Phase 6
    PEER_RELATIVE = "peer_relative"


@dataclass
class ReturnMeasurement:
    """One return calculation, with the inputs that produced it kept alongside for auditability."""
    window_name: str
    method: ReturnMethod
    price_before: Optional[Decimal] = None
    price_after: Optional[Decimal] = None
    raw_return: Optional[float] = None
    benchmark_id: Optional[str] = None
    benchmark_return: Optional[float] = None
    expected_return: Optional[float] = None
    abnormal_return: Optional[float] = None
    benchmark_model: Optional[BenchmarkModel] = None
    quality: DataQuality = field(default_factory=DataQuality)

    @property
    def has_abnormal(self) -> bool:
        return self.abnormal_return is not None


@dataclass
class VolumeReaction:
    """
    Volume response, always NORMALIZED (spec §11) — raw share counts
    are meaningless across securities, so only relative volume and
    z-score are exposed as comparable quantities.
    """
    window_name: str
    event_volume: Optional[float] = None
    baseline_mean_volume: Optional[float] = None
    baseline_std_volume: Optional[float] = None
    relative_volume: Optional[float] = None     # event / baseline mean
    volume_zscore: Optional[float] = None
    quality: DataQuality = field(default_factory=DataQuality)


@dataclass
class VolatilityReaction:
    """Volatility response, measured as realized volatility before vs after."""
    window_name: str
    pre_volatility: Optional[float] = None
    post_volatility: Optional[float] = None
    volatility_change_pct: Optional[float] = None
    quality: DataQuality = field(default_factory=DataQuality)


@dataclass
class GapAnalysis:
    """
    Overnight / non-session behaviour (spec §13). Relevant precisely
    because financial events cluster OUTSIDE trading hours — earnings
    after the close, macro releases before the open.
    """
    occurred_during_session: bool = True
    previous_close: Optional[Decimal] = None
    next_open: Optional[Decimal] = None
    gap_return: Optional[float] = None          # previous close -> next open
    intraday_followthrough: Optional[float] = None   # next open -> that session's close


# ============================================================
# Context
# ============================================================

@dataclass
class SectorContext:
    """Sector environment around the event — lets a company-specific move be told apart from a sector-wide one (spec §15)."""
    sector_id: Optional[str] = None
    sector_return_pre: Optional[float] = None
    sector_return_post: Optional[float] = None
    sector_volatility: Optional[float] = None


@dataclass
class PeerContext:
    """
    Peer comparison (spec §16).

    `peers_are_defined` records whether these are DECLARED peers or a
    sector-membership fallback — the spec warns against treating every
    company in a sector as a peer, so which one it is must be visible
    in the data, not assumed by the reader.
    """
    peer_instrument_ids: List[str] = field(default_factory=list)
    peers_are_defined: bool = False
    peer_mean_return: Optional[float] = None
    peer_median_return: Optional[float] = None
    peer_dispersion: Optional[float] = None
    relative_to_peers: Optional[float] = None


@dataclass
class MacroContext:
    """Macro backdrop at the time (spec §17), sourced from FRED observations available AT the event, never later revisions."""
    observations: Dict[str, float] = field(default_factory=dict)
    as_of: Optional[datetime] = None

    def __post_init__(self):
        self.as_of = _require_utc(self.as_of, "as_of")


class RegimeTrend(str, Enum):
    UPTREND = "uptrend"
    DOWNTREND = "downtrend"
    RANGEBOUND = "rangebound"
    UNKNOWN = "unknown"


class RegimeVolatility(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    ELEVATED = "elevated"
    UNKNOWN = "unknown"


@dataclass
class MarketRegime:
    """
    Market context (spec §14).

    Explicitly NOT "bull = price up". Trend is classified from the
    benchmark's position relative to its own trailing mean, and
    volatility from a percentile of its trailing realized volatility —
    both computed only from data available at the event, and both
    reporting UNKNOWN rather than guessing when history is short.
    """
    trend: RegimeTrend = RegimeTrend.UNKNOWN
    volatility_regime: RegimeVolatility = RegimeVolatility.UNKNOWN
    benchmark_id: Optional[str] = None
    trailing_return: Optional[float] = None
    trailing_volatility: Optional[float] = None
    volatility_percentile: Optional[float] = None
    method: str = ""


@dataclass
class ConfoundingEvent:
    """
    Another catalyst overlapping the event's window (spec §18).

    Its presence does NOT invalidate the measurement — it invalidates
    the ATTRIBUTION. The reaction is still recorded; what changes is
    that no share of it may be assigned to this event.
    """
    event_id: Optional[str] = None
    description: str = ""
    kind: str = ""                  # "macro" | "sector" | "company" | "geopolitical"
    occurred_at: Optional[datetime] = None
    severity: str = "unknown"       # "high" | "medium" | "low" | "unknown"

    def __post_init__(self):
        self.occurred_at = _require_utc(self.occurred_at, "occurred_at")


# ============================================================
# Impact dimensions & profile
# ============================================================

class ImpactDirection(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    MIXED = "mixed"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class ImpactMagnitude(str, Enum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    UNKNOWN = "unknown"


class ImpactSpeed(str, Enum):
    IMMEDIATE = "immediate"
    DELAYED = "delayed"
    UNKNOWN = "unknown"


class ImpactDuration(str, Enum):
    SHORT = "short"
    MEDIUM = "medium"
    PERSISTENT = "persistent"
    UNKNOWN = "unknown"


class ImpactBreadth(str, Enum):
    COMPANY = "company"
    SECTOR = "sector"
    MARKET = "market"
    UNKNOWN = "unknown"


class ImpactLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


@dataclass
class ImpactDimensions:
    """
    Impact expressed across independent axes (spec §19) — deliberately
    NOT collapsed into one number, because a large-but-brief
    company-specific move and a small-but-persistent sector-wide one
    are different phenomena that a single score would render
    indistinguishable.
    """
    direction: ImpactDirection = ImpactDirection.UNKNOWN
    magnitude: ImpactMagnitude = ImpactMagnitude.UNKNOWN
    speed: ImpactSpeed = ImpactSpeed.UNKNOWN
    duration: ImpactDuration = ImpactDuration.UNKNOWN
    breadth: ImpactBreadth = ImpactBreadth.UNKNOWN
    volatility_impact: ImpactLevel = ImpactLevel.UNKNOWN
    volume_impact: ImpactLevel = ImpactLevel.UNKNOWN
    measurement_confidence: float = 0.0


@dataclass
class EventStudy:
    """
    One event measured against one instrument (spec §10).

    Holds the four timestamps separately, plus the crucial fifth —
    `market_visibility_earliest/latest`, the range in which the
    information plausibly became public. That range, not the
    publication timestamp, is what anchors the windows.
    """
    study_id: str
    event_id: str
    instrument_id: str
    benchmark_id: Optional[str] = None

    event_time: Optional[datetime] = None
    publication_time: Optional[datetime] = None
    ingestion_time: Optional[datetime] = None
    market_visibility_earliest: Optional[datetime] = None
    market_visibility_latest: Optional[datetime] = None
    visibility_basis: str = ""

    returns: Dict[str, ReturnMeasurement] = field(default_factory=dict)
    volume: Dict[str, VolumeReaction] = field(default_factory=dict)
    volatility: Dict[str, VolatilityReaction] = field(default_factory=dict)
    gap: Optional[GapAnalysis] = None

    sector_context: Optional[SectorContext] = None
    peer_context: Optional[PeerContext] = None
    macro_context: Optional[MacroContext] = None
    market_regime: Optional[MarketRegime] = None
    confounding_events: List[ConfoundingEvent] = field(default_factory=list)

    quality: DataQuality = field(default_factory=DataQuality)
    computed_at: Optional[datetime] = None
    is_direct: bool = True            # spec §6: direct vs indirect reaction

    def __post_init__(self):
        for name in ("event_time", "publication_time", "ingestion_time",
                      "market_visibility_earliest", "market_visibility_latest", "computed_at"):
            setattr(self, name, _require_utc(getattr(self, name), name))

    @property
    def has_confounders(self) -> bool:
        return bool(self.confounding_events)

    @property
    def attribution_permitted(self) -> bool:
        """
        Whether ANY share of the observed move may be attributed to
        this event. False whenever a confounder overlaps — the
        measurement stands, the attribution does not (spec §18).
        """
        return not self.has_confounders and self.quality.is_usable

    def abnormal_return(self, window_name: str) -> Optional[float]:
        measurement = self.returns.get(window_name)
        return measurement.abnormal_return if measurement else None


@dataclass
class ImpactProfile:
    """
    The summary view of one event's market impact (spec §25).

    `event_confidence` (how sure we are the EVENT is correctly
    represented — from Phase 5) is carried separately from every impact
    field, because they are unrelated questions (spec §14 of Phase 5,
    §25 here): a perfectly-evidenced event can have no market impact,
    and a dubious one can coincide with a huge move.
    """
    profile_id: str
    event_id: str
    primary_instrument_id: Optional[str] = None
    dimensions: ImpactDimensions = field(default_factory=ImpactDimensions)
    impact_score: Optional[float] = None
    event_confidence: Optional[float] = None
    study_ids: List[str] = field(default_factory=list)
    comparable_event_count: int = 0
    quality: DataQuality = field(default_factory=DataQuality)
    computed_at: Optional[datetime] = None
    methodology_note: str = ""

    def __post_init__(self):
        self.computed_at = _require_utc(self.computed_at, "computed_at")


@dataclass
class ReactionDistribution:
    """
    Descriptive statistics over historical reactions to comparable
    events (spec §21, §24).

    STRICTLY DESCRIPTIVE. `small_sample` is set from `sample_size` and
    must be surfaced wherever these numbers are shown: a median drawn
    from four observations is not evidence, and presenting it beside
    one drawn from four hundred without that flag is misleading.
    """
    window_name: str
    sample_size: int = 0
    mean: Optional[float] = None
    median: Optional[float] = None
    std_dev: Optional[float] = None
    p25: Optional[float] = None
    p75: Optional[float] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    probability_above: Dict[str, float] = field(default_factory=dict)
    probability_below: Dict[str, float] = field(default_factory=dict)
    small_sample: bool = True

    #: Below this, results are descriptive colour, not evidence.
    MIN_MEANINGFUL_SAMPLE = 30
