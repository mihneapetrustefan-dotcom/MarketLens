"""
src/memory/api.py
-------------------------
The query surface for Trading Memory (§44), snapshots (§37), and
research export (§68).

CONVENTION
--------------
`docs/API_AUDIT.md` records that this repository has no HTTP layer, by
decision. Phases 19 and 20 implemented their routes as typed functions
over a connection; this follows, so three phases share one convention.

    GET /memory/experiences        -> list_experiences()
    GET /memory/patterns           -> list_patterns()
    GET /memory/models/{id}        -> retrieval.model_memory()
    GET /memory/signals/{id}       -> experience_detail()
    GET /memory/regimes/{id}       -> retrieval.regime_memory()
    GET /memory/events/{id}        -> retrieval.event_memory()
    GET /memory/instruments/{id}   -> retrieval.instrument_memory()
    GET /memory/search             -> retrieval.similar_experiences()

EVERY LISTING ACCEPTS `as_of`
--------------------------------
Not decoration. §71 requires that future research be able to ask for
memory as it stood, and an endpoint that could only answer "now" would
push callers to filter afterwards — which is exactly where the leak
would appear.

SNAPSHOTS ARE A RECORD, NOT A CACHE (§37)
---------------------------------------------
A snapshot answers "what did the system know at time T" by recording
the counts that were true then. It is never used to serve a query,
because a stale snapshot answering a live question would be worse than
recomputing. `memory_as_of()` always recomputes from the experience
table; snapshots exist so the growth of knowledge is itself auditable.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from src.data_access.memory_schema import initialize_memory_schema
from src.domain.memory_models import (
    MEMORY_METHOD_VERSION, MIN_PATTERN_SAMPLE, MemoryPattern,
)
from src.memory import retrieval

MAX_LIMIT = 1000

_EXPERIENCE_COLUMNS = (
    "experience_id", "memory_version", "kind", "subject_kind", "subject_id",
    "horizon", "outcome_method_version", "attribution_method_version",
    "trained_model_id", "model_status", "strategy_id", "observation_id",
    "information_cutoff", "available_at", "expected_direction",
    "expected_return", "expected_horizon", "signal_confidence",
    "signal_strength", "actual_return", "actual_direction",
    "direction_result", "mfe", "mae", "time_to_mfe_seconds", "primary_error",
    "contributing_errors", "attribution_confidence", "attribution_severity",
    "evidence_count", "context_schema_version", "context_json",
    "market_regime", "event_type", "instrument_id", "asset_class",
    "sector_id", "experience_class", "quality", "notes_json", "created_at",
)

_PATTERN_COLUMNS = (
    "pattern_id", "memory_version", "context_schema_version", "pattern_type",
    "conditions_json", "sample_size", "instrument_count", "experimental_count",
    "hits", "misses", "neutrals", "hit_rate", "mean_return", "median_return",
    "stdev_return", "mean_mfe", "mean_mae", "error_counts_json",
    "class_counts_json", "first_seen", "last_seen", "periods_json",
    "stability", "regime_breakdown_json", "contradictions_json", "quality",
    "confidence", "notes_json", "created_at",
)


def _decode(record: Dict[str, Any]) -> Dict[str, Any]:
    for key in list(record):
        if key.endswith("_json"):
            try:
                record[key[:-5]] = json.loads(record.pop(key) or "null")
            except (TypeError, ValueError):
                record[key[:-5]] = None
    return record


def list_experiences(conn: sqlite3.Connection, *,
                     as_of: Optional[str] = None,
                     kind: Optional[str] = None,
                     experience_class: Optional[str] = None,
                     quality: Optional[str] = None,
                     trained_model_id: Optional[str] = None,
                     instrument_id: Optional[str] = None,
                     event_type: Optional[str] = None,
                     memory_version: str = MEMORY_METHOD_VERSION,
                     limit: int = 100, offset: int = 0
                     ) -> List[Dict[str, Any]]:
    """`GET /memory/experiences`, point-in-time aware."""
    initialize_memory_schema(conn)
    clauses = ["memory_version = ?"]
    params: List[Any] = [memory_version]
    if as_of:
        clauses.append("available_at IS NOT NULL AND available_at <= ?")
        params.append(as_of)
    for column, value in (("kind", kind), ("experience_class", experience_class),
                          ("quality", quality),
                          ("trained_model_id", trained_model_id),
                          ("instrument_id", instrument_id),
                          ("event_type", event_type)):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    sql = (f"SELECT {', '.join(_EXPERIENCE_COLUMNS)} FROM trading_experiences "
           f"WHERE {' AND '.join(clauses)} "
           f"ORDER BY available_at DESC, experience_id LIMIT ? OFFSET ?")
    params += [max(1, min(int(limit), MAX_LIMIT)), max(0, int(offset))]
    return [_decode(dict(zip(_EXPERIENCE_COLUMNS, row)))
            for row in conn.execute(sql, params)]


def experience_detail(conn: sqlite3.Connection, experience_id: str, *,
                      memory_version: str = MEMORY_METHOD_VERSION
                      ) -> Optional[Dict[str, Any]]:
    """
    One experience, with the patterns it supports (§46).

    The reverse link matters: knowing which generalisations a single
    experience is holding up is how a reader spots a pattern resting on
    one instrument.
    """
    initialize_memory_schema(conn)
    row = conn.execute(f"""
        SELECT {', '.join(_EXPERIENCE_COLUMNS)} FROM trading_experiences
        WHERE experience_id = ? AND memory_version = ?
    """, (experience_id, memory_version)).fetchone()
    if row is None:
        return None
    record = _decode(dict(zip(_EXPERIENCE_COLUMNS, row)))
    record["supports_patterns"] = [
        {"pattern_id": p[0], "pattern_type": p[1], "sample_size": p[2],
         "quality": p[3]}
        for p in conn.execute("""
            SELECT p.pattern_id, p.pattern_type, p.sample_size, p.quality
            FROM memory_pattern_evidence e
            JOIN memory_patterns p ON p.pattern_id = e.pattern_id
                                  AND p.memory_version = e.memory_version
            WHERE e.experience_id = ? AND e.memory_version = ?
            ORDER BY p.sample_size DESC LIMIT 25
        """, (experience_id, memory_version))
    ]
    return record


def list_patterns(conn: sqlite3.Connection, *,
                  pattern_type: Optional[str] = None,
                  quality: Optional[str] = None,
                  confidence: Optional[str] = None,
                  min_sample: int = 0,
                  memory_version: str = MEMORY_METHOD_VERSION,
                  limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    """
    `GET /memory/patterns`.

    `min_sample` defaults to 0 — everything, flagged — because hiding
    weak patterns makes the record look stronger than it is. At 27 days
    of history 68% of patterns are WEAK, and that is the finding.
    """
    initialize_memory_schema(conn)
    clauses = ["memory_version = ?"]
    params: List[Any] = [memory_version]
    for column, value in (("pattern_type", pattern_type), ("quality", quality),
                          ("confidence", confidence)):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    if min_sample:
        clauses.append("sample_size >= ?")
        params.append(int(min_sample))
    sql = (f"SELECT {', '.join(_PATTERN_COLUMNS)} FROM memory_patterns "
           f"WHERE {' AND '.join(clauses)} "
           f"ORDER BY sample_size DESC, pattern_id LIMIT ? OFFSET ?")
    params += [max(1, min(int(limit), MAX_LIMIT)), max(0, int(offset))]
    return [_decode(dict(zip(_PATTERN_COLUMNS, row)))
            for row in conn.execute(sql, params)]


def pattern_detail(conn: sqlite3.Connection, pattern_id: str, *,
                   memory_version: str = MEMORY_METHOD_VERSION,
                   evidence_limit: int = 50) -> Optional[Dict[str, Any]]:
    """
    One pattern with the experiences that formed it (§47).

    Evidence is always attached. §25 forbids orphaned knowledge, and an
    endpoint that could return a pattern without its support would let a
    consumer treat a generalisation as free-standing.
    """
    initialize_memory_schema(conn)
    row = conn.execute(f"""
        SELECT {', '.join(_PATTERN_COLUMNS)} FROM memory_patterns
        WHERE pattern_id = ? AND memory_version = ?
    """, (pattern_id, memory_version)).fetchone()
    if row is None:
        return None
    record = _decode(dict(zip(_PATTERN_COLUMNS, row)))
    record["evidence"] = [
        {"experience_id": e[0], "available_at": e[1],
         "direction_result": e[2], "actual_return": e[3],
         "primary_error": e[4], "quality": e[5]}
        for e in conn.execute("""
            SELECT e.experience_id, e.available_at, x.direction_result,
                   x.actual_return, x.primary_error, x.quality
            FROM memory_pattern_evidence e
            LEFT JOIN trading_experiences x
                   ON x.experience_id = e.experience_id
                  AND x.memory_version = e.memory_version
            WHERE e.pattern_id = ? AND e.memory_version = ?
            ORDER BY e.available_at DESC LIMIT ?
        """, (pattern_id, memory_version, evidence_limit))
    ]
    record["evidence_total"] = conn.execute("""
        SELECT COUNT(*) FROM memory_pattern_evidence
        WHERE pattern_id = ? AND memory_version = ?
    """, (pattern_id, memory_version)).fetchone()[0]
    return record


def search(conn: sqlite3.Connection, context: Dict[str, Any], *,
           as_of: Optional[str] = None,
           memory_version: str = MEMORY_METHOD_VERSION) -> Dict[str, Any]:
    """`GET /memory/search` — structured similarity with provenance."""
    return retrieval.similar_experiences(
        conn, context, as_of=as_of, memory_version=memory_version).as_dict()


def summary(conn: sqlite3.Connection, *,
            as_of: Optional[str] = None,
            memory_version: str = MEMORY_METHOD_VERSION) -> Dict[str, Any]:
    """Coverage and composition, with patterns recomputed for the moment."""
    view = retrieval.memory_as_of(conn, as_of, memory_version=memory_version)
    patterns: Sequence[MemoryPattern] = view.pop("patterns", [])
    from collections import Counter
    view["pattern_quality"] = dict(Counter(p.quality.value for p in patterns))
    view["pattern_confidence"] = dict(Counter(p.confidence.value for p in patterns))
    view["stability"] = dict(Counter(p.stability for p in patterns))
    return view


def timeline(conn: sqlite3.Connection, *,
             memory_version: str = MEMORY_METHOD_VERSION) -> List[Dict[str, Any]]:
    """
    How the record accumulated, day by day (§48).

    Built from `available_at`, so it shows when knowledge BECAME
    available rather than when rows were written — which is the only
    version of this timeline that means anything.
    """
    initialize_memory_schema(conn)
    rows = []
    running = 0
    for day, count, validated, experimental in conn.execute("""
        SELECT substr(available_at, 1, 10), COUNT(*),
               SUM(CASE WHEN quality='validated' THEN 1 ELSE 0 END),
               SUM(CASE WHEN quality='experimental' THEN 1 ELSE 0 END)
        FROM trading_experiences
        WHERE memory_version = ? AND available_at IS NOT NULL
        GROUP BY 1 ORDER BY 1
    """, (memory_version,)):
        running += count
        rows.append({"day": day, "new_experiences": count,
                     "cumulative": running, "validated": validated or 0,
                     "experimental": experimental or 0})
    return rows


# ======================================================================
# Snapshots (§37)
# ======================================================================

def write_snapshot(conn: sqlite3.Connection, as_of: str, *,
                   memory_version: str = MEMORY_METHOD_VERSION,
                   now: Optional[datetime] = None) -> str:
    """
    Record what the system knew at `as_of`.

    Deterministic id from `(memory_version, as_of)`, so re-taking a
    snapshot of the same moment replaces rather than duplicates (§54).
    """
    initialize_memory_schema(conn)
    now = now or datetime.now(timezone.utc)
    view = retrieval.memory_as_of(conn, as_of, memory_version=memory_version)
    patterns: Sequence[MemoryPattern] = view.get("patterns", [])
    digest = hashlib.sha256(f"{memory_version}|{as_of}".encode()).hexdigest()[:24]
    snapshot_id = f"snap-{digest}"

    from collections import Counter
    conn.execute("""
        INSERT OR REPLACE INTO memory_snapshots (
            snapshot_id, memory_version, as_of, experience_count,
            pattern_count, validated_count, experimental_count,
            class_counts_json, error_counts_json, summary_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (snapshot_id, memory_version, as_of, view["experience_count"],
          view["pattern_count"], view["validated_count"],
          view["experimental_count"],
          json.dumps(view["class_counts"], sort_keys=True),
          json.dumps(view["error_counts"], sort_keys=True),
          json.dumps({
              "patterns_above_sample_threshold":
                  view["patterns_above_sample_threshold"],
              "pattern_quality": dict(Counter(p.quality.value for p in patterns)),
              "first_experience": view["first_experience"],
              "last_experience": view["last_experience"],
          }, sort_keys=True),
          now.isoformat()))
    conn.commit()
    return snapshot_id


