"""
src/events/extractor.py
----------------------------
Tiered event extraction pipeline (Phase 4, spec §6, §7, §8, §26).

    ARTICLE -> RELEVANT? -> ENTITIES -> CANDIDATES -> TYPE -> ATTRIBUTES
            -> TEMPORAL -> PARTICIPANTS -> CONFIDENCE -> STRUCTURED EVENT

TIERING, AND WHAT THIS PHASE ACTUALLY IMPLEMENTS (spec §26):
    Tier 1  cheap relevance filter        IMPLEMENTED
    Tier 2  deterministic rules           IMPLEMENTED
    Tier 3  statistical / NLP             interface only, NOT implemented
    Tier 4  semantic / LLM                interface only, NOT implemented
    Tier 5  human review                  correction model only (spec §27)

Stated plainly: Phase 4 ships tiers 1-2. Tiers 3-4 are pluggable via
`fallback_extractors` — the architecture supports them, this phase
does not build them, and NOTHING here calls a model. ExtractionStats
therefore reports llm_calls=0 by construction, not by accident.
"""

import time
import uuid
import logging
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any, Callable, Tuple

from src.domain.event_models import (
    StructuredEvent, EventEvidence, EventConfidence, EventParticipant,
    ParticipationRole, EventStatus, ExtractionTier, ExtractionStats, EventGeography,
)
from src.events.taxonomy import (
    EventType, EVENT_TYPE_RULES, category_for, from_legacy_string, is_valid_subtype,
)
from src.events.fingerprint import compute_event_fingerprint

logger = logging.getLogger("marketlens.events.extractor")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


#: Tier 1 gate. An article with none of these has essentially no chance
#: of describing a financial event, and is discarded before any
#: further work — the cheapest possible saving at scale.
_FINANCIAL_RELEVANCE_MARKERS = [
    "company", "shares", "stock", "market", "revenue", "earnings", "profit", "loss",
    "investor", "quarterly", "guidance", "acquisition", "merger", "partnership",
    "contract", "regulator", "chief executive", "ceo", "dividend", "bond", "ipo",
    "tariff", "sanction", "production", "chip", "supply", "inflation", "interest rate",
    "central bank", "gdp", "unemployment", "lawsuit", "bankruptcy", "layoff",
]

#: Source-quality inputs to confidence. Mirrors source_credibility.py's
#: existing tiers rather than inventing a parallel scheme.
_SOURCE_QUALITY_BY_TIER = {
    "official": 1.0,
    "wire_and_major_press": 0.8,
    # The `news_sources` table spells the same tier with "or". Both
    # are accepted here rather than relying on every caller to
    # normalise, because falling through to the 0.4 unclassified
    # default is silent and looks exactly like a genuinely unknown
    # source.
    "wire_or_major_press": 0.8,
    "specialized_or_aggregator": 0.6,
    "unclassified": 0.4,
}


