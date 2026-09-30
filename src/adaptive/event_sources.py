"""
src/adaptive/event_sources.py
-------------------------------------------
Reading canonical events, as of a moment (Phase 25.9I-F).

WHY A READER AND NOT A PIPELINE
-----------------------------------
The event pipeline already exists and is not touched: news ingestion,
normalisation, entity resolution, extraction, fusion clustering and
corroboration all run today and produced the 581 canonical events this
module reads. What did NOT exist, audited 2026-09-30, is a way to read
them back -- `src/data_access/fusion_schema.py` has an initializer and
no repository class, and `EventRepository` reads the pre-fusion
`events` table, not `canonical_events`.

So this module is a reader. It creates no event, resolves no entity,
scores no corroboration and clusters nothing.

EVENT != ARTICLE, AND THE DATA ALREADY KNOWS IT
---------------------------------------------------
846 reports collapsed into 581 canonical events; 442 events carry one
report and 139 carry between two and six. Deduplication is fusion's job
and it has already happened, so five articles about one acquisition
arrive here as ONE `canonical_event_id`. This module never counts
articles.

THE INSTRUMENT PATH, MEASURED
---------------------------------
`event_instruments` holds 0 rows, so the mapping is derived by join:

    canonical_event_participants.entity_id
      -> companies.company_id
      -> securities.company_id -> securities.security_id
      -> instruments.security_id -> instruments.instrument_id

Coverage measured 2026-09-30: 581 of 581 canonical events resolve to at
least one instrument through their PRIMARY participant; 244 distinct
instruments; 389 of 389 companies carry a sector. A participant that
does not resolve yields nothing -- never a ticker-substring guess.

AS-OF, ENFORCED IN SQL
--------------------------
Every query bounds `first_reported_at <= as_of`. A later article cannot
strengthen an earlier assessment because the row is not returned at
all, and `EventEvidenceBundle` refuses a timestamp after its own
`as_of` as a second, structural guard.

WHAT THIS CORPUS CANNOT TELL US, AND SAYS SO
------------------------------------------------
Measured over all 581 events:

    event_time                 NULL for every one   -> occurrence time
                                                       is inferred from
                                                       first report
    independent_source_count   0 for every one      -> independence was
                                                       never established
    has_contradictions         false for every one
    lifecycle_state            'reported' for every one

None of that is patched over. `source_independence_known` is False, so
`is_independently_corroborated` cannot be True however many reports
arrive, and MISSING_EVENT_TIME is recorded as a data-integrity concern.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from src.domain.event_position_models import (
    DataIntegrityConcern, EventEvidenceBundle,
)
from src.domain.fusion_models import CorroborationState

#: Scheduled event types. A calendar of actual scheduled dates does not
#: exist in this repository, so an event is treated as scheduled only by
#: TYPE, and the caller supplies the scheduled time. Nothing here
#: invents a date.
SCHEDULED_EVENT_TYPES = frozenset({
    "earnings", "guidance", "dividend",
    "interest_rate_decision", "central_bank_decision",
    "inflation", "employment", "gdp",
})


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class InstrumentResolution:
    """One entity's resolved instruments, with the path that produced them."""
    entity_id: str
    instrument_ids: Tuple[str, ...] = ()
    sector_id: Optional[str] = None
    role: str = ""
    resolution_confidence: Optional[float] = None
    method: str = "participant->company->security->instrument"

    @property
    def is_resolved(self) -> bool:
        return bool(self.instrument_ids)

    @property
    def is_ambiguous(self) -> bool:
        """Several instruments for one entity: a choice nobody has made."""
        return len(self.instrument_ids) > 1


