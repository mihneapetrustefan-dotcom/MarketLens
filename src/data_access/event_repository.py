"""
src/data_access/event_repository.py
----------------------------------------
Event storage + the internal Event API (Phase 4, spec §17, §23, §24).

Additive: creates its own tables, never touches the existing
`articles` / `recommendations` / `portfolio_snapshots` tables, nor
Phase 1-3's.

INDEXES: one per named query the Event API supports — by entity, by
type, by publication time, by fingerprint (duplicate detection), by
article. Low-selectivity fields (status, category) are not indexed,
the same discipline applied in Phase 2.
"""

import json
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any

from src.domain.event_models import (
    StructuredEvent, EventEvidence, EventConfidence, EventParticipant, EventGeography,
    EventStatus, ExtractionTier, ParticipationRole, ArticleEventLink, ArticleEventRelation,
    EventCorrection,
)
from src.events.taxonomy import EventType, EventCategory

logger = logging.getLogger("marketlens.events.repository")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError):
        return None


def initialize_event_schema(conn: sqlite3.Connection) -> None:
    """Create the Phase 4 event tables and indexes if absent. Idempotent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            event_id             TEXT PRIMARY KEY,
            event_type           TEXT NOT NULL,
            category             TEXT NOT NULL,
            subtype              TEXT,
            title                TEXT,
            description          TEXT,
            geography_json       TEXT,
            event_time           TEXT,
            publication_time     TEXT,
            ingestion_time       TEXT,
            detection_time       TEXT,
            confidence_json      TEXT NOT NULL DEFAULT '{}',
            confidence_score     REAL,
            status               TEXT NOT NULL DEFAULT 'detected',
            extraction_tier      TEXT NOT NULL DEFAULT 'deterministic_rule',
            attributes_json      TEXT NOT NULL DEFAULT '{}',
            fingerprint          TEXT,
            supersedes_event_id  TEXT,
            version              INTEGER NOT NULL DEFAULT 1,
            created_at           TEXT,
            updated_at           TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS event_participants (
            event_id               TEXT NOT NULL,
            entity_id              TEXT NOT NULL,
            role                   TEXT NOT NULL,
            entity_type            TEXT NOT NULL DEFAULT 'company',
            resolution_confidence  REAL,
            PRIMARY KEY (event_id, entity_id, role)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS event_evidence (
            event_id       TEXT NOT NULL,
            article_id     TEXT NOT NULL,
            source_id      TEXT,
            source_name    TEXT,
            published_at   TEXT,
            excerpt        TEXT,
            is_syndicated  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (event_id, article_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS article_event_links (
            article_id  TEXT NOT NULL,
            event_id    TEXT NOT NULL,
            relation    TEXT NOT NULL DEFAULT 'references',
            created_at  TEXT,
            PRIMARY KEY (article_id, event_id, relation)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS event_instruments (
            event_id       TEXT NOT NULL,
            instrument_id  TEXT NOT NULL,
            PRIMARY KEY (event_id, instrument_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS event_sectors (
            event_id   TEXT NOT NULL,
            sector_id  TEXT NOT NULL,
            PRIMARY KEY (event_id, sector_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS event_corrections (
            correction_id  TEXT PRIMARY KEY,
            event_id       TEXT NOT NULL,
            field_name     TEXT NOT NULL,
            old_value      TEXT,
            new_value      TEXT,
            corrected_by   TEXT NOT NULL,
            reason         TEXT,
            corrected_at   TEXT
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_publication_time ON events(publication_time DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_fingerprint ON events(fingerprint)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_event_participants_entity ON event_participants(entity_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_event_evidence_article ON event_evidence(article_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_article_event_links_event ON article_event_links(event_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_event_instruments_instrument ON event_instruments(instrument_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_event_sectors_sector ON event_sectors(sector_id)")
    conn.commit()


class EventRepository:
    """Storage and query access for structured events."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._conn.row_factory = sqlite3.Row

    # ---------------- write ----------------

    def save(self, event: StructuredEvent) -> None:
        """
        Persist an event and all of its child records.

        Refuses to store an event that fails validate() — chiefly, one
        with no evidence (spec §9: an untraceable event must never
        exist). The refusal raises rather than silently dropping, so a
        pipeline bug producing evidence-free events is loud.
        """
        problem = event.validate()
        if problem:
            raise ValueError(f"refusing to store invalid event {event.event_id}: {problem}")

        geo = event.geography
        self._conn.execute("""
            INSERT OR REPLACE INTO events (
                event_id, event_type, category, subtype, title, description, geography_json,
                event_time, publication_time, ingestion_time, detection_time,
                confidence_json, confidence_score, status, extraction_tier,
                attributes_json, fingerprint, supersedes_event_id, version, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            event.event_id, event.event_type.value, event.category.value, event.subtype,
            event.title, event.description,
            json.dumps(geo.__dict__) if geo else None,
            _iso(event.event_time), _iso(event.publication_time),
            _iso(event.ingestion_time), _iso(event.detection_time),
            json.dumps(event.confidence.explain()), event.confidence.score(),
            event.status.value, event.extraction_tier.value,
            json.dumps(event.attributes), event.fingerprint, event.supersedes_event_id,
            event.version, _iso(event.created_at), _iso(event.updated_at),
        ))

        for p in event.participants:
            self._conn.execute(
                "INSERT OR REPLACE INTO event_participants (event_id, entity_id, role, entity_type, resolution_confidence) VALUES (?,?,?,?,?)",
                (event.event_id, p.entity_id, p.role.value, p.entity_type, p.resolution_confidence),
            )
        for e in event.evidence:
            self._conn.execute(
                "INSERT OR REPLACE INTO event_evidence (event_id, article_id, source_id, source_name, published_at, excerpt, is_syndicated) VALUES (?,?,?,?,?,?,?)",
                (event.event_id, e.article_id, e.source_id, e.source_name, _iso(e.published_at), e.excerpt, int(e.is_syndicated)),
            )
        for instrument_id in event.instrument_ids:
            self._conn.execute("INSERT OR IGNORE INTO event_instruments (event_id, instrument_id) VALUES (?,?)",
                                (event.event_id, instrument_id))
        for sector_id in event.sector_ids:
            self._conn.execute("INSERT OR IGNORE INTO event_sectors (event_id, sector_id) VALUES (?,?)",
                                (event.event_id, sector_id))
        self._conn.commit()

    def link_article(self, link: ArticleEventLink) -> None:
        """Record an explicit article -> event relationship (spec §15)."""
        self._conn.execute(
            "INSERT OR REPLACE INTO article_event_links (article_id, event_id, relation, created_at) VALUES (?,?,?,?)",
            (link.article_id, link.event_id, link.relation.value, _iso(link.created_at or datetime.now(timezone.utc))),
        )
        self._conn.commit()

    def add_evidence(self, event_id: str, evidence: EventEvidence) -> None:
        """Attach an additional supporting article to an existing event — how a second report joins one event rather than creating a duplicate."""
        self._conn.execute(
            "INSERT OR REPLACE INTO event_evidence (event_id, article_id, source_id, source_name, published_at, excerpt, is_syndicated) VALUES (?,?,?,?,?,?,?)",
            (event_id, evidence.article_id, evidence.source_id, evidence.source_name,
             _iso(evidence.published_at), evidence.excerpt, int(evidence.is_syndicated)),
        )
        self._conn.commit()

    def supersede(self, old_event_id: str, new_event: StructuredEvent) -> None:
        """
        Record an evolved event (spec §14): the new version points back
        at the old one, and the old one is marked SUPERSEDED rather
        than deleted — historical states are never destroyed.
        """
        new_event.supersedes_event_id = old_event_id
        previous = self.get(old_event_id)
        new_event.version = (previous.version + 1) if previous else 1
        new_event.updated_at = datetime.now(timezone.utc)
        self.save(new_event)
        self._conn.execute("UPDATE events SET status = ?, updated_at = ? WHERE event_id = ?",
                            (EventStatus.SUPERSEDED.value, _iso(datetime.now(timezone.utc)), old_event_id))
        self._conn.commit()

    def update_status(self, event_id: str, status: EventStatus) -> None:
        self._conn.execute("UPDATE events SET status = ?, updated_at = ? WHERE event_id = ?",
                            (status.value, _iso(datetime.now(timezone.utc)), event_id))
        self._conn.commit()

    def save_correction(self, correction: EventCorrection) -> None:
        """Persist an auditable human correction (spec §27) — stored alongside, never overwriting the original extraction."""
        self._conn.execute("""
            INSERT OR REPLACE INTO event_corrections
            (correction_id, event_id, field_name, old_value, new_value, corrected_by, reason, corrected_at)
            VALUES (?,?,?,?,?,?,?,?)
        """, (
            correction.correction_id, correction.event_id, correction.field_name,
            correction.old_value, correction.new_value, correction.corrected_by,
            correction.reason, _iso(correction.corrected_at or datetime.now(timezone.utc)),
        ))
        self._conn.commit()

    def get_corrections(self, event_id: str) -> List[EventCorrection]:
        rows = self._conn.execute("SELECT * FROM event_corrections WHERE event_id = ? ORDER BY corrected_at", (event_id,)).fetchall()
        return [EventCorrection(
            correction_id=r["correction_id"], event_id=r["event_id"], field_name=r["field_name"],
            old_value=r["old_value"], new_value=r["new_value"], corrected_by=r["corrected_by"],
            reason=r["reason"], corrected_at=_parse(r["corrected_at"]),
        ) for r in rows]

    # ---------------- read ----------------

    def _hydrate(self, row: sqlite3.Row) -> StructuredEvent:
        event_id = row["event_id"]

        participants = [EventParticipant(
            entity_id=r["entity_id"], role=ParticipationRole(r["role"]),
            entity_type=r["entity_type"], resolution_confidence=r["resolution_confidence"],
        ) for r in self._conn.execute("SELECT * FROM event_participants WHERE event_id = ?", (event_id,)).fetchall()]

        evidence = [EventEvidence(
            article_id=r["article_id"], source_id=r["source_id"], source_name=r["source_name"],
            published_at=_parse(r["published_at"]), excerpt=r["excerpt"], is_syndicated=bool(r["is_syndicated"]),
        ) for r in self._conn.execute("SELECT * FROM event_evidence WHERE event_id = ?", (event_id,)).fetchall()]

        instruments = [r["instrument_id"] for r in self._conn.execute(
            "SELECT instrument_id FROM event_instruments WHERE event_id = ?", (event_id,)).fetchall()]
        sectors = [r["sector_id"] for r in self._conn.execute(
            "SELECT sector_id FROM event_sectors WHERE event_id = ?", (event_id,)).fetchall()]

        confidence_data = json.loads(row["confidence_json"] or "{}").get("components", {})
        confidence = EventConfidence(**{name: data["value"] for name, data in confidence_data.items()}) \
            if confidence_data else EventConfidence()

        geo_data = json.loads(row["geography_json"]) if row["geography_json"] else None

        return StructuredEvent(
            event_id=event_id, event_type=EventType(row["event_type"]),
            category=EventCategory(row["category"]), subtype=row["subtype"],
            title=row["title"] or "", description=row["description"] or "",
            participants=participants, instrument_ids=instruments, sector_ids=sectors,
            geography=EventGeography(**geo_data) if geo_data else None,
            event_time=_parse(row["event_time"]), publication_time=_parse(row["publication_time"]),
            ingestion_time=_parse(row["ingestion_time"]), detection_time=_parse(row["detection_time"]),
            evidence=evidence, confidence=confidence, status=EventStatus(row["status"]),
            extraction_tier=ExtractionTier(row["extraction_tier"]),
            attributes=json.loads(row["attributes_json"] or "{}"),
            fingerprint=row["fingerprint"], supersedes_event_id=row["supersedes_event_id"],
            version=row["version"], created_at=_parse(row["created_at"]), updated_at=_parse(row["updated_at"]),
        )

    def get(self, event_id: str) -> Optional[StructuredEvent]:
        row = self._conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
        return self._hydrate(row) if row else None

    def find_candidate_events(self, event: StructuredEvent, window_days: int = 7) -> List[StructuredEvent]:
        """
        BOUNDED candidate set for duplicate detection — same event type,
        published within +/- window_days. Keeps duplicate checking
        tractable at scale (never a full-table scan).
        """
        moment = event.event_time or event.publication_time
        if not moment:
            return []
        rows = self._conn.execute(
            "SELECT * FROM events WHERE event_type = ? AND publication_time BETWEEN ? AND ? AND event_id != ?",
            (event.event_type.value, _iso(moment - timedelta(days=window_days)),
             _iso(moment + timedelta(days=window_days)), event.event_id),
        ).fetchall()
        return [self._hydrate(r) for r in rows]

    def query(
        self,
        entity_id: Optional[str] = None,
        instrument_id: Optional[str] = None,
        sector_id: Optional[str] = None,
        event_type: Optional[EventType] = None,
        category: Optional[EventCategory] = None,
        country: Optional[str] = None,
        published_after: Optional[datetime] = None,
        published_before: Optional[datetime] = None,
        min_confidence: Optional[float] = None,
        status: Optional[EventStatus] = None,
        article_id: Optional[str] = None,
        include_superseded: bool = False,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """
        The internal Event API (spec §24). Every filter is server-side;
        results are cursor-paginated, never unbounded.

        Returns:
            {"events": [...], "next_cursor": str|None, "has_more": bool}
        """
        limit = max(1, min(limit, 500))
        where, params, joins = [], [], []

        if not include_superseded:
            where.append("e.status != ?")
            params.append(EventStatus.SUPERSEDED.value)
        if entity_id:
            joins.append("JOIN event_participants p ON p.event_id = e.event_id")
            where.append("p.entity_id = ?")
            params.append(entity_id)
        if instrument_id:
            joins.append("JOIN event_instruments i ON i.event_id = e.event_id")
            where.append("i.instrument_id = ?")
            params.append(instrument_id)
        if sector_id:
            joins.append("JOIN event_sectors s ON s.event_id = e.event_id")
            where.append("s.sector_id = ?")
            params.append(sector_id)
        if article_id:
            joins.append("JOIN event_evidence ev ON ev.event_id = e.event_id")
            where.append("ev.article_id = ?")
            params.append(article_id)
        if event_type:
            where.append("e.event_type = ?")
            params.append(event_type.value)
        if category:
            where.append("e.category = ?")
            params.append(category.value)
        if country:
            where.append("e.geography_json LIKE ?")
            params.append(f'%"country": "{country}"%')
        if published_after:
            where.append("e.publication_time >= ?")
            params.append(_iso(published_after))
        if published_before:
            where.append("e.publication_time <= ?")
            params.append(_iso(published_before))
        if min_confidence is not None:
            where.append("e.confidence_score >= ?")
            params.append(min_confidence)
        if status:
            where.append("e.status = ?")
            params.append(status.value)
        if cursor:
            where.append("e.publication_time < ?")
            params.append(cursor)

        clause = f"WHERE {' AND '.join(where)}" if where else ""
        sql = f"SELECT DISTINCT e.* FROM events e {' '.join(joins)} {clause} ORDER BY e.publication_time DESC LIMIT ?"
        rows = self._conn.execute(sql, (*params, limit + 1)).fetchall()

        has_more = len(rows) > limit
        rows = rows[:limit]
        events = [self._hydrate(r) for r in rows]
        next_cursor = _iso(events[-1].publication_time) if (events and has_more) else None
        return {"events": events, "next_cursor": next_cursor, "has_more": has_more}

    def get_articles_for_event(self, event_id: str) -> List[str]:
        rows = self._conn.execute("SELECT article_id FROM event_evidence WHERE event_id = ?", (event_id,)).fetchall()
        return [r["article_id"] for r in rows]

    def count(self, include_superseded: bool = False) -> int:
        if include_superseded:
            return self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return self._conn.execute("SELECT COUNT(*) FROM events WHERE status != ?",
                                   (EventStatus.SUPERSEDED.value,)).fetchone()[0]
