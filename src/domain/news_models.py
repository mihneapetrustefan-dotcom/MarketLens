"""
src/domain/news_models.py
------------------------------
Phase 2 canonical news models.

RESPONSIBILITY:
Separate the two representations Phase 2 requires:

    Provider Article -> RawArticle -> Normalizer -> NormalizedArticle

RawArticle preserves what the provider actually sent (so an ingestion
result can be inspected/reproduced later without re-querying the
provider). NormalizedArticle is the internal canonical shape the rest
of the application uses — no provider-specific field ever appears on it.

RELATIONSHIP TO PHASE 1: domain/models.py already defines a minimal
`NewsArticle` as pure foundation (never populated, per Phase 1's
scope). Phase 2's NormalizedArticle is that concept, now built out to
production shape with the fields this phase's spec requires. The
Phase 1 NewsArticle is left in place, unchanged, so nothing importing
it breaks; new code should use NormalizedArticle.

FIELD DISCIPLINE: every field below exists for a stated reason —
either the spec required it, an existing feature needs it, or
deduplication/look-ahead-bias prevention depends on it. Fields that
merely "sound useful" were deliberately left out.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, List, Dict, Any


def _require_utc(value: Optional[datetime], field_name: str) -> Optional[datetime]:
    """Same UTC enforcement as Phase 1's domain/models.py — naive or non-UTC timestamps are rejected, never silently guessed."""
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware (got a naive datetime)")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{field_name} must be in UTC (got offset {value.utcoffset()})")
    return value


class ProcessingStatus(str, Enum):
    """
    Where an article is in the pipeline. Makes a failed or incomplete
    article RECOVERABLE — a crash mid-pipeline leaves the article at
    its last completed stage, so a later run can resume it rather than
    silently losing or re-processing it.

    Stages beyond ENTITY_LINKED/READY (EVENT_EXTRACTED, EVENT_FUSED,
    IMPACT_ANALYZED) are deliberately NOT included — per this phase's
    "do not implement future stages yet" instruction. They are added
    when the stage that produces them is actually built.
    """
    INGESTED = "ingested"
    NORMALIZED = "normalized"
    DEDUPLICATED = "deduplicated"
    ENTITY_LINKED = "entity_linked"
    READY = "ready"
    REJECTED = "rejected"      # failed validation — kept, never silently discarded (see spec §23)


class DuplicateMatchLevel(str, Enum):
    """Which deduplication level matched — recorded so a duplicate decision is always auditable, never a black box."""
    NONE = "none"
    PROVIDER_ID = "provider_id"            # Level 1 — cheapest, most certain
    CANONICAL_URL = "canonical_url"        # Level 2
    TITLE_SOURCE_TIME = "title_source_time"  # Level 3
    CONTENT_SIMILARITY = "content_similarity"  # Level 4 — most expensive, least certain


@dataclass
class RawArticle:
    """
    Exactly what a provider returned, preserved for inspection and
    reproducibility. Never consumed directly by business logic — the
    normalizer is the only thing that reads it.

    LEGAL NOTE (see spec §20): `payload` stores the provider's own
    response metadata. It is deliberately NOT assumed to be a place to
    permanently store full licensed article text — providers differ on
    what may be retained, and the normalizer stores only title,
    summary/description and URL by default.
    """
    raw_id: str
    provider: str                      # e.g. "rss", "finnhub", "alpha_vantage"
    provider_article_id: Optional[str]  # the provider's own id, when it supplies one
    fetched_at: datetime                # our ingestion clock — distinct from any provider timestamp
    payload: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.fetched_at = _require_utc(self.fetched_at, "fetched_at")


