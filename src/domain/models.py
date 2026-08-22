"""
src/domain/models.py
-------------------------
Canonical domain models for MarketLens's Phase 1 data foundation.

RESPONSIBILITY:
Give every concept the existing application already works with
(companies, sectors, tickers, articles, sources, events, market data)
one explicit, typed, provider-agnostic shape — instead of the untyped
dicts every module currently passes around.

WHAT THIS PHASE DELIBERATELY DOES NOT DO:
- No AI/sentiment/scoring logic lives here — these are pure data
  containers with light structural validation only.
- Company/Security/Instrument are kept LOGICALLY DISTINCT (per the
  brief's own example: NVIDIA Corporation the Company, NVIDIA common
  stock the Security, NASDAQ:NVDA the Instrument) — a ticker is never
  used as a universal identity. This directly fixes a real, documented
  bug class in the existing system (company_registry.py's own
  docstring flags "EL" colliding between Electrica/BVB and Estee
  Lauder/NYSE precisely because ticker was being treated as unique).
- Financial figures (prices, monetary values) use `Decimal`, not
  `float` — per the phase's explicit "financial precision" requirement.
  The EXISTING pipeline (market_data.py, backtest_engine.py, etc.)
  keeps using floats unchanged in this phase; conversion to Decimal
  happens only at the boundary where a provider adapter builds a
  canonical MarketObservation (see providers/registry_adapter.py and,
  in a future phase, live-provider adapters for market_data.py itself).
- Every timestamp field is REQUIRED to be timezone-aware UTC —
  enforced in __post_init__, not just documented — since ambiguous
  timestamps were flagged as a real risk in the Phase 0 audit.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List

from .enums import AssetClass, InstrumentType, SourceType, EventStatus, EventDirection


def _require_utc(value: Optional[datetime], field_name: str) -> Optional[datetime]:
    """
    Validate that a timestamp is timezone-aware and in UTC. Raises
    ValueError rather than silently guessing a timezone — per the
    Phase 0 audit finding that naive/ambiguous timestamps are a real,
    already-observed risk in the existing codebase.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware (got a naive datetime)")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{field_name} must be in UTC (got offset {value.utcoffset()})")
    return value


@dataclass
class Exchange:
    """A trading venue. Did not exist as an explicit entity before Phase 1 — was only ever an implicit 'category' string."""
    exchange_id: str          # short, stable code, e.g. "NASDAQ", "NYSE", "BVB"
    name: str                 # human-readable, e.g. "Bursa de Valori Bucuresti"
    country: str              # ISO-ish country name or code, e.g. "US", "RO"
    timezone: str = "UTC"     # IANA timezone name for this exchange's local trading hours


@dataclass
class AssetClassInfo:
    """Lightweight descriptor wrapping the AssetClass enum with a display label, for anywhere the UI needs a human-readable name."""
    asset_class: AssetClass
    display_label_ro: str


@dataclass
class Industry:
    """Optional sub-classification beneath Sector. Not populated by any existing registry today — foundation only."""
    industry_id: str
    name: str
    sector_id: str


@dataclass
class Sector:
    """A broad economic sector. Maps 1:1 onto sector_registry.py's existing sector names."""
    sector_id: str
    name: str


@dataclass
class Company:
    """
    A legal company entity — independent of any specific ticker or
    exchange listing. Maps onto one entry of company_registry.py's
    COMPANY_REGISTRY, but WITHOUT that entry's ticker (tickers now
    belong to Security/Instrument, never to Company directly).
    """
    company_id: str            # stable internal id, derived from canonical_name (see registry_adapter.py)
    canonical_name: str        # exactly company_registry.py's canonical_name — the migration join key
    aliases: List[str] = field(default_factory=list)
    sector_id: Optional[str] = None


@dataclass
class Security:
    """
    One tradable claim on a Company (e.g. "NVIDIA common stock"). A
    Company may have zero or more Securities — e.g. dual-listed shares
    — which the OLD company_registry.py model could not represent
    (one row = one company = one ticker, conflated).
    """
    security_id: str
    company_id: Optional[str]     # None for securities with no underlying company (indices, commodities, forex, crypto)
    instrument_type: InstrumentType
    currency: str = "USD"


@dataclass
class Instrument:
    """
    A Security as it actually trades on ONE specific Exchange, under
    ONE specific ticker. This is the level at which "ticker" is
    finally meaningful — deliberately NOT unique on its own (the same
    ticker string can and does legitimately belong to two different
    Instruments on two different Exchanges; see the documented
    Electrica/Estee Lauder "EL" collision in company_registry.py,
    which this split is designed to make structurally unambiguous).
    """
    instrument_id: str
    security_id: str
    exchange_id: str
    ticker: str
    asset_class: AssetClass

    def identity_key(self) -> str:
        """The actually-unique identity: (exchange, ticker) — never ticker alone."""
        return f"{self.exchange_id}:{self.ticker}"