class EventInstrumentResolver:
    """
    entity -> instruments, through canonical tables only.

    Built once from a read-only connection and then answers from memory,
    so it cannot re-read the database mid-assessment and cannot be
    handed to something that would write through it.
    """

    def __init__(self, by_entity: Dict[str, InstrumentResolution],
                 sectors: Optional[Dict[str, str]] = None):
        self._by_entity = dict(by_entity)
        self._sectors = dict(sectors or {})

    @classmethod
    def from_connection(cls, conn: sqlite3.Connection) -> "EventInstrumentResolver":
        by_entity: Dict[str, InstrumentResolution] = {}
        sectors: Dict[str, str] = {}
        try:
            rows = conn.execute(
                "SELECT c.company_id, c.sector_id, i.instrument_id "
                "FROM companies c "
                "LEFT JOIN securities s ON s.company_id = c.company_id "
                "LEFT JOIN instruments i ON i.security_id = s.security_id"
            ).fetchall()
        except sqlite3.DatabaseError:
            return cls({}, {})
        collected: Dict[str, List[str]] = {}
        for company_id, sector_id, instrument_id in rows:
            key = str(company_id)
            if sector_id:
                sectors[key] = str(sector_id)
            if instrument_id:
                collected.setdefault(key, []).append(str(instrument_id))
        for entity_id, instruments in collected.items():
            by_entity[entity_id] = InstrumentResolution(
                entity_id=entity_id,
                instrument_ids=tuple(sorted(set(instruments))),
                sector_id=sectors.get(entity_id))
        # Entities with a sector but no instrument still resolve to a
        # SECTOR relation, so they are kept rather than dropped.
        for entity_id, sector_id in sectors.items():
            by_entity.setdefault(entity_id, InstrumentResolution(
                entity_id=entity_id, sector_id=sector_id))
        return cls(by_entity, sectors)

    def resolve(self, entity_id: str) -> InstrumentResolution:
        return self._by_entity.get(str(entity_id)) or InstrumentResolution(
            entity_id=str(entity_id), method="unresolved")

    def sector_of(self, entity_id: str) -> Optional[str]:
        return self._sectors.get(str(entity_id))

    def instruments_in_sector(self, sector_id: str) -> Tuple[str, ...]:
        found: List[str] = []
        for entity_id, resolution in self._by_entity.items():
            if resolution.sector_id == str(sector_id):
                found.extend(resolution.instrument_ids)
        return tuple(sorted(set(found)))

    def __len__(self) -> int:
        return len(self._by_entity)

    def coverage(self) -> Dict[str, object]:
        resolved = [r for r in self._by_entity.values() if r.is_resolved]
        return {
            "entities": len(self._by_entity),
            "entities_with_instruments": len(resolved),
            "distinct_instruments": len({i for r in resolved
                                         for i in r.instrument_ids}),
            "ambiguous_entities": sum(1 for r in resolved if r.is_ambiguous),
            "entities_with_sector": sum(1 for r in self._by_entity.values()
                                        if r.sector_id),
        }