def list_snapshots(conn: sqlite3.Connection, *,
                   memory_version: str = MEMORY_METHOD_VERSION
                   ) -> List[Dict[str, Any]]:
    """Every snapshot, oldest first — the growth of knowledge over time."""
    initialize_memory_schema(conn)
    keys = ("snapshot_id", "as_of", "experience_count", "pattern_count",
            "validated_count", "experimental_count", "class_counts_json",
            "summary_json", "created_at")
    return [_decode(dict(zip(keys, row))) for row in conn.execute(f"""
        SELECT {', '.join(keys)} FROM memory_snapshots
        WHERE memory_version = ? ORDER BY as_of
    """, (memory_version,))]


# ======================================================================
# Export (§68)
# ======================================================================

def export_experiences_csv(conn: sqlite3.Connection, path: str, *,
                           memory_version: str = MEMORY_METHOD_VERSION) -> int:
    """
    The experience table, flat, with every version reference intact.

    CSV rather than Parquet: pyarrow is not a dependency and this
    repository computes research numbers on the standard library
    everywhere else.
    """
    rows = list_experiences(conn, memory_version=memory_version,
                            limit=MAX_LIMIT, offset=0)
    # Page through rather than raising the ceiling: the ceiling exists
    # because an unbounded listing over a growing table is a memory
    # incident waiting for a slow week.
    collected: List[Dict[str, Any]] = []
    offset = 0
    while True:
        page = list_experiences(conn, memory_version=memory_version,
                                limit=MAX_LIMIT, offset=offset)
        if not page:
            break
        collected.extend(page)
        offset += len(page)
        if len(page) < MAX_LIMIT:
            break
    return _write_csv(path, collected)


