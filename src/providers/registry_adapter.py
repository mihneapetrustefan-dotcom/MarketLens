"""
src/providers/registry_adapter.py
--------------------------------------
Normalizes the EXISTING, unchanged registries (company_registry.py,
sector_registry.py, sources.py, source_credibility.py) into Phase 1
canonical domain models.

RESPONSIBILITY:
This is the concrete proof that the new foundation holds real,
existing data — not a rewrite of the registries themselves. It reads
company_registry.COMPANY_REGISTRY, sector_registry.COMPANY_SECTOR_MAP,
sources.RSS_FEEDS, and source_credibility.SOURCE_TIERS exactly as they
are today and produces canonical Sector / Company / Security /
Instrument / Exchange / NewsSource instances.

DESIGN DECISIONS:
- IDs are DETERMINISTIC, derived from the existing canonical_name /
  sector name / source name (lowercased, non-alphanumerics replaced
  with "-") — so re-running the adapter twice produces the SAME ids,
  which is required for the migration script (see
  scripts/migrate_registries_to_canonical.py) to be safely re-runnable
  without creating duplicates.
- Every company_registry.py entry becomes exactly one Security (its
  "common stock" or "cryptocurrency" claim) and exactly one Instrument
  (its existing ticker, on an Exchange derived from its "category").
  This is a faithful 1:1 migration of what the OLD model could
  express — Phase 1 does not yet split any company into multiple real
  Securities (e.g. dual listings), since the old registry has no data
  to support that; the CAPABILITY exists in the model (see
  domain/models.py), population of a second Security for the same
  Company is future work.
- Exchange is inferred from category ("bvb" -> BVB exchange; "stocks"
  -> a single generic "US_AND_INTL" placeholder exchange, since
  company_registry.py does not currently record which of NYSE/NASDAQ/
  other exchange a given US-listed company trades on; "crypto" -> a
  placeholder "CRYPTO" pseudo-exchange). This is an HONEST
  simplification, documented rather than hidden — see the phase's own
  "if a complete migration cannot safely happen, document what remains"
  allowance. Refining real per-company exchange assignment is future
  work, not fabricated here.
"""

import re
from typing import List, Dict, Any, Tuple

from src.domain.models import Company, Security, Instrument, Exchange, Sector, NewsSource
from src.domain.enums import AssetClass, InstrumentType, SourceType
from src.providers.base import SourceAdapter


def _slugify(name: str) -> str:
    """Deterministic id derivation: lowercase, non-alphanumerics -> '-', collapse repeats, strip edges."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower())
    return slug.strip("-")


# Category (as used today in company_registry.py / ticker_registry.py)
# -> the placeholder Exchange this Phase 1 migration assigns it to.
# See the module docstring for why this is an honest simplification,
# not a claim of real per-company exchange data.
_CATEGORY_TO_EXCHANGE: Dict[str, Exchange] = {
    "bvb": Exchange(exchange_id="BVB", name="Bursa de Valori Bucuresti", country="RO"),
    "stocks": Exchange(exchange_id="US_AND_INTL", name="US & International (unspecified)", country="US"),
    "crypto": Exchange(exchange_id="CRYPTO", name="Cryptocurrency (unspecified venue)", country="GLOBAL"),
}

_CATEGORY_TO_ASSET_CLASS: Dict[str, AssetClass] = {
    "bvb": AssetClass.BVB,
    "stocks": AssetClass.STOCK,
    "crypto": AssetClass.CRYPTO,
}

_CATEGORY_TO_INSTRUMENT_TYPE: Dict[str, InstrumentType] = {
    "bvb": InstrumentType.COMMON_STOCK,
    "stocks": InstrumentType.COMMON_STOCK,
    "crypto": InstrumentType.CRYPTOCURRENCY,
}


class SectorRegistryAdapter(SourceAdapter[Sector]):
    """Normalizes sector_registry.py's COMPANY_SECTOR_MAP (a dict of company -> sector name) into unique canonical Sectors."""

    def normalize(self, raw_records: List[Any]) -> List[Sector]:
        """
        Args:
            raw_records: a list of sector NAME strings (e.g. the
                distinct values of sector_registry.COMPANY_SECTOR_MAP)
                — the caller is responsible for de-duplicating the
                source dict's values before calling this, exactly as
                the migration script does.
        """
        seen = set()
        sectors = []
        for name in raw_records:
            if name in seen:
                continue
            seen.add(name)
            sectors.append(Sector(sector_id=_slugify(name), name=name))
        return sectors


