"""
src/domain/enums.py
------------------------
Canonical enumerations for MarketLens's Phase 1 data foundation.

RESPONSIBILITY:
Replace the free-text "category" strings scattered across the existing
codebase (company_registry.py's "stocks"/"bvb"/"crypto",
ticker_registry.py's "etf"/"forex", market_instruments.py's
"index"/"commodity") with a single, explicit, typed source of truth.

MIGRATION NOTE: existing modules keep using their own string literals
unchanged in this phase — nothing reads or writes these enums yet
except the new Phase 1 foundation itself (models, adapters,
repositories). See registry_adapter.py for the string -> enum mapping
used when normalizing today's registries into canonical models.
"""

from enum import Enum


class AssetClass(str, Enum):
    """Mirrors the categories already in use across the existing registries, made explicit and typo-proof."""
    STOCK = "stock"
    BVB = "bvb"
    CRYPTO = "crypto"
    ETF = "etf"
    FOREX = "forex"
    INDEX = "index"
    COMMODITY = "commodity"


class InstrumentType(str, Enum):
    """What kind of tradable instrument a Security is listed as."""
    COMMON_STOCK = "common_stock"
    ETF = "etf"
    CRYPTOCURRENCY = "cryptocurrency"
    FOREX_PAIR = "forex_pair"
    INDEX = "index"
    COMMODITY_FUTURE = "commodity_future"


class SourceType(str, Enum):
    """Broad category of a news source, independent of its credibility tier (see source_credibility.py, unchanged in this phase)."""
    OFFICIAL = "official"
    WIRE_OR_MAJOR_PRESS = "wire_or_major_press"
    SPECIALIZED_OR_AGGREGATOR = "specialized_or_aggregator"
    UNCLASSIFIED = "unclassified"


class EventStatus(str, Enum):
    """Lifecycle status of a canonical Event record. Foundation only in Phase 1 — no code transitions an Event between these yet."""
    DETECTED = "detected"
    CONFIRMED = "confirmed"
    RETRACTED = "retracted"


class EventDirection(str, Enum):
    """Directional implication of an Event, when known. Foundation only — not populated by any Phase 1 code."""
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"