def export_patterns_csv(conn: sqlite3.Connection, path: str, *,
                        memory_version: str = MEMORY_METHOD_VERSION) -> int:
    """Patterns with their conditions, statistics and quality states."""
    collected: List[Dict[str, Any]] = []
    offset = 0
    while True:
        page = list_patterns(conn, memory_version=memory_version,
                             limit=MAX_LIMIT, offset=offset)
        if not page:
            break
        collected.extend(page)
        offset += len(page)
        if len(page) < MAX_LIMIT:
            break
    return _write_csv(path, collected)


def export_pattern_evidence_csv(conn: sqlite3.Connection, path: str, *,
                                memory_version: str = MEMORY_METHOD_VERSION) -> int:
    """
    The pattern→experience links (§25, §68).

    Exported separately so a downstream consumer can rebuild any pattern
    from its evidence and check the aggregation rather than trusting it.
    """
    initialize_memory_schema(conn)
    keys = ("pattern_id", "experience_id", "available_at")
    rows = [dict(zip(keys, row)) for row in conn.execute("""
        SELECT pattern_id, experience_id, available_at
        FROM memory_pattern_evidence WHERE memory_version = ?
        ORDER BY pattern_id, available_at
    """, (memory_version,))]
    return _write_csv(path, rows)


def export_json(conn: sqlite3.Connection, path: str, *,
                memory_version: str = MEMORY_METHOD_VERSION,
                as_of: Optional[str] = None) -> int:
    """
    A single JSON document: summary, patterns, and the version stamps.

    Every version reference travels with it — memory, context, outcome
    and attribution — so a consumer can tell which methodology produced
    what it is reading (§35, §68).
    """
    from src.domain.attribution_models import ATTRIBUTION_METHOD_VERSION
    from src.domain.memory_models import CONTEXT_SCHEMA_VERSION
    from src.domain.outcome_models import OUTCOME_METHOD_VERSION

    document = {
        "memory_version": memory_version,
        "context_schema_version": CONTEXT_SCHEMA_VERSION,
        "outcome_method_version": OUTCOME_METHOD_VERSION,
        "attribution_method_version": ATTRIBUTION_METHOD_VERSION,
        "as_of": as_of,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary(conn, as_of=as_of, memory_version=memory_version),
        "patterns": list_patterns(conn, memory_version=memory_version,
                                  limit=MAX_LIMIT),
    }
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, default=str)
    return len(document["patterns"])


