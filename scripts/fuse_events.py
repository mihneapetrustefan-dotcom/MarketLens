"""
Runs Phase 5 (Event Fusion) over the event reports persisted by
Phase 4, and stores the canonical events it produces.

PIPELINE THIS SCRIPT CONNECTS
------------------------------
    events (Phase 4 = EVENT REPORTS, one per article-claim)
        -> EventReport wrappers
        -> FusionEngine.process_batch  (blocking -> scoring ->
           decision -> merge -> corroboration -> contradiction ->
           timeline)
        -> canonical_events + decisions + contradictions + timeline
           + review cases

WHY THE INPUT IS REPORT-LEVEL
------------------------------
Fusion counts INDEPENDENT SOURCES. If Phase 4 had already collapsed
three articles about one acquisition into a single row, fusion would
see one report and could never count three sources. populate_events.py
therefore writes one row per (article, event_type), and the collapsing
happens here — which is the whole point of the phase.

SOURCE CATEGORY IS DERIVED, NOT INVENTED
-----------------------------------------
SourceCategory feeds quality scoring. It is mapped from the article's
source name through an explicit, inspectable table below. Anything not
in that table is UNKNOWN — never guessed into a flattering category.

LINEAGE IS LEFT UNKNOWN, ON PURPOSE
------------------------------------
EventReport.is_independent() returns True only for a lineage
explicitly marked ORIGINAL_REPORT. The legacy corpus carries no
syndication information, so this script attaches NO lineage at all.
The consequence is stated plainly rather than papered over:
independent_source_count will be 0 for every canonical event, and
corroboration states will be conservative. Inventing lineage to make
the numbers look better would be fabricating provenance.

SAFETY
------
- Schema creation is CREATE TABLE IF NOT EXISTS.
- `events` and its child tables are read-only inputs.
- --dry-run (the default) runs the whole fusion in memory and reports
  the outcome without writing.
- Re-running is safe: decision ids are derived from report ids, and
  every write is INSERT OR REPLACE on a stable primary key.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from typing import List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
SRC = os.path.join(REPO_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from src.data_access.event_repository import EventRepository, initialize_event_schema
from src.data_access.fusion_schema import initialize_fusion_schema
from src.domain.fusion_models import EventReport, SourceCategory
from src.fusion.engine import FusionEngine

DEFAULT_DB = os.path.join(REPO_ROOT, "data", "marketlens.db")

# Measured on the real corpus; see the report at the end of this run.
ESTIMATED_BYTES_PER_CANONICAL = 2600

#: Explicit, inspectable mapping. Substring match on the article's
#: source name, first hit wins. Absent -> UNKNOWN.
SOURCE_CATEGORY_RULES = [
    ("reuters", SourceCategory.MAJOR_FINANCIAL_PRESS),
    ("bloomberg", SourceCategory.MAJOR_FINANCIAL_PRESS),
    ("financial times", SourceCategory.MAJOR_FINANCIAL_PRESS),
    ("wall street journal", SourceCategory.MAJOR_FINANCIAL_PRESS),
    ("cnbc", SourceCategory.MAJOR_FINANCIAL_PRESS),
    ("marketwatch", SourceCategory.MAJOR_FINANCIAL_PRESS),
    ("barron", SourceCategory.MAJOR_FINANCIAL_PRESS),
    ("investing.com", SourceCategory.SPECIALIZED_PRESS),
    ("seeking alpha", SourceCategory.ANALYST_COMMENTARY),
    ("benzinga", SourceCategory.SPECIALIZED_PRESS),
    ("techcrunch", SourceCategory.SPECIALIZED_PRESS),
    ("sec.gov", SourceCategory.REGULATORY_FILING),
]


def categorize_source(source_name: Optional[str]) -> SourceCategory:
    """Map a source name to a category, or UNKNOWN if unrecognized."""
    if not source_name:
        return SourceCategory.UNKNOWN
    lowered = source_name.lower()
    for needle, category in SOURCE_CATEGORY_RULES:
        if needle in lowered:
            return category
    return SourceCategory.UNKNOWN


def decision_id_for(report_id: str) -> str:
    """Stable decision id, so re-running rewrites rather than appends."""
    return f"fd-{hashlib.sha1(report_id.encode('utf-8')).hexdigest()[:16]}"


def canonical_id_for(report_ids: List[str]) -> str:
    """
    Stable id for a canonical event, derived from the reports it fuses.

    FusionEngine mints canonical_event_id with uuid4() and starts from
    an empty in-memory state on every run, so persisting its ids
    directly would create a fresh duplicate set each time. The
    occurrence itself, however, is identified by the set of reports
    that describe it, so hashing that set gives a stable key.

    CONSEQUENCE, STATED PLAINLY: if a later run attaches an additional
    report to a group, the group's id changes. That is why persist()
    prunes canonical rows absent from the current run — the fusion
    output is a derived view over `events`, and a half-updated view
    would be worse than a recomputed one. The underlying reports in
    `events` are never touched.
    """
    joined = "|".join(sorted(report_ids))
    return f"ce-{hashlib.sha1(joined.encode('utf-8')).hexdigest()[:16]}"


def load_reports(conn: sqlite3.Connection, limit: Optional[int]) -> List[EventReport]:
    """Rehydrate Phase 4 rows as EventReports, newest first."""
    repo = EventRepository(conn)
    conn.row_factory = sqlite3.Row
    # Secondary sort on event_id: publication_time has ties, and fusion
    # grouping depends on processing order, so an unstable sort would
    # make the whole run non-reproducible.
    sql = "SELECT event_id FROM events ORDER BY publication_time DESC, event_id"
    params: List[object] = []
    if limit:
        sql += " LIMIT ?"
        params.append(limit)

    reports = []
    for row in conn.execute(sql, params).fetchall():
        event = repo.get(row["event_id"])
        if event is None:
            continue
        source_name = event.evidence[0].source_name if event.evidence else None
        reports.append(EventReport(
            report_id=event.event_id,
            structured_event=event,
            source_category=categorize_source(source_name),
            # lineage deliberately omitted — see module docstring.
        ))
    return reports


def persist(conn: sqlite3.Connection, engine: FusionEngine) -> int:
    """
    Write the fusion output. Returns the number of stale canonical rows
    pruned.

    Every engine-minted uuid is remapped to a deterministic id first,
    so the whole write is idempotent.
    """
    id_map = {
        event.canonical_event_id: canonical_id_for(event.report_ids)
        for event in engine.canonical_events.values()
    }
    current_ids = set(id_map.values())

    # Prune canonical rows from earlier runs that this run no longer
    # produces. Reports in `events` are never touched.
    existing = {r[0] for r in conn.execute("SELECT canonical_event_id FROM canonical_events")}
    stale = existing - current_ids
    for stale_id in stale:
        for table in ("canonical_events", "canonical_event_participants",
                      "canonical_event_reports", "fusion_contradictions",
                      "fusion_timeline", "fusion_review_cases"):
            conn.execute(f"DELETE FROM {table} WHERE canonical_event_id = ?", (stale_id,))

    for event in engine.canonical_events.values():
        cid = id_map[event.canonical_event_id]
        geo = event.geography
        conn.execute("""
            INSERT OR REPLACE INTO canonical_events (
                canonical_event_id, event_type, category, subtype, title, geography_json,
                attributes_json, first_reported_at, last_updated_at, event_time,
                lifecycle_state, corroboration_state, independent_source_count,
                total_report_count, has_contradictions, quality_confidence, fingerprint
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            cid, event.event_type.value, event.category.value,
            event.subtype, event.title,
            json.dumps(geo.__dict__) if geo else None,
            json.dumps({k: str(v) for k, v in event.attributes.items()}),
            _iso(event.first_reported_at), _iso(event.last_updated_at), _iso(event.event_time),
            event.lifecycle_state.value, event.corroboration_state.value,
            event.independent_source_count, event.total_report_count,
            int(event.has_contradictions), event.quality_confidence, event.fingerprint,
        ))
        for p in event.participants:
            conn.execute("""
                INSERT OR REPLACE INTO canonical_event_participants
                (canonical_event_id, entity_id, role, entity_type, resolution_confidence)
                VALUES (?,?,?,?,?)
            """, (cid, p.entity_id, p.role.value, p.entity_type, p.resolution_confidence))
        for report_id in event.report_ids:
            conn.execute(
                "INSERT OR IGNORE INTO canonical_event_reports (canonical_event_id, report_id) VALUES (?,?)",
                (cid, report_id))

    for d in engine.decisions:
        conn.execute("""
            INSERT OR REPLACE INTO fusion_decisions
            (decision_id, report_id, canonical_event_id, state, score, method, reason, candidate_count, decided_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            decision_id_for(d.report_id), d.report_id,
            id_map.get(d.canonical_event_id), d.state.value,
            d.score.score() if d.score else None,
            d.method.value if d.method else None,
            d.reason, d.candidate_count, _iso(d.decided_at),
        ))

    for c in engine.contradictions:
        conn.execute("""
            INSERT OR REPLACE INTO fusion_contradictions
            (contradiction_id, canonical_event_id, contradiction_type, field_name, description, detected_at)
            VALUES (?,?,?,?,?,?)
        """, (c.contradiction_id, id_map.get(c.canonical_event_id), c.contradiction_type.value,
              c.field_name, c.description, _iso(c.detected_at)))

    for t in engine.timeline:
        # Timeline entry ids are uuid-minted per run; derive a stable id
        # from the canonical event, entry type and report so re-running
        # rewrites the same rows.
        stable_entry = f"tl-{hashlib.sha1(f'{id_map.get(t.canonical_event_id)}|{t.entry_type.value}|{t.report_id}'.encode()).hexdigest()[:16]}"
        conn.execute("""
            INSERT OR REPLACE INTO fusion_timeline
            (entry_id, canonical_event_id, entry_type, occurred_at, description, report_id, old_value, new_value)
            VALUES (?,?,?,?,?,?,?,?)
        """, (stable_entry, id_map.get(t.canonical_event_id), t.entry_type.value, _iso(t.occurred_at),
              t.description, t.report_id, t.old_value, t.new_value))

    for r in engine.review_cases:
        stable_review = f"rev-{hashlib.sha1(f'{r.report_id}|{r.reason.value}'.encode()).hexdigest()[:16]}"
        conn.execute("""
            INSERT OR REPLACE INTO fusion_review_cases
            (review_id, reason, report_id, canonical_event_id, description, created_at, resolved)
            VALUES (?,?,?,?,?,?,?)
        """, (stable_review, r.reason.value, r.report_id, id_map.get(r.canonical_event_id),
              r.description, _iso(r.created_at), 0))

    conn.commit()
    return len(stale)


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, default=None,
                        help="Max reports to fuse, newest first.")
    parser.add_argument("--max-db-mb", type=float, default=1400.0,
                        help="Refuse to write if the projected size exceeds this "
                             "(MB). See populate_events.py -- the old 96 MB "
                             "default protected a 100 MB git limit the database "
                             "no longer lives under.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write. Without this the script is a dry run.")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"EROARE: baza nu exista: {args.db}")
        return 1

    size_before = os.path.getsize(args.db)
    print(f"Baza          : {args.db}")
    print(f"Marime curenta: {size_before / 1024 / 1024:.2f} MB")

    conn = sqlite3.connect(args.db)
    initialize_event_schema(conn)
    initialize_fusion_schema(conn)

    reports = load_reports(conn, args.limit)
    print(f"Rapoarte de eveniment incarcate: {len(reports):,}")

    cat_counts = Counter(r.source_category.value for r in reports)
    print("Categorii de sursa:")
    for c, n in cat_counts.most_common():
        print(f"  {c:26s} {n:>6,}")

    engine = FusionEngine()
    events, stats = engine.process_batch(reports)

    corr_counts = Counter(e.corroboration_state.value for e in engine.canonical_events.values())
    life_counts = Counter(e.lifecycle_state.value for e in engine.canonical_events.values())

    print()
    print(f"Evenimente canonice        : {len(engine.canonical_events):,}")
    print(f"Rata de colapsare          : {len(reports):,} rapoarte -> {len(engine.canonical_events):,} evenimente")
    print(f"Decizii de fuziune         : {len(engine.decisions):,}")
    print(f"Contradictii detectate     : {len(engine.contradictions):,}")
    print(f"Cazuri pentru revizuire    : {len(engine.review_cases):,}")
    print(f"Intrari cronologie         : {len(engine.timeline):,}")
    print("Stare de corroborare:")
    for c, n in corr_counts.most_common():
        print(f"  {c:26s} {n:>6,}")
    print("Stare ciclu de viata:")
    for c, n in life_counts.most_common():
        print(f"  {c:26s} {n:>6,}")

    projected = size_before + len(engine.canonical_events) * ESTIMATED_BYTES_PER_CANONICAL
    print()
    print(f"Proiectie marime : {projected / 1024 / 1024:.2f} MB (prag {args.max_db_mb:.1f} MB)")

    if projected / 1024 / 1024 > args.max_db_mb:
        print("REFUZ: proiectia depaseste pragul. Nimic nu a fost scris.")
        conn.close()
        return 2

    if not args.apply:
        print("DRY RUN — nimic nu a fost scris. Adaugati --apply pentru a scrie.")
        conn.close()
        return 0

    pruned = persist(conn, engine)
    conn.close()
    size_after = os.path.getsize(args.db)
    print(f"SCRIS: {len(engine.canonical_events):,} evenimente canonice"
          + (f", {pruned:,} randuri invechite curatate" if pruned else ""))
    print(f"Marime dupa: {size_after / 1024 / 1024:.2f} MB "
          f"(+{(size_after - size_before) / 1024 / 1024:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
