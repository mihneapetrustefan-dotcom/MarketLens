"""
src/providers/base.py
--------------------------
Provider abstraction for MarketLens's Phase 1 data foundation.

RESPONSIBILITY:
Define the shape every external data source must be normalized
through, so provider-specific response formats never spread past this
one boundary:

    Provider -> Adapter -> Normalized Data -> Canonical Model

WHAT THIS PHASE DELIBERATELY DOES NOT DO: this defines the INTERFACE
only. It does not touch or wrap the existing live connectors
(finnhub_news_collector.py, alpha_vantage_news_collector.py,
fred_connector.py, market_data.py) — those keep working exactly as
they do today. registry_adapter.py (this package) is the one CONCRETE
adapter built in this phase, and it normalizes STATIC REGISTRY DATA
(company_registry.py, sector_registry.py, sources.py), not a live API
call — proving the abstraction against real, existing data without
touching anything that runs in production today.
"""

from abc import ABC, abstractmethod
from typing import Any, List, TypeVar, Generic

T = TypeVar("T")


class SourceAdapter(ABC, Generic[T]):
    """
    Base class for a provider adapter: takes whatever shape a source
    of raw data produces (a registry list, an API JSON response, an
    RSS feed entry) and returns a list of canonical domain model
    instances. Never mutates the input; never talks to a database.
    """

    @abstractmethod
    def normalize(self, raw_records: List[Any]) -> List[T]:
        """Convert a batch of raw, provider-specific records into canonical model instances."""
        raise NotImplementedError

    def normalize_one(self, raw_record: Any) -> T:
        """Convenience wrapper for a single record, built on top of normalize()."""
        result = self.normalize([raw_record])
        if not result:
            raise ValueError("normalize() returned no records for a single input record")
        return result[0]
