"""
src/entities/index_builder.py
----------------------------------
Builds an AliasIndex from the EXISTING, unmodified registries.

RESPONSIBILITY:
Turn company_registry.py / ticker_registry.py / sector_registry.py —
read-only, exactly as they are today — into the aliases and
identifiers the resolver needs. This is what makes Phase 3 work on the
real 389-company dataset rather than on a fixture.

AMBIGUITY FLAGGING IS DERIVED FROM THE REAL DATA, NOT INVENTED: an
alias is marked ambiguity-prone when it is (a) shared by more than one
entity, or (b) present in the explicit risky-name list below. That
list contains only names the project has ALREADY documented as
colliding in practice (see company_registry.py's own "KNOWN REMAINING
AMBIGUITY" notes) — nothing speculative was added.
"""

from typing import List, Dict, Any, Optional, Iterable

from src.domain.entity_models import (
    EntityAlias, EntityIdentifier, EntityType, AliasType, IdentifierType,
)
from src.entities.alias_index import AliasIndex, normalize_name
from src.providers.registry_adapter import _slugify, _CATEGORY_TO_EXCHANGE


#: Names the project has ALREADY observed colliding with ordinary
#: language or other meanings — documented in company_registry.py's own
#: module docstring. Marked ambiguity-prone so the resolver refuses to
#: match them without supporting financial context.
KNOWN_AMBIGUOUS_NAMES = {
    "apple",      # the fruit
    "visa",       # the travel document
    "oracle",     # a data oracle in crypto/blockchain writing
    "target",     # ordinary English verb/noun
    "shell",      # ordinary English noun
    "ford",       # a river crossing / surname
    "meta",       # ordinary prefix/adjective
    "block",      # ordinary English noun/verb
    "now",        # ServiceNow's ticker is NOW
    "bvb",        # also the football club Borussia Dortmund
    "vale",       # ordinary word (valley)
    "aon",        # short, collides easily
}


def build_index_from_registries(
    company_registry: List[Dict[str, Any]],
    sector_map: Optional[Dict[str, str]] = None,
    include_sectors: bool = True,
) -> AliasIndex:
    """
    Build a fully populated AliasIndex from the existing registries.

    Args:
        company_registry: company_registry.COMPANY_REGISTRY, unmodified.
        sector_map: sector_registry.COMPANY_SECTOR_MAP, unmodified —
            used to register sectors as their own resolvable entities.
        include_sectors: whether to index sectors alongside companies.

    Returns:
        An AliasIndex ready for EntityResolver.
    """
    aliases: List[EntityAlias] = []
    identifiers: List[EntityIdentifier] = []

    # First pass: count how many DISTINCT entities each normalized
    # alias maps to, so genuinely shared names can be flagged from the
    # data itself rather than guessed at.
    alias_owner_count: Dict[str, set] = {}
    for entry in company_registry:
        company_id = _slugify(entry["canonical_name"])
        for alias_text in [entry["canonical_name"], *entry.get("aliases", [])]:
            alias_owner_count.setdefault(normalize_name(alias_text), set()).add(company_id)

    for entry in company_registry:
        canonical_name = entry["canonical_name"]
        company_id = _slugify(canonical_name)
        category = entry["category"]
        ticker = entry["ticker"]
        exchange = _CATEGORY_TO_EXCHANGE.get(category, _CATEGORY_TO_EXCHANGE["stocks"])

        for alias_text in dict.fromkeys([canonical_name, *entry.get("aliases", [])]):
            normalized = normalize_name(alias_text)
            if not normalized:
                continue
            risky = (
                normalized in KNOWN_AMBIGUOUS_NAMES
                or len(alias_owner_count.get(normalized, set())) > 1
            )
            aliases.append(EntityAlias(
                entity_id=company_id,
                entity_type=EntityType.COMPANY,
                alias=alias_text,
                normalized_alias=normalized,
                alias_type=AliasType.LEGAL_NAME if alias_text == canonical_name else AliasType.DISPLAY_NAME,
                ambiguity_risk=risky,
            ))

        identifiers.append(EntityIdentifier(
            entity_id=company_id, entity_type=EntityType.COMPANY,
            identifier_type=IdentifierType.TICKER, value=ticker,
        ))
        identifiers.append(EntityIdentifier(
            entity_id=company_id, entity_type=EntityType.COMPANY,
            identifier_type=IdentifierType.EXCHANGE_TICKER,
            value=f"{exchange.exchange_id}:{ticker}",
        ))

    if include_sectors and sector_map:
        for sector_name in sorted(set(sector_map.values())):
            sector_id = _slugify(sector_name)
            aliases.append(EntityAlias(
                entity_id=sector_id, entity_type=EntityType.SECTOR,
                alias=sector_name, normalized_alias=normalize_name(sector_name),
                alias_type=AliasType.DISPLAY_NAME,
            ))

    return AliasIndex(aliases=aliases, identifiers=identifiers)
