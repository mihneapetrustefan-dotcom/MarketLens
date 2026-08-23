"""
src/domain/entity_models.py
--------------------------------
Phase 3 canonical entity-intelligence models.

RESPONSIBILITY:
Answer "WHO/WHAT is this piece of information about?" — with an
explicit, auditable confidence, and never by force. The models here
sit ON TOP of Phase 1's Company/Security/Instrument (domain/models.py),
which remain the canonical identities; nothing here replaces them.

THREE DESIGN COMMITMENTS, each reflecting an explicit Phase 3 rule:

1. AMBIGUITY IS A FIRST-CLASS OUTCOME. A resolution may legitimately
   end as AMBIGUOUS or UNRESOLVED. The system must be able to say
   "I am not sufficiently confident this is X" rather than guessing.

2. FACT AND INFERENCE ARE NEVER MIXED. Every relationship records its
   provenance and whether it was directly sourced or inferred. No
   relationship is ever fabricated to fill a gap.

3. HISTORICAL IDENTITY IS NEVER DESTROYED. Renames, ticker changes,
   mergers and delistings are recorded as timestamped CHANGES against
   a stable internal id — so an article linked to a company in 2024
   stays linked after the company renames in 2026.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, List, Dict, Any


def _require_utc(value: Optional[datetime], field_name: str) -> Optional[datetime]:
    """Same UTC enforcement used across Phase 1/2 — naive or non-UTC timestamps are rejected, never silently guessed."""
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware (got a naive datetime)")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{field_name} must be in UTC (got offset {value.utcoffset()})")
    return value


class EntityType(str, Enum):
    """
    What KIND of thing an entity is. Deliberately not collapsed into
    "company" — a sector, a country and a company are different things
    and conflating them is exactly the failure this phase exists to fix.

    PERSON / ORGANIZATION / PRODUCT are declared but NOT populated by
    any Phase 3 code — the model is extensible for them, per the
    phase's "do not implement unnecessary future entities" rule.
    """
    COMPANY = "company"
    SECURITY = "security"
    INSTRUMENT = "instrument"
    EXCHANGE = "exchange"
    SECTOR = "sector"
    INDUSTRY = "industry"
    COUNTRY = "country"
    PERSON = "person"                # extensible, not populated in Phase 3
    ORGANIZATION = "organization"    # extensible, not populated in Phase 3
    PRODUCT = "product"              # extensible, not populated in Phase 3


class ResolutionStatus(str, Enum):
    """
    Outcome of a resolution attempt. AMBIGUOUS and UNRESOLVED are
    SUCCESSFUL outcomes of an honest system, not failures to hide.
    """
    RESOLVED = "resolved"                  # single confident match
    HIGH_CONFIDENCE = "high_confidence"    # single match, strong but not exact
    AMBIGUOUS = "ambiguous"                # several plausible candidates — deliberately NOT picked
    UNRESOLVED = "unresolved"              # no candidate cleared the bar
    REJECTED = "rejected"                  # matched a known false-positive pattern


class ResolutionMethod(str, Enum):
    """
    Which tier of the pipeline produced a match — recorded on every
    mention so resolution quality can be measured per method, and so a
    wrong match is always traceable to the rule that caused it.

    Ordered cheapest/most-certain first. SEMANTIC_MODEL exists as a
    declared tier but is NEVER used by Phase 3 code: the pipeline is
    entirely deterministic + fuzzy, per the phase's cost-control rule.
    """
    EXACT_NAME = "exact_name"
    ALIAS = "alias"
    TICKER = "ticker"
    EXCHANGE_TICKER = "exchange_ticker"
    PROVIDER_ID = "provider_id"
    FUZZY = "fuzzy"
    CONTEXTUAL = "contextual"
    SEMANTIC_MODEL = "semantic_model"      # reserved; not used in Phase 3
    NONE = "none"


class MentionRelevance(str, Enum):
    """
    How central an entity is TO THE TEXT.

    Explicitly NOT a claim about market impact — "mentioned in an
    article" and "materially affected by an event" are different
    questions, and the latter belongs to the future Event/Impact
    layers, not here.
    """
    PRIMARY = "primary"
    SECONDARY = "secondary"
    MENTIONED = "mentioned"
    RELATED = "related"


class IdentifierType(str, Enum):
    """External identifier namespaces. Internal ids remain primary — these are always MAPPINGS, never the identity itself."""
    TICKER = "ticker"
    EXCHANGE_TICKER = "exchange_ticker"
    ISIN = "isin"
    CUSIP = "cusip"
    SEDOL = "sedol"
    PROVIDER_ID = "provider_id"
    DOMAIN = "domain"


class AliasType(str, Enum):
    """How an alias relates to its entity — kept explicit so risky alias classes can be scored differently."""
    LEGAL_NAME = "legal_name"
    DISPLAY_NAME = "display_name"
    SHORT_NAME = "short_name"
    ABBREVIATION = "abbreviation"
    TICKER_ALIAS = "ticker_alias"
    HISTORICAL_NAME = "historical_name"    # e.g. "Facebook" for Meta Platforms
    PROVIDER_ALIAS = "provider_alias"


class RelationshipType(str, Enum):
    """Company-to-company relationship kinds."""
    OWNS = "owns"
    SUBSIDIARY_OF = "subsidiary_of"
    PARENT_OF = "parent_of"
    COMPETITOR_OF = "competitor_of"
    SUPPLIER_OF = "supplier_of"
    CUSTOMER_OF = "customer_of"
    PARTNER_OF = "partner_of"
    INVESTOR_IN = "investor_in"
    OPERATES_IN = "operates_in"


class ProvenanceKind(str, Enum):
    """
    Whether a relationship is a stated FACT or a derived INFERENCE.
    These are never mixed: a downstream consumer must always be able
    to filter to facts alone.
    """
    SOURCED_FACT = "sourced_fact"
    INFERRED = "inferred"


class IdentityChangeType(str, Enum):
    """Corporate identity events that must not destroy historical continuity."""
    RENAME = "rename"
    TICKER_CHANGE = "ticker_change"
    MERGER = "merger"
    ACQUISITION = "acquisition"
    SPINOFF = "spinoff"
    DELISTING = "delisting"
    EXCHANGE_CHANGE = "exchange_change"


@dataclass
class EntityIdentifier:
    """
    One external identifier mapped to an internal entity. Many
    identifiers may point at the same entity; the internal id stays
    primary (per the phase's "do not make provider IDs the primary
    identity" rule).
    """
    entity_id: str
    entity_type: EntityType
    identifier_type: IdentifierType
    value: str
    provider: Optional[str] = None      # which provider issued it, for PROVIDER_ID
    active: bool = True


@dataclass
class EntityAlias:
    """
    One name an entity is known by, plus a NORMALIZED form for lookup.

    `ambiguity_risk` is set by whoever registers the alias and is used
    by the resolver to refuse blind matches on dangerous names (e.g.
    the bare word "Apple"). It is a declared property of the alias, not
    an invented score.
    """
    entity_id: str
    entity_type: EntityType
    alias: str
    normalized_alias: str
    alias_type: AliasType = AliasType.DISPLAY_NAME
    ambiguity_risk: bool = False
    valid_from: Optional[datetime] = None    # for historical names
    valid_until: Optional[datetime] = None

    def __post_init__(self):
        self.valid_from = _require_utc(self.valid_from, "valid_from")
        self.valid_until = _require_utc(self.valid_until, "valid_until")


@dataclass
class ResolutionResult:
    """
    The outcome of resolving ONE mention text. Always records HOW it
    resolved and how confident it is — a match with no traceable
    method is never produced.
    """
    query: str
    status: ResolutionStatus
    method: ResolutionMethod = ResolutionMethod.NONE
    entity_id: Optional[str] = None
    entity_type: Optional[EntityType] = None
    confidence: Decimal = Decimal("0")
    candidates: List[str] = field(default_factory=list)   # populated when AMBIGUOUS
    reason: Optional[str] = None                           # why it was ambiguous/unresolved/rejected

    @property
    def is_confident(self) -> bool:
        return self.status in (ResolutionStatus.RESOLVED, ResolutionStatus.HIGH_CONFIDENCE)


@dataclass
class EntityMention:
    """
    A specific entity referenced by a specific article.

    `relevance` describes prominence IN THE TEXT only (see
    MentionRelevance's own note) — never market impact.
    """
    article_id: str
    entity_id: str
    entity_type: EntityType
    mention_text: str
    relevance: MentionRelevance = MentionRelevance.MENTIONED
    confidence: Decimal = Decimal("0")
    method: ResolutionMethod = ResolutionMethod.NONE
    position: Optional[int] = None      # character offset, when the extractor supplies one


@dataclass
class EntityRelationship:
    """
    A directed relationship between two entities, WITH PROVENANCE.

    A relationship without a stated source is not storable by
    construction: `source` is required. Nothing here invents
    relationships — see the phase's explicit "do NOT fabricate
    relationships" rule.
    """
    from_entity_id: str
    to_entity_id: str
    relationship_type: RelationshipType
    source: str                                    # required: where this came from
    provenance_kind: ProvenanceKind = ProvenanceKind.SOURCED_FACT
    confidence: Decimal = Decimal("1")
    observed_at: Optional[datetime] = None
    method: Optional[str] = None                   # how it was derived, for INFERRED relationships
    valid_until: Optional[datetime] = None         # relationships end (a supplier contract lapses)

    def __post_init__(self):
        self.observed_at = _require_utc(self.observed_at, "observed_at")
        self.valid_until = _require_utc(self.valid_until, "valid_until")
        if not self.source or not str(self.source).strip():
            raise ValueError("EntityRelationship requires a non-empty `source` (provenance is mandatory)")

    @property
    def is_fact(self) -> bool:
        return self.provenance_kind == ProvenanceKind.SOURCED_FACT


@dataclass
class IdentityChange:
    """
    A timestamped corporate identity event recorded AGAINST a stable
    internal entity id.

    Because the internal id never changes, a rename or ticker change
    leaves every historical article link intact — which is precisely
    what future quant research needs (per the phase's "do not destroy
    historical identity" rule).
    """
    entity_id: str
    change_type: IdentityChangeType
    effective_at: datetime
    previous_value: Optional[str] = None
    new_value: Optional[str] = None
    related_entity_id: Optional[str] = None    # the other party in a merger/acquisition/spinoff
    source: Optional[str] = None
    notes: Optional[str] = None

    def __post_init__(self):
        self.effective_at = _require_utc(self.effective_at, "effective_at")


@dataclass
class SectorClassification:
    """
    A sector/industry assignment FROM A NAMED SOURCE.

    Providers genuinely disagree about classification, so an assignment
    is always attributed rather than treated as absolute truth — and a
    conflicting assignment is stored ALONGSIDE, never silently
    overwriting an existing one (per the phase's explicit rule).
    """
    entity_id: str
    sector_id: Optional[str] = None
    industry_id: Optional[str] = None
    source: str = "internal"
    is_canonical: bool = False       # exactly one classification per entity should carry this
    effective_at: Optional[datetime] = None

    def __post_init__(self):
        self.effective_at = _require_utc(self.effective_at, "effective_at")


@dataclass
class ResolutionQualityMetrics:
    """Diagnostics for measuring entity-resolution quality over time (per the phase's quality-control rule)."""
    total_mentions: int = 0
    resolved: int = 0
    high_confidence: int = 0
    ambiguous: int = 0
    unresolved: int = 0
    rejected: int = 0
    by_method: Dict[str, int] = field(default_factory=dict)

    @property
    def resolution_rate(self) -> Optional[float]:
        """Share of mentions that reached a confident outcome. None when there is nothing to measure."""
        if self.total_mentions == 0:
            return None
        return round((self.resolved + self.high_confidence) / self.total_mentions, 4)

    @property
    def ambiguity_rate(self) -> Optional[float]:
        if self.total_mentions == 0:
            return None
        return round(self.ambiguous / self.total_mentions, 4)

    def as_log_dict(self) -> Dict[str, Any]:
        return {
            "total_mentions": self.total_mentions, "resolved": self.resolved,
            "high_confidence": self.high_confidence, "ambiguous": self.ambiguous,
            "unresolved": self.unresolved, "rejected": self.rejected,
            "resolution_rate": self.resolution_rate, "ambiguity_rate": self.ambiguity_rate,
            "by_method": dict(self.by_method),
        }
