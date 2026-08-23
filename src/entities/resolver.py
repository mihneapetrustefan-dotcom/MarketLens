"""
src/entities/resolver.py
-----------------------------
The tiered entity resolution pipeline (Phase 3, §11, §10, §12, §25).

    TEXT -> NORMALIZE -> IDENTIFIER -> EXACT ALIAS -> FUZZY -> CONTEXT -> RESULT

TIER ORDER IS A COST DECISION, not just a correctness one. Each tier is
attempted only if every cheaper tier missed:

    1. cashtag / exchange-qualified ticker   exact, free, unambiguous
    2. bare ticker                            exact, free, MAY be ambiguous
    3. exact normalized alias                 O(1) dict hit, free
    4. fuzzy alias match                      O(candidates), still cheap
    5. contextual disambiguation              only for otherwise-ambiguous cases

NO MODEL CALL EXISTS IN THIS FILE. ResolutionMethod.SEMANTIC_MODEL is
declared in the enum for a future tier but is never produced here —
the entire pipeline is deterministic + string-similarity, so cost per
mention stays effectively zero at millions of mentions.

REFUSING TO GUESS IS A FEATURE. When several entities match equally
well, the result is AMBIGUOUS with the candidates listed — never an
arbitrary pick. When a known-risky bare name ("Apple") appears with no
supporting context, the result is AMBIGUOUS rather than a confident
wrong answer.
"""

import re
import logging
from decimal import Decimal
from difflib import SequenceMatcher
from typing import List, Optional, Dict, Any, Set

from src.domain.entity_models import (
    EntityType, ResolutionStatus, ResolutionMethod, ResolutionResult,
    IdentifierType, ResolutionQualityMetrics,
)
from src.entities.alias_index import AliasIndex, normalize_name

logger = logging.getLogger("marketlens.entities.resolver")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


#: Words whose presence in surrounding text supports a FINANCIAL
#: reading of an ambiguous name ("Apple sales rose" vs "apple pie").
#: Deliberately a small, high-precision list — a broad list would
#: manufacture false confidence, which is the exact failure mode this
#: phase is meant to prevent.
_FINANCIAL_CONTEXT_TERMS = {
    "shares", "stock", "stocks", "earnings", "revenue", "profit", "quarterly",
    "nasdaq", "nyse", "investors", "analyst", "analysts", "guidance", "dividend",
    "market", "trading", "valuation", "ipo", "sec", "ceo", "cfo", "acquisition",
    "merger", "forecast", "outlook", "billion", "million", "eps", "buyback",
}

#: Cashtag form, e.g. "$NVDA" — an unambiguous, deliberate ticker reference.
_CASHTAG_PATTERN = re.compile(r"^\$([A-Za-z][A-Za-z0-9.\-]{0,9})$")

#: Exchange-qualified form, e.g. "NASDAQ:NVDA".
_EXCHANGE_TICKER_PATTERN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]{1,15}):([A-Za-z][A-Za-z0-9.\-]{0,9})$")