@dataclass
class NormalizedArticle:
    """
    The internal canonical article. Provider-agnostic by construction:
    nothing downstream should ever need to know which provider
    produced a given article.
    """
    # --- identity ---
    article_id: str
    provider: str
    provider_article_id: Optional[str] = None
    raw_id: Optional[str] = None                 # link back to the RawArticle it came from

    # --- source ---
    source_id: Optional[str] = None              # canonical NewsSource id (Phase 1)
    source_name: Optional[str] = None            # kept alongside the id: existing code (source_credibility.py,
                                                  # dashboard.py) joins on the NAME, so dropping it would break them
    source_url: Optional[str] = None
    canonical_url: Optional[str] = None          # dedup Level 2 key

    # --- content ---
    title: str = ""
    summary: Optional[str] = None
    language: Optional[str] = None
    country: Optional[str] = None
    author: Optional[str] = None
    categories: List[str] = field(default_factory=list)

    # --- time (all four kept distinct — see spec §9, look-ahead bias) ---
    published_at: Optional[datetime] = None      # when the SOURCE says it was published
    ingested_at: Optional[datetime] = None       # when WE first stored it
    updated_at: Optional[datetime] = None        # when the provider last revised it, if ever

    # --- deduplication ---
    fingerprint: Optional[str] = None            # normalized title+source+day — dedup Level 3
    content_fingerprint: Optional[str] = None    # normalized body/summary text — dedup Level 4
    duplicate_of: Optional[str] = None           # article_id of the canonical original, if this is a duplicate
    duplicate_match_level: DuplicateMatchLevel = DuplicateMatchLevel.NONE

    # --- entity references (Phase 1 canonical ids, NOT raw ticker strings) ---
    company_ids: List[str] = field(default_factory=list)
    instrument_ids: List[str] = field(default_factory=list)
    sector_ids: List[str] = field(default_factory=list)
    event_ids: List[str] = field(default_factory=list)

    # --- analytics carried over from the EXISTING pipeline, preserved as-is ---
    # These are NOT recomputed by Phase 2 — they are carried through so
    # migrated articles keep the sentiment/impact values the existing
    # engines already produced (see spec §24: existing UI must keep working).
    sentiment_label: Optional[str] = None
    sentiment_score: Optional[Decimal] = None
    impact_score: Optional[Decimal] = None

    # --- pipeline state ---
    processing_status: ProcessingStatus = ProcessingStatus.INGESTED
    rejection_reason: Optional[str] = None       # populated only when status is REJECTED

    # --- provider version tracking (see spec §8) ---
    version: int = 1

    def __post_init__(self):
        self.published_at = _require_utc(self.published_at, "published_at")
        self.ingested_at = _require_utc(self.ingested_at, "ingested_at")
        self.updated_at = _require_utc(self.updated_at, "updated_at")

    @property
    def is_duplicate(self) -> bool:
        return self.duplicate_of is not None


@dataclass
class IngestionCheckpoint:
    """
    Resumable position within a historical import (see spec §15). If an
    import stops halfway, the next run reads the checkpoint and
    continues from `cursor` rather than restarting from zero.
    """
    checkpoint_id: str
    provider: str
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None
    cursor: Optional[str] = None                 # provider-specific page token / offset / last-seen id
    articles_ingested: int = 0
    completed: bool = False
    last_updated_at: Optional[datetime] = None
    last_error: Optional[str] = None

    def __post_init__(self):
        self.period_start = _require_utc(self.period_start, "period_start")
        self.period_end = _require_utc(self.period_end, "period_end")
        self.last_updated_at = _require_utc(self.last_updated_at, "last_updated_at")


@dataclass
class IngestionStats:
    """Per-run observability counters (see spec §22). Deliberately contains no API keys or credentials."""
    provider: str
    requested: int = 0
    received: int = 0
    normalized: int = 0
    rejected: int = 0
    duplicates_detected: int = 0
    updated: int = 0
    entities_linked: int = 0
    provider_errors: int = 0
    rate_limit_events: int = 0
    duration_seconds: Optional[float] = None

    def as_log_dict(self) -> Dict[str, Any]:
        """Flat dict suitable for structured logging — no sensitive fields by construction."""
        return {
            "provider": self.provider, "requested": self.requested, "received": self.received,
            "normalized": self.normalized, "rejected": self.rejected,
            "duplicates_detected": self.duplicates_detected, "updated": self.updated,
            "entities_linked": self.entities_linked, "provider_errors": self.provider_errors,
            "rate_limit_events": self.rate_limit_events, "duration_seconds": self.duration_seconds,
        }