def revision_key_for(bundle_fields: Sequence[object]) -> str:
    """
    A content-derived key for "this version of this event".

    `canonical_events` has no `version` column, so a revision is
    identified by the fields whose change would MATTER: lifecycle,
    corroboration, report count, contradictions and last update. A
    re-crawl that changes nothing produces the same key and therefore no
    second reaction; a rumour becoming a confirmed filing changes the
    key and may re-trigger.
    """
    import hashlib
    raw = "|".join("" if value is None else str(value)
                   for value in bundle_fields)
    return "rev-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class CanonicalEventReader:
    """
    Read-only access to `canonical_events`, bounded by `as_of`.

    Deterministic ordering (spec section 48), defined once here and used
    everywhere:

        1. known occurrence time  (event_time, else first_reported_at)
        2. first_reported_at
        3. canonical_event_id     (tie-break, so equal timestamps never
                                   depend on insertion order)

    Database row order is never relied upon.
    """

    def __init__(self, conn: sqlite3.Connection,
                 resolver: Optional[EventInstrumentResolver] = None):
        self.conn = conn
        self.resolver = resolver or EventInstrumentResolver.from_connection(conn)

    # ---------------- reading ----------------

    def _bundle(self, row: Sequence[object], as_of: datetime,
                first_seen_at: Optional[datetime] = None) -> EventEvidenceBundle:
        (canonical_event_id, event_type, category, title, subtype,
         first_reported_at, last_updated_at, event_time, lifecycle_state,
         corroboration_state, independent_source_count, total_report_count,
         has_contradictions, quality_confidence) = row

        occurred = _parse(event_time)
        reported = _parse(first_reported_at)
        updated = _parse(last_updated_at)

        concerns: List[DataIntegrityConcern] = []
        if occurred is None:
            concerns.append(DataIntegrityConcern.MISSING_EVENT_TIME)
        if reported and occurred and reported < occurred:
            concerns.append(DataIntegrityConcern.TIMESTAMP_CONFLICT)

        entity_ids, primary, instruments, sector_ids = self._participants(
            str(canonical_event_id))
        if not instruments:
            concerns.append(DataIntegrityConcern.UNRESOLVED_ENTITY)

        try:
            corroboration = CorroborationState(str(corroboration_state or "").lower())
        except ValueError:
            corroboration = CorroborationState.UNCONFIRMED

        # Independence is only KNOWN when the pipeline actually
        # established it. A stored 0 in a corpus where every row is 0
        # means "never determined", not "checked and found none".
        independence_known = bool(independent_source_count)

        revision = revision_key_for((
            canonical_event_id, lifecycle_state, corroboration_state,
            total_report_count, independent_source_count, has_contradictions,
            subtype, last_updated_at))

        return EventEvidenceBundle(
            canonical_event_id=str(canonical_event_id),
            event_type=str(event_type or ""),
            event_category=str(category or ""),
            as_of=as_of,
            title=str(title or ""),
            subtype=str(subtype or ""),
            lifecycle_state=str(lifecycle_state or ""),
            corroboration_state=corroboration,
            total_report_count=int(total_report_count or 0),
            independent_source_count=int(independent_source_count or 0),
            source_independence_known=independence_known,
            has_contradictions=bool(has_contradictions),
            quality_confidence=(float(quality_confidence)
                                if quality_confidence is not None else None),
            event_time=occurred,
            first_reported_at=reported,
            last_updated_at=updated if (updated and updated <= as_of) else None,
            first_seen_at=first_seen_at or reported,
            is_scheduled=str(event_type or "").lower() in SCHEDULED_EVENT_TYPES,
            revision_key=revision,
            entity_ids=entity_ids,
            primary_entity_id=primary,
            instrument_ids=instruments,
            sector_ids=sector_ids,
            integrity_concerns=tuple(concerns))

    def _participants(self, canonical_event_id: str
                      ) -> Tuple[Tuple[str, ...], Optional[str],
                                 Tuple[str, ...], Tuple[str, ...]]:
        try:
            rows = self.conn.execute(
                "SELECT entity_id, role FROM canonical_event_participants "
                "WHERE canonical_event_id = ? ORDER BY role, entity_id",
                (canonical_event_id,)).fetchall()
        except sqlite3.DatabaseError:
            return (), None, (), ()
        entity_ids: List[str] = []
        primary: Optional[str] = None
        instruments: List[str] = []
        sectors: List[str] = []
        for entity_id, role in rows:
            entity_ids.append(str(entity_id))
            if str(role).lower() == "primary" and primary is None:
                primary = str(entity_id)
            resolution = self.resolver.resolve(str(entity_id))
            instruments.extend(resolution.instrument_ids)
            if resolution.sector_id:
                sectors.append(resolution.sector_id)
        return (tuple(entity_ids), primary, tuple(sorted(set(instruments))),
                tuple(sorted(set(sectors))))

    _SELECT = (
        "SELECT canonical_event_id, event_type, category, title, subtype, "
        "first_reported_at, last_updated_at, event_time, lifecycle_state, "
        "corroboration_state, independent_source_count, total_report_count, "
        "has_contradictions, quality_confidence FROM canonical_events ")

    def get(self, canonical_event_id: str, *,
            as_of: datetime) -> Optional[EventEvidenceBundle]:
        """
        One event as it was known at `as_of`, or None.

        The `first_reported_at <= as_of` bound is in the WHERE clause,
        so an event that had not been reported yet is not merely
        filtered later -- it does not exist for this query.
        """
        row = self.conn.execute(
            self._SELECT + "WHERE canonical_event_id = ? "
            "AND (first_reported_at IS NULL OR first_reported_at <= ?)",
            (str(canonical_event_id), as_of.isoformat())).fetchone()
        return self._bundle(row, as_of) if row else None

    def events_as_of(self, as_of: datetime, *,
                     instrument_ids: Optional[Sequence[str]] = None,
                     since: Optional[datetime] = None,
                     limit: int = 200) -> Tuple[EventEvidenceBundle, ...]:
        """
        Events visible at `as_of`, newest first, deterministically ordered.

        `instrument_ids` filters AFTER resolution rather than in SQL,
        because the instrument link is a join through companies and
        securities rather than a column.
        """
        params: List[object] = [as_of.isoformat()]
        clause = "WHERE (first_reported_at IS NULL OR first_reported_at <= ?) "
        if since is not None:
            clause += "AND first_reported_at >= ? "
            params.append(since.isoformat())
        rows = self.conn.execute(
            self._SELECT + clause
            + "ORDER BY COALESCE(event_time, first_reported_at) DESC, "
              "first_reported_at DESC, canonical_event_id ASC "
              "LIMIT ?", params + [int(limit) * 4]).fetchall()

        wanted = {str(i) for i in instrument_ids} if instrument_ids else None
        bundles: List[EventEvidenceBundle] = []
        for row in rows:
            bundle = self._bundle(row, as_of)
            if wanted is not None and not (set(bundle.instrument_ids) & wanted):
                continue
            bundles.append(bundle)
            if len(bundles) >= limit:
                break
        return order_events(bundles)

    def coverage(self, as_of: datetime) -> Dict[str, object]:
        """Evidence-completeness diagnostics over the visible corpus."""
        rows = self.conn.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN event_time IS NOT NULL THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN independent_source_count > 0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN total_report_count > 1 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN quality_confidence IS NOT NULL THEN 1 ELSE 0 END) "
            "FROM canonical_events WHERE first_reported_at IS NULL "
            "OR first_reported_at <= ?", (as_of.isoformat(),)).fetchone()
        total = int(rows[0] or 0)
        return {
            "events_visible": total,
            "with_event_time": int(rows[1] or 0),
            "with_established_independence": int(rows[2] or 0),
            "multi_report": int(rows[3] or 0),
            "with_quality_confidence": int(rows[4] or 0),
            "instrument_resolution": self.resolver.coverage(),
        }


def order_events(bundles: Iterable[EventEvidenceBundle]
                 ) -> Tuple[EventEvidenceBundle, ...]:
    """
    Deterministic ordering (spec section 48). Insertion order is never used.

    Sorted by known occurrence time, then first report, then canonical
    id. Events with no timestamp at all sort last rather than first: an
    undated event is not the oldest event, it is the least placeable
    one, and putting it at the front would let it lead a sequence it has
    no claim to.
    """
    far_future = datetime.max.replace(tzinfo=None)

    def key(bundle: EventEvidenceBundle):
        occurred = bundle.known_occurrence_at
        reported = bundle.first_reported_at
        return (0 if occurred else 1,
                occurred.replace(tzinfo=None) if occurred else far_future,
                0 if reported else 1,
                reported.replace(tzinfo=None) if reported else far_future,
                bundle.canonical_event_id)

    return tuple(sorted(bundles, key=key))