class EntityResolver:
    """
    Resolves messy real-world text to canonical entity ids, recording
    the method and confidence behind every decision.
    """

    def __init__(
        self,
        index: AliasIndex,
        fuzzy_threshold: float = 0.90,
        min_fuzzy_length: int = 5,
        cache_enabled: bool = True,
    ):
        """
        Args:
            index: the precomputed alias/identifier index.
            fuzzy_threshold: minimum similarity for a fuzzy match.
                Default 0.90 — high on purpose. Fuzzy matching is the
                single largest source of false positives in entity
                resolution, and a wrong confident match is far more
                damaging than an unresolved mention.
            min_fuzzy_length: names shorter than this are never fuzzy
                matched. Short strings are similar to each other by
                accident ("AMD"/"AMC" score highly), so fuzzy matching
                them produces confident nonsense.
            cache_enabled: memoize repeated identical queries. Article
                text repeats entity names constantly, so this is a
                large, free win (see the phase's caching rule).
        """
        self.index = index
        self.fuzzy_threshold = fuzzy_threshold
        self.min_fuzzy_length = min_fuzzy_length
        self.cache_enabled = cache_enabled
        self._cache: Dict[str, ResolutionResult] = {}
        self.metrics = ResolutionQualityMetrics()

    # ---------------- public API ----------------

    def resolve(self, text: str, context: Optional[str] = None,
                expected_type: Optional[EntityType] = None) -> ResolutionResult:
        """
        Resolve one mention text to a canonical entity.

        Args:
            text: the raw mention, e.g. "NVIDIA Corp.", "$NVDA", "NASDAQ:NVDA".
            context: optional surrounding text (headline + summary),
                used ONLY to disambiguate cases that are otherwise
                ambiguous — never to upgrade an already-clear match.
            expected_type: restrict matching to one entity type, when
                the caller knows it.

        Returns:
            A ResolutionResult — always, never None. An honest
            AMBIGUOUS/UNRESOLVED outcome is a valid result.
        """
        query = (text or "").strip()
        if not query:
            return self._record(ResolutionResult(
                query=query, status=ResolutionStatus.UNRESOLVED, reason="empty query"
            ))

        cache_key = f"{query}||{context or ''}||{expected_type.value if expected_type else ''}"
        if self.cache_enabled and cache_key in self._cache:
            return self._cache[cache_key]

        result = self._resolve_uncached(query, context, expected_type)

        if self.cache_enabled:
            self._cache[cache_key] = result
        return self._record(result)

    def resolve_batch(self, texts: List[str], context: Optional[str] = None,
                       expected_type: Optional[EntityType] = None) -> List[ResolutionResult]:
        """Resolve many mentions sharing one context (e.g. all names found in one article)."""
        return [self.resolve(t, context=context, expected_type=expected_type) for t in texts]

    def clear_cache(self) -> None:
        self._cache.clear()

    # ---------------- pipeline tiers ----------------

    def _resolve_uncached(self, query: str, context: Optional[str],
                           expected_type: Optional[EntityType]) -> ResolutionResult:

        # --- TIER 1: exchange-qualified ticker (unambiguous by construction) ---
        exchange_match = _EXCHANGE_TICKER_PATTERN.match(query)
        if exchange_match:
            exchange_id, ticker = exchange_match.group(1), exchange_match.group(2)
            ids = self.index.lookup_exchange_ticker(exchange_id, ticker)
            if len(ids) == 1:
                return ResolutionResult(
                    query=query, status=ResolutionStatus.RESOLVED,
                    method=ResolutionMethod.EXCHANGE_TICKER, entity_id=ids[0],
                    entity_type=self.index.entity_type_of(ids[0]), confidence=Decimal("1.0"),
                )

        # --- TIER 2: cashtag / bare ticker ---
        cashtag = _CASHTAG_PATTERN.match(query)
        ticker_candidate = cashtag.group(1) if cashtag else (query if self._looks_like_bare_ticker(query) else None)
        if ticker_candidate:
            ids = self._filter_by_type(self.index.lookup_ticker(ticker_candidate), expected_type)
            if len(ids) == 1:
                # A cashtag is an explicit, deliberate ticker reference,
                # so it scores higher than an inferred bare ticker.
                confidence = Decimal("1.0") if cashtag else Decimal("0.95")
                return ResolutionResult(
                    query=query, status=ResolutionStatus.RESOLVED,
                    method=ResolutionMethod.TICKER, entity_id=ids[0],
                    entity_type=self.index.entity_type_of(ids[0]), confidence=confidence,
                )
            if len(ids) > 1:
                disambiguated = self._disambiguate_with_context(ids, context)
                if disambiguated:
                    return ResolutionResult(
                        query=query, status=ResolutionStatus.HIGH_CONFIDENCE,
                        method=ResolutionMethod.CONTEXTUAL, entity_id=disambiguated,
                        entity_type=self.index.entity_type_of(disambiguated),
                        confidence=Decimal("0.75"), candidates=ids,
                        reason="several entities share this ticker; chosen using context",
                    )
                return ResolutionResult(
                    query=query, status=ResolutionStatus.AMBIGUOUS,
                    method=ResolutionMethod.TICKER, candidates=ids,
                    reason=f"ticker '{ticker_candidate}' maps to {len(ids)} entities across exchanges",
                )

        # --- TIER 3: exact normalized alias ---
        ids = self._filter_by_type(self.index.lookup_alias(query), expected_type)
        if len(ids) == 1:
            entity_id = ids[0]
            # A name flagged as ambiguity-prone is NOT accepted on the
            # strength of a single index hit — it must earn confidence
            # from surrounding financial context, or stay ambiguous.
            if self.index.is_ambiguous_alias(query):
                if self._has_financial_context(context):
                    return ResolutionResult(
                        query=query, status=ResolutionStatus.HIGH_CONFIDENCE,
                        method=ResolutionMethod.CONTEXTUAL, entity_id=entity_id,
                        entity_type=self.index.entity_type_of(entity_id), confidence=Decimal("0.80"),
                        reason="ambiguity-prone name accepted on financial context",
                    )
                return ResolutionResult(
                    query=query, status=ResolutionStatus.AMBIGUOUS,
                    method=ResolutionMethod.ALIAS, candidates=ids,
                    reason="ambiguity-prone name with no supporting financial context",
                )
            return ResolutionResult(
                query=query, status=ResolutionStatus.RESOLVED,
                method=ResolutionMethod.EXACT_NAME if normalize_name(query) else ResolutionMethod.ALIAS,
                entity_id=entity_id, entity_type=self.index.entity_type_of(entity_id),
                confidence=Decimal("0.98"),
            )
        if len(ids) > 1:
            disambiguated = self._disambiguate_with_context(ids, context)
            if disambiguated:
                return ResolutionResult(
                    query=query, status=ResolutionStatus.HIGH_CONFIDENCE,
                    method=ResolutionMethod.CONTEXTUAL, entity_id=disambiguated,
                    entity_type=self.index.entity_type_of(disambiguated),
                    confidence=Decimal("0.75"), candidates=ids,
                )
            return ResolutionResult(
                query=query, status=ResolutionStatus.AMBIGUOUS,
                method=ResolutionMethod.ALIAS, candidates=ids,
                reason=f"name matches {len(ids)} entities",
            )

        # --- TIER 4: fuzzy ---
        fuzzy = self._fuzzy_match(query, expected_type)
        if fuzzy:
            entity_id, score = fuzzy
            return ResolutionResult(
                query=query, status=ResolutionStatus.HIGH_CONFIDENCE,
                method=ResolutionMethod.FUZZY, entity_id=entity_id,
                entity_type=self.index.entity_type_of(entity_id),
                confidence=Decimal(str(round(score, 3))),
                reason=f"fuzzy match at {score:.2f}",
            )

        return ResolutionResult(
            query=query, status=ResolutionStatus.UNRESOLVED,
            reason="no identifier, alias or sufficiently similar name matched",
        )

    # ---------------- helpers ----------------

    @staticmethod
    def _looks_like_bare_ticker(text: str) -> bool:
        """A short all-caps token — the shape of a bare ticker. Lowercase text is never treated as a ticker."""
        return bool(re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", text))

    def _filter_by_type(self, entity_ids: List[str], expected_type: Optional[EntityType]) -> List[str]:
        if not expected_type:
            return entity_ids
        return [e for e in entity_ids if self.index.entity_type_of(e) == expected_type]

    @staticmethod
    def _has_financial_context(context: Optional[str]) -> bool:
        """Whether surrounding text contains enough financial vocabulary to support a company reading."""
        if not context:
            return False
        tokens = set(re.sub(r"[^\w\s]", " ", context.lower()).split())
        return len(tokens & _FINANCIAL_CONTEXT_TERMS) >= 2

    def _disambiguate_with_context(self, candidate_ids: List[str], context: Optional[str]) -> Optional[str]:
        """
        Pick between equally-matching candidates using context.

        Deliberately CONSERVATIVE: a candidate is chosen only if its own
        name/alias also appears in the surrounding text and no other
        candidate's does. If two candidates are both supported, or none
        is, this returns None and the caller reports AMBIGUOUS — the
        system declines rather than guessing.
        """
        if not context:
            return None
        context_normalized = normalize_name(context)
        if not context_normalized:
            return None

        supported = []
        for entity_id in candidate_ids:
            for normalized_alias, ids in self.index._by_normalized_alias.items():
                if entity_id in ids and len(normalized_alias) >= 4 and normalized_alias in context_normalized:
                    supported.append(entity_id)
                    break
        unique = list(dict.fromkeys(supported))
        return unique[0] if len(unique) == 1 else None

    def _fuzzy_match(self, query: str, expected_type: Optional[EntityType]):
        """
        Best fuzzy match above threshold, or None.

        Returns None (not a weak guess) when the top two candidates
        score within 0.02 of each other — near-ties are genuine
        ambiguity, and picking one arbitrarily would be exactly the
        blind resolution this phase forbids.
        """
        normalized_query = normalize_name(query)
        if len(normalized_query) < self.min_fuzzy_length:
            return None

        scored = []
        for candidate in self.index.all_normalized_aliases():
            if abs(len(candidate) - len(normalized_query)) > 6:
                continue  # cheap length prefilter before the O(n*m) comparison
            score = SequenceMatcher(None, normalized_query, candidate).ratio()
            if score >= self.fuzzy_threshold:
                for entity_id in self.index._by_normalized_alias[candidate]:
                    if not expected_type or self.index.entity_type_of(entity_id) == expected_type:
                        scored.append((entity_id, score))

        if not scored:
            return None
        scored.sort(key=lambda pair: pair[1], reverse=True)
        if len(scored) > 1 and scored[0][0] != scored[1][0] and (scored[0][1] - scored[1][1]) < 0.02:
            return None
        return scored[0]

    # ---------------- quality metrics ----------------

    def _record(self, result: ResolutionResult) -> ResolutionResult:
        """Update running quality metrics (per the phase's quality-control rule)."""
        self.metrics.total_mentions += 1
        if result.status == ResolutionStatus.RESOLVED:
            self.metrics.resolved += 1
        elif result.status == ResolutionStatus.HIGH_CONFIDENCE:
            self.metrics.high_confidence += 1
        elif result.status == ResolutionStatus.AMBIGUOUS:
            self.metrics.ambiguous += 1
        elif result.status == ResolutionStatus.REJECTED:
            self.metrics.rejected += 1
        else:
            self.metrics.unresolved += 1

        if result.method != ResolutionMethod.NONE:
            key = result.method.value
            self.metrics.by_method[key] = self.metrics.by_method.get(key, 0) + 1
        return result