def _write_csv(path: str, rows: Sequence[Dict[str, Any]]) -> int:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    if not rows:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write("")
        return 0
    fieldnames = list(rows[0].keys())
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames,
                                extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: (json.dumps(v, default=str)
                                 if isinstance(v, (dict, list)) else v)
                             for k, v in row.items()})
    return len(rows)


def integrity_check(conn: sqlite3.Connection, *,
                    memory_version: str = MEMORY_METHOD_VERSION
                    ) -> Dict[str, int]:
    """
    §73, as a query rather than a promise. Every count must be zero.

    Run by the CLI after each pass. "Every memory item has provenance"
    is the sort of claim that is true until one day it quietly is not.
    """
    initialize_memory_schema(conn)

    def scalar(sql: str, params: tuple = ()) -> int:
        try:
            return conn.execute(sql, params).fetchone()[0]
        except sqlite3.OperationalError:
            return 0

    return {
        "experiences_without_an_outcome": scalar("""
            SELECT COUNT(*) FROM trading_experiences x
            WHERE x.memory_version = ? AND NOT EXISTS (
                SELECT 1 FROM outcome_measurements o
                WHERE o.subject_kind = x.subject_kind
                  AND o.subject_id = x.subject_id AND o.horizon = x.horizon)
        """, (memory_version,)),
        "patterns_without_evidence": scalar("""
            SELECT COUNT(*) FROM memory_patterns p
            WHERE p.memory_version = ? AND NOT EXISTS (
                SELECT 1 FROM memory_pattern_evidence e
                WHERE e.pattern_id = p.pattern_id
                  AND e.memory_version = p.memory_version)
        """, (memory_version,)),
        "evidence_without_a_pattern": scalar("""
            SELECT COUNT(*) FROM memory_pattern_evidence e
            WHERE e.memory_version = ? AND NOT EXISTS (
                SELECT 1 FROM memory_patterns p
                WHERE p.pattern_id = e.pattern_id
                  AND p.memory_version = e.memory_version)
        """, (memory_version,)),
        "evidence_without_an_experience": scalar("""
            SELECT COUNT(*) FROM memory_pattern_evidence e
            WHERE e.memory_version = ? AND NOT EXISTS (
                SELECT 1 FROM trading_experiences x
                WHERE x.experience_id = e.experience_id
                  AND x.memory_version = e.memory_version)
        """, (memory_version,)),
        "usable_experiences_without_available_at": scalar("""
            SELECT COUNT(*) FROM trading_experiences
            WHERE memory_version = ? AND quality IN ('validated','experimental')
              AND available_at IS NULL
        """, (memory_version,)),
        "experiences_available_before_their_cutoff": scalar("""
            SELECT COUNT(*) FROM trading_experiences
            WHERE memory_version = ? AND available_at IS NOT NULL
              AND information_cutoff IS NOT NULL
              AND available_at < information_cutoff
        """, (memory_version,)),
        "invalid_quality": scalar("""
            SELECT COUNT(*) FROM trading_experiences
            WHERE memory_version = ? AND quality NOT IN
                  ('validated','experimental','incomplete','superseded')
        """, (memory_version,)),
        "patterns_claiming_confidence_on_a_small_sample": scalar(f"""
            SELECT COUNT(*) FROM memory_patterns
            WHERE memory_version = ? AND sample_size < {MIN_PATTERN_SAMPLE}
              AND confidence IN ('high','medium')
        """, (memory_version,)),
    }