class CompanyRegistryAdapter(SourceAdapter[Tuple[Company, Security, Instrument, Exchange]]):
    """
    Normalizes one company_registry.py entry (plus its sector, from
    sector_registry.py) into a (Company, Security, Instrument,
    Exchange) tuple — see the module docstring for why these 4 objects
    come from one old-style registry row.
    """

    def __init__(self, sector_map: Dict[str, str]):
        """
        Args:
            sector_map: sector_registry.COMPANY_SECTOR_MAP, unchanged —
                canonical_name -> sector name.
        """
        self.sector_map = sector_map

    def normalize(self, raw_records: List[Dict[str, Any]]) -> List[Tuple[Company, Security, Instrument, Exchange]]:
        """
        Args:
            raw_records: entries exactly as they appear in
                company_registry.COMPANY_REGISTRY (each a dict with
                canonical_name/aliases/ticker/category).
        """
        results = []
        for entry in raw_records:
            canonical_name = entry["canonical_name"]
            category = entry["category"]
            ticker = entry["ticker"]

            sector_name = self.sector_map.get(canonical_name)
            sector_id = _slugify(sector_name) if sector_name else None

            company_id = _slugify(canonical_name)
            company = Company(
                company_id=company_id,
                canonical_name=canonical_name,
                aliases=list(entry.get("aliases", [])),
                sector_id=sector_id,
            )

            security_id = f"{company_id}-common"
            security = Security(
                security_id=security_id,
                company_id=company_id,
                instrument_type=_CATEGORY_TO_INSTRUMENT_TYPE.get(category, InstrumentType.COMMON_STOCK),
            )

            exchange = _CATEGORY_TO_EXCHANGE.get(category, _CATEGORY_TO_EXCHANGE["stocks"])
            instrument = Instrument(
                instrument_id=f"{exchange.exchange_id.lower()}-{ticker.lower()}",
                security_id=security_id,
                exchange_id=exchange.exchange_id,
                ticker=ticker,
                asset_class=_CATEGORY_TO_ASSET_CLASS.get(category, AssetClass.STOCK),
            )

            results.append((company, security, instrument, exchange))
        return results


class NewsSourceRegistryAdapter(SourceAdapter[NewsSource]):
    """Normalizes sources.py's RSS_FEEDS entries, enriched with source_credibility.py's tier map, into canonical NewsSource records."""

    def __init__(self, tier_map: Dict[str, str]):
        """
        Args:
            tier_map: source_credibility.SOURCE_TIERS, unchanged —
                source name -> tier string ("official",
                "wire_and_major_press", "specialized_or_aggregator").
        """
        self.tier_map = tier_map

    _TIER_STRING_TO_ENUM = {
        "official": SourceType.OFFICIAL,
        "wire_and_major_press": SourceType.WIRE_OR_MAJOR_PRESS,
        "specialized_or_aggregator": SourceType.SPECIALIZED_OR_AGGREGATOR,
    }

    def normalize(self, raw_records: List[Dict[str, Any]]) -> List[NewsSource]:
        """
        Args:
            raw_records: entries exactly as they appear in
                sources.RSS_FEEDS (each a dict with name/url/category).
        """
        results = []
        for entry in raw_records:
            name = entry["name"]
            tier_string = self.tier_map.get(name, "unclassified")
            source_type = self._TIER_STRING_TO_ENUM.get(tier_string, SourceType.UNCLASSIFIED)
            results.append(NewsSource(
                source_id=_slugify(name),
                name=name,
                source_type=source_type,
                url=entry.get("url"),
                active=True,
            ))
        return results