@dataclass
class NewsSource:
    """A publisher of news articles. Maps onto sources.py's RSS_FEEDS entries plus source_credibility.py's tier map."""
    source_id: str
    name: str                                   # must match sources.py's "name" / an article's "source" field, for join purposes
    source_type: SourceType = SourceType.UNCLASSIFIED
    url: Optional[str] = None
    active: bool = True


@dataclass
class NewsArticle:
    """
    Canonical shape for a news article. Foundation only in this
    phase — the EXISTING pipeline (news_database.py, pipeline_core.py)
    keeps using its own plain-dict article shape unchanged; nothing in
    Phase 1 migrates real article volume into this model yet (see the
    audit's §8.6 finding and this phase's own "do not implement
    large-scale ingestion yet" instruction).
    """
    article_id: str
    source_id: Optional[str]
    external_id: Optional[str] = None          # provider-specific id, if any
    title: str = ""
    url: Optional[str] = None
    published_at: Optional[datetime] = None     # when the SOURCE says it was published
    ingested_at: Optional[datetime] = None      # when WE first saw it — never equal to published_at by assumption
    language: Optional[str] = None
    fingerprint: Optional[str] = None           # for future duplicate detection (see duplicate_detector.py, unchanged)
    entity_ids: List[str] = field(default_factory=list)   # Company ids this article mentions
    event_ids: List[str] = field(default_factory=list)    # Event ids this article contributes to

    def __post_init__(self):
        self.published_at = _require_utc(self.published_at, "published_at")
        self.ingested_at = _require_utc(self.ingested_at, "ingested_at")


@dataclass
class Event:
    """
    Canonical shape for a fused event (see event_fusion.py). Foundation
    only — the EXISTING EventFusion keeps computing its own dict shape,
    fresh, every run, unchanged in this phase (per the "do not build
    advanced event fusion in this phase" instruction). This model is
    what a future phase will persist EventFusion's output INTO.
    """
    event_id: str
    event_type: str
    detected_at: datetime
    entity_ids: List[str] = field(default_factory=list)
    instrument_ids: List[str] = field(default_factory=list)
    sector_ids: List[str] = field(default_factory=list)
    source_article_ids: List[str] = field(default_factory=list)
    direction: EventDirection = EventDirection.UNKNOWN
    magnitude: Optional[Decimal] = None
    confidence: Optional[Decimal] = None
    status: EventStatus = EventStatus.DETECTED

    def __post_init__(self):
        self.detected_at = _require_utc(self.detected_at, "detected_at")


@dataclass
class EconomicEvent:
    """
    Canonical shape for a scheduled or published macro event. Maps
    conceptually onto economic_calendar.py's FOMC dates (scheduled,
    future) and fred_connector.py's indicator observations (published,
    past) — neither is migrated into this model yet in this phase.
    """
    economic_event_id: str
    name: str
    scheduled_at: Optional[datetime] = None    # for a future/scheduled event (e.g. an FOMC meeting)
    published_at: Optional[datetime] = None    # for an already-published data point (e.g. a FRED observation)
    value: Optional[Decimal] = None
    source_id: Optional[str] = None

    def __post_init__(self):
        self.scheduled_at = _require_utc(self.scheduled_at, "scheduled_at")
        self.published_at = _require_utc(self.published_at, "published_at")


@dataclass
class MarketObservation:
    """
    Canonical shape for one price/volume observation of an Instrument,
    at one timeframe. Foundation only — the existing market_data.py
    keeps fetching and returning its own plain-dict snapshot shape
    unchanged in this phase. Prices use Decimal, per the phase's
    explicit financial-precision requirement.
    """
    instrument_id: str
    observed_at: datetime
    timeframe: str                  # "1m" / "5m" / "15m" / "1h" / "1d" — only "1d" is actually populated anywhere today
    open: Optional[Decimal] = None
    high: Optional[Decimal] = None
    low: Optional[Decimal] = None
    close: Optional[Decimal] = None
    adjusted_close: Optional[Decimal] = None
    volume: Optional[int] = None
    currency: str = "USD"
    source_id: Optional[str] = None
    ingested_at: Optional[datetime] = None

    def __post_init__(self):
        self.observed_at = _require_utc(self.observed_at, "observed_at")
        self.ingested_at = _require_utc(self.ingested_at, "ingested_at")


@dataclass
class CorporateAction:
    """
    Canonical shape for a corporate action (split, dividend, etc.).
    Not produced by any existing module — pure foundation, included
    because the phase's canonical model list requires it, for a future
    phase to populate once dividend/split-adjusted analysis is needed.
    """
    corporate_action_id: str
    security_id: str
    action_type: str            # e.g. "split", "dividend" — free text in this phase, deliberately not an enum yet (too little real usage to know the right set of values)
    effective_date: datetime
    details: Optional[str] = None

    def __post_init__(self):
        self.effective_date = _require_utc(self.effective_date, "effective_date")
