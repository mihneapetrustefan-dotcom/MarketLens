"""
src/entities/alias_index.py
--------------------------------
Precomputed, in-memory lookup index for entity resolution.

RESPONSIBILITY:
Make the cheap tiers of the resolution pipeline O(1). The system will
eventually resolve millions of mentions; a linear scan over 389+
companies per mention would dominate the cost for no reason.

WHAT IS CACHED, AND WHY (per the phase's caching rule): the index
holds normalized alias -> entity maps built ONCE at construction, then
reused for every lookup. It is deliberately a plain in-process dict —
not Redis, not a external cache — because the entity set is small
(hundreds), changes rarely, and rebuilding is milliseconds. Adding
external cache infrastructure here would be unjustified.

NORMALIZATION is the single most important detail: "NVIDIA Corp.",
"Nvidia Corporation" and "nvidia corp" must all collapse to the same
lookup key, while genuinely different names must not.
"""

import re
from typing import Dict, List, Set, Optional, Iterable

from src.domain.entity_models import EntityAlias, EntityIdentifier, EntityType, IdentifierType


# Corporate suffixes stripped during normalization so "NVIDIA Corp" and
# "NVIDIA Corporation" collapse together. Deliberately conservative —
# only unambiguous legal-form suffixes, never ordinary words.
_CORPORATE_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company",
    "ltd", "limited", "llc", "plc", "sa", "nv", "ag", "gmbh", "spa",
    "holdings", "holding", "group", "technologies", "technology",
}


def normalize_name(name: Optional[str]) -> str:
    """
    Normalize an entity name for lookup: lowercase, strip punctuation,
    drop corporate suffixes, collapse whitespace.

    IMPORTANT: suffix stripping never empties a name — if a name
    consists ONLY of suffix words (e.g. "Holdings"), the un-stripped
    form is kept, since returning "" would make every such name
    collide with every other.
    """
    if not name:
        return ""
    cleaned = re.sub(r"[^\w\s]", " ", name.lower())
    tokens = [t for t in cleaned.split() if t]
    if not tokens:
        return ""
    stripped = [t for t in tokens if t not in _CORPORATE_SUFFIXES]
    return " ".join(stripped) if stripped else " ".join(tokens)


class AliasIndex:
    """
    In-memory index over aliases and external identifiers.

    Every lookup method returns a LIST of entity ids, never a single
    id — because a name can legitimately map to several entities, and
    hiding that behind a single return value is exactly how blind
    false-positive matches happen. Choosing between candidates is the
    resolver's job, not the index's.
    """

    def __init__(self, aliases: Optional[Iterable[EntityAlias]] = None,
                 identifiers: Optional[Iterable[EntityIdentifier]] = None):
        self._by_normalized_alias: Dict[str, List[str]] = {}
        self._alias_records: Dict[str, List[EntityAlias]] = {}
        self._by_identifier: Dict[str, List[str]] = {}
        self._entity_types: Dict[str, EntityType] = {}
        self._ambiguous_normalized: Set[str] = set()

        for alias in aliases or []:
            self.add_alias(alias)
        for identifier in identifiers or []:
            self.add_identifier(identifier)

    # ---------------- building ----------------

    def add_alias(self, alias: EntityAlias) -> None:
        key = alias.normalized_alias or normalize_name(alias.alias)
        if not key:
            return
        self._by_normalized_alias.setdefault(key, [])
        if alias.entity_id not in self._by_normalized_alias[key]:
            self._by_normalized_alias[key].append(alias.entity_id)
        self._alias_records.setdefault(key, []).append(alias)
        self._entity_types[alias.entity_id] = alias.entity_type
        if alias.ambiguity_risk:
            self._ambiguous_normalized.add(key)

    def add_identifier(self, identifier: EntityIdentifier) -> None:
        key = self._identifier_key(identifier.identifier_type, identifier.value)
        self._by_identifier.setdefault(key, [])
        if identifier.entity_id not in self._by_identifier[key]:
            self._by_identifier[key].append(identifier.entity_id)
        self._entity_types[identifier.entity_id] = identifier.entity_type

    @staticmethod
    def _identifier_key(identifier_type: IdentifierType, value: str) -> str:
        # Tickers are case-insensitive in practice ("$nvda" and "NVDA"
        # are the same); ISIN/CUSIP/SEDOL are conventionally uppercase.
        return f"{identifier_type.value}:{(value or '').strip().upper()}"

    # ---------------- lookup ----------------

    def lookup_alias(self, text: str) -> List[str]:
        """Entity ids whose normalized alias exactly matches `text`. Empty list when nothing matches."""
        return list(self._by_normalized_alias.get(normalize_name(text), []))

    def lookup_identifier(self, identifier_type: IdentifierType, value: str) -> List[str]:
        """Entity ids carrying this external identifier."""
        return list(self._by_identifier.get(self._identifier_key(identifier_type, value), []))

    def lookup_ticker(self, ticker: str) -> List[str]:
        """
        Entity ids for a BARE ticker. May legitimately return several
        (the documented Electrica/Estee Lauder "EL" case) — the caller
        must handle that rather than assuming one.
        """
        cleaned = (ticker or "").strip().lstrip("$")
        return self.lookup_identifier(IdentifierType.TICKER, cleaned)

    def lookup_exchange_ticker(self, exchange_id: str, ticker: str) -> List[str]:
        """The unambiguous form: exchange + ticker together."""
        return self.lookup_identifier(IdentifierType.EXCHANGE_TICKER, f"{exchange_id}:{ticker}")

    def is_ambiguous_alias(self, text: str) -> bool:
        """Whether this normalized name was registered as ambiguity-prone (e.g. bare 'Apple')."""
        return normalize_name(text) in self._ambiguous_normalized

    def entity_type_of(self, entity_id: str) -> Optional[EntityType]:
        return self._entity_types.get(entity_id)

    def all_normalized_aliases(self) -> List[str]:
        """Every normalized alias key — the candidate pool for fuzzy matching."""
        return list(self._by_normalized_alias.keys())

    def alias_records_for(self, normalized_alias: str) -> List[EntityAlias]:
        return list(self._alias_records.get(normalized_alias, []))

    @property
    def alias_count(self) -> int:
        return len(self._by_normalized_alias)

    @property
    def identifier_count(self) -> int:
        return len(self._by_identifier)