class EventExtractor:
    """Extracts structured events from normalized articles, cheapest tier first."""

    def __init__(
        self,
        min_confidence: float = 0.3,
        fallback_extractors: Optional[List[Callable[[Dict[str, Any]], List[StructuredEvent]]]] = None,
    ):
        """
        Args:
            min_confidence: events scoring below this are rejected
                rather than stored as noise. Default 0.3.
            fallback_extractors: optional Tier 3/4 extractors, tried
                only when the deterministic tier finds nothing. None in
                Phase 4 — the hook exists so a later phase can add a
                model-based extractor without touching this class.
        """
        self.min_confidence = min_confidence
        self.fallback_extractors = fallback_extractors or []

    # ---------------- Tier 1: cheap relevance filter ----------------

    @staticmethod
    def is_potentially_relevant(article: Dict[str, Any]) -> bool:
        """
        Tier 1 gate: does this article plausibly concern financial
        events at all? Deliberately permissive — its job is discarding
        the obviously irrelevant cheaply, not deciding anything.
        """
        text = f"{article.get('title', '')} {article.get('summary', '') or ''}".lower()
        if not text.strip():
            return False
        return any(marker in text for marker in _FINANCIAL_RELEVANCE_MARKERS)

    # ---------------- Tier 2: deterministic classification ----------------

    @staticmethod
    def classify(text: str) -> List[Tuple[EventType, float, str]]:
        """
        Apply the taxonomy's deterministic rules.

        Returns:
            A list of (event_type, extraction_certainty, matched_phrase),
            one per distinct type matched — an article CAN legitimately
            describe several events (spec §2), so this never collapses
            to a single answer.

        Certainty reflects how specific the matched phrase was: a long,
        unambiguous phrase is stronger evidence than a short generic one.
        """
        lowered = text.lower()
        matches: Dict[EventType, Tuple[float, str]] = {}

        for rule in EVENT_TYPE_RULES:
            if any(neg in lowered for neg in rule.negations):
                continue
            for phrase in rule.phrases:
                if phrase in lowered:
                    # Longer matched phrase -> higher certainty, capped at 0.9;
                    # a deterministic keyword match never claims certainty.
                    certainty = min(0.9, 0.5 + len(phrase) / 60)
                    previous = matches.get(rule.event_type)
                    if previous is None or certainty > previous[0]:
                        matches[rule.event_type] = (certainty, phrase)
                    break

        return [(t, c, p) for t, (c, p) in matches.items()]

    # ---------------- attribute + temporal extraction ----------------

    @staticmethod
    def extract_attributes(event_type: EventType, text: str) -> Dict[str, Any]:
        """
        Event-type-specific structured attributes (spec §11).

        DELIBERATELY MINIMAL: only attributes that can be read off the
        text deterministically and unambiguously are recorded. Numeric
        financial figures (revenue, EPS, deal value) are NOT parsed
        here — free-text figures come with currencies, units, scaling
        ("$2.5bn"), and period qualifiers that a regex gets wrong often
        enough to corrupt the factual record. Those belong to a
        structured-provider tier, and are left absent rather than
        guessed.
        """
        lowered = text.lower()
        attributes: Dict[str, Any] = {}

        if event_type == EventType.EARNINGS:
            if "beat" in lowered or "tops" in lowered or "exceeds" in lowered:
                attributes["outcome"] = "beat"
            elif "miss" in lowered or "falls short" in lowered or "below" in lowered:
                attributes["outcome"] = "miss"
            for period in ("q1", "q2", "q3", "q4"):
                if period in lowered:
                    attributes["reported_period"] = period.upper()
                    break

        elif event_type == EventType.GUIDANCE:
            if "raise" in lowered or "raises" in lowered or "lifts" in lowered:
                attributes["direction"] = "raised"
            elif "cut" in lowered or "lowers" in lowered or "reduces" in lowered:
                attributes["direction"] = "cut"

        elif event_type in (EventType.ACQUISITION, EventType.MERGER):
            if "completes" in lowered or "completed" in lowered:
                attributes["deal_status"] = "completed"
            elif "agrees" in lowered or "to acquire" in lowered:
                attributes["deal_status"] = "announced"

        elif event_type == EventType.MANAGEMENT_CHANGE:
            if "ceo" in lowered or "chief executive" in lowered:
                attributes["role"] = "ceo"
            elif "cfo" in lowered or "chief financial" in lowered:
                attributes["role"] = "cfo"

        return attributes

    @staticmethod
    def infer_subtype(event_type: EventType, attributes: Dict[str, Any]) -> Optional[str]:
        """Derive a taxonomy subtype from extracted attributes, when one is clearly implied."""
        mapping = {
            (EventType.EARNINGS, "beat"): "earnings_beat",
            (EventType.EARNINGS, "miss"): "earnings_miss",
            (EventType.GUIDANCE, "raised"): "guidance_raised",
            (EventType.GUIDANCE, "cut"): "guidance_cut",
            (EventType.MANAGEMENT_CHANGE, "ceo"): "ceo_change",
            (EventType.MANAGEMENT_CHANGE, "cfo"): "cfo_change",
        }
        for key in ("outcome", "direction", "role"):
            value = attributes.get(key)
            if value:
                subtype = mapping.get((event_type, value))
                if subtype and is_valid_subtype(event_type, subtype):
                    return subtype
        return None

    @staticmethod
    def build_participants(
        entity_ids: List[str],
        resolution_confidence_by_entity: Optional[Dict[str, float]] = None,
    ) -> List[EventParticipant]:
        """
        Assign participation roles (spec §14, §19).

        HEURISTIC, STATED PLAINLY: the first resolved entity is treated
        as PRIMARY and the rest as SECONDARY. That reflects how
        financial headlines are conventionally written ("NVIDIA expands
        partnership with TSMC"), and is right far more often than not —
        but it IS a heuristic, not a determination, which is why
        resolution confidence travels with each participant and why the
        role can be corrected (spec §27) rather than being treated as
        settled fact.
        """
        confidences = resolution_confidence_by_entity or {}
        participants = []
        for index, entity_id in enumerate(entity_ids):
            participants.append(EventParticipant(
                entity_id=entity_id,
                role=ParticipationRole.PRIMARY if index == 0 else ParticipationRole.SECONDARY,
                resolution_confidence=confidences.get(entity_id),
            ))
        return participants

    # ---------------- confidence ----------------

    @staticmethod
    def build_confidence(
        extraction_certainty: float,
        participants: List[EventParticipant],
        source_tier: Optional[str],
        has_event_time: bool,
    ) -> EventConfidence:
        """
        Assemble the explainable confidence components (spec §8). Every
        input is named and inspectable; nothing is a magic constant.
        """
        resolution_values = [p.resolution_confidence for p in participants if p.resolution_confidence is not None]
        entity_confidence = sum(resolution_values) / len(resolution_values) if resolution_values else 0.5

        return EventConfidence(
            extraction_certainty=extraction_certainty,
            entity_resolution_confidence=entity_confidence,
            source_quality=_SOURCE_QUALITY_BY_TIER.get(source_tier or "unclassified", 0.4),
            # Temporal certainty is LOW when we only know when it was
            # PUBLISHED, not when it HAPPENED — the distinction that
            # matters for later market-impact work (spec §12).
            temporal_certainty=1.0 if has_event_time else 0.5,
            corroboration=0.0,   # Phase 5's job; honestly zero until then
        )

    # ---------------- main entry point ----------------

    def extract_from_article(
        self,
        article: Dict[str, Any],
        entity_ids: Optional[List[str]] = None,
        resolution_confidence_by_entity: Optional[Dict[str, float]] = None,
        source_tier: Optional[str] = None,
        geography: Optional[EventGeography] = None,
    ) -> List[StructuredEvent]:
        """
        Extract every structured event described by one article.

        Args:
            article: a normalized article dict (title, summary,
                article_id, source_name, published_at, ingested_at,
                event_time optionally).
            entity_ids: canonical Phase 3 entity ids already resolved
                for this article. This extractor does NOT resolve
                entities itself — that is Phase 3's responsibility, and
                duplicating it here would create a second, divergent
                resolver.

        Returns:
            Zero, one, or several StructuredEvents (spec §2). An
            article with no entities yields nothing: an event with no
            participants is not a financial event.
        """
        if not self.is_potentially_relevant(article):
            return []
        if not entity_ids:
            return []

        text = f"{article.get('title', '')} {article.get('summary', '') or ''}"
        classifications = self.classify(text)

        if not classifications:
            for fallback in self.fallback_extractors:
                events = fallback(article)
                if events:
                    return events
            return []

        detection_time = datetime.now(timezone.utc)
        publication_time = article.get("published_at")
        event_time = article.get("event_time")

        evidence = EventEvidence(
            article_id=article.get("article_id", ""),
            source_id=article.get("source_id"),
            source_name=article.get("source_name"),
            published_at=publication_time,
            excerpt=(article.get("title") or "")[:300],
            is_syndicated=bool(article.get("is_syndicated", False)),
        )

        participants = self.build_participants(entity_ids, resolution_confidence_by_entity)

        events = []
        for event_type, certainty, matched_phrase in classifications:
            attributes = self.extract_attributes(event_type, text)
            attributes["matched_phrase"] = matched_phrase

            confidence = self.build_confidence(certainty, participants, source_tier, event_time is not None)
            if confidence.score() < self.min_confidence:
                continue

            event = StructuredEvent(
                event_id=f"evt-{uuid.uuid4().hex[:16]}",
                event_type=event_type,
                category=category_for(event_type),
                subtype=self.infer_subtype(event_type, attributes),
                title=article.get("title", "")[:300],
                # Factual restatement only — never an implication (spec §10).
                description=f"Reported: {article.get('title', '')}"[:500],
                participants=participants,
                geography=geography if (geography and not geography.is_empty()) else None,
                event_time=event_time,
                publication_time=publication_time,
                ingestion_time=article.get("ingested_at"),
                detection_time=detection_time,
                evidence=[evidence],
                confidence=confidence,
                status=EventStatus.DETECTED,   # never CONFIRMED from one report
                extraction_tier=ExtractionTier.DETERMINISTIC_RULE,
                attributes=attributes,
                created_at=detection_time,
            )
            event.fingerprint = compute_event_fingerprint(event)
            events.append(event)

        return events

    def extract_batch(
        self,
        articles: List[Dict[str, Any]],
        entity_ids_by_article: Optional[Dict[str, List[str]]] = None,
        source_tier_by_article: Optional[Dict[str, str]] = None,
    ) -> Tuple[List[StructuredEvent], ExtractionStats]:
        """
        Extract from a batch, returning both the events and full
        observability stats (spec §28).

        A failure on one article never aborts the batch — it is counted
        in stats.extraction_failures and the run continues, the same
        resilience principle the existing pipeline already uses.
        """
        started = time.monotonic()
        stats = ExtractionStats()
        entity_ids_by_article = entity_ids_by_article or {}
        source_tier_by_article = source_tier_by_article or {}

        all_events: List[StructuredEvent] = []
        for article in articles:
            stats.articles_processed += 1
            article_id = article.get("article_id", "")

            if not self.is_potentially_relevant(article):
                stats.articles_filtered_out += 1
                continue

            try:
                events = self.extract_from_article(
                    article,
                    entity_ids=entity_ids_by_article.get(article_id),
                    source_tier=source_tier_by_article.get(article_id),
                )
            except Exception as exc:  # noqa: BLE001 — one bad article must not kill the batch
                stats.extraction_failures += 1
                logger.error("Event extraction failed for article %s: %s", article_id, exc)
                continue

            for event in events:
                stats.events_detected += 1
                type_key = event.event_type.value
                stats.by_event_type[type_key] = stats.by_event_type.get(type_key, 0) + 1
                band_key = event.confidence.band().value
                stats.by_confidence_band[band_key] = stats.by_confidence_band.get(band_key, 0) + 1
            all_events.extend(events)

        stats.duration_seconds = round(time.monotonic() - started, 3)
        logger.info("Event extraction: %s", stats.as_log_dict())
        return all_events, stats
