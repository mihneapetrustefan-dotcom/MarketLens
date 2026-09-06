#!/usr/bin/env python3
"""
scripts/build_memory.py
-----------------------------
Turn outcomes and attributions into structured experience (Phase 21).

    outcome (19) + attribution (20) + decision-time context
        -> experience -> patterns -> snapshot

WHAT IT DOES NOT DO
-----------------------
It changes no model, strategy, threshold, feature, risk limit, position
size, execution setting or capital figure. Memory is a record; learning
is a later phase, and §63 lists all eight of those as forbidden here.

IT IS ALLOWED TO SAY IT DOES NOT KNOW
-----------------------------------------
Most patterns in this database are WEAK, because the record is 27 days
deep and a regularity needs more than that. That is reported rather
than smoothed over: §64 asks for fewer high-quality memories over many
low-quality ones, and a WEAK pattern honestly labelled is worth more
than a confident one that is wrong.

POINT-IN-TIME
-----------------
`--as-of` restricts everything to experience that was KNOWABLE at that
moment — the outcome window had closed. Not when the row was written.
That is the property every later learning phase depends on.

    python scripts/build_memory.py --apply
    python scripts/build_memory.py --apply --since 2026-09-01
    python scripts/build_memory.py --as-of 2026-08-20
    python scripts/build_memory.py --snapshot 2026-08-20 --apply
    python scripts/build_memory.py --export data/exports/memory
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.data_access.memory_schema import initialize_memory_schema
from src.domain.attribution_models import ATTRIBUTION_METHOD_VERSION
from src.domain.memory_models import (
    CONTEXT_SCHEMA_VERSION, MEMORY_METHOD_VERSION, MIN_PATTERN_SAMPLE,
)
from src.domain.outcome_models import OUTCOME_METHOD_VERSION
from src.memory import api, patterns as pattern_layer, retrieval
from src.memory.experience import build_all as build_experiences
from src.memory.experience import save as save_experiences

DEFAULT_DB = os.path.join("data", "marketlens.db")


def line(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 68 - len(title)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--memory-version", default=MEMORY_METHOD_VERSION,
                        help="Bump to record a change of meaning as NEW rows "
                             "beside the old ones.")
    parser.add_argument("--outcome-version", default=OUTCOME_METHOD_VERSION)
    parser.add_argument("--attribution-version", default=ATTRIBUTION_METHOD_VERSION)
    parser.add_argument("--since", default=None, metavar="ISO_DATE",
                        help="Incremental: only outcomes whose window closed "
                             "after this. Patterns are still rebuilt over the "
                             "whole record, because an aggregate over part of "
                             "it would be wrong.")
    parser.add_argument("--as-of", default=None, metavar="ISO_DATE",
                        help="Report memory as it stood at this moment. "
                             "Read-only.")
    parser.add_argument("--snapshot", default=None, metavar="ISO_DATE",
                        help="Record what the system knew at this moment.")
    parser.add_argument("--export", default=None, metavar="DIR",
                        help="Write CSV and JSON research exports here.")
    parser.add_argument("--rebuild", action="store_true",
                        help="Rebuild every experience, ignoring --since.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write. Without this it is a dry run.")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"No database at {args.db}.")
        return 1

    conn = sqlite3.connect(args.db)
    initialize_memory_schema(conn)

    # ---- read-only: memory as of a moment ---------------------------
    if args.as_of and not args.apply and not args.snapshot:
        view = api.summary(conn, as_of=args.as_of,
                           memory_version=args.memory_version)
        line(f"MEMORY AS OF {args.as_of}")
        print(f"  experiences knowable       {view['experience_count']:,}")
        print(f"    validated                {view['validated_count']:,}")
        print(f"    experimental             {view['experimental_count']:,}")
        print(f"  patterns (recomputed)      {view['pattern_count']:,}")
        print(f"  above the sample threshold {view['patterns_above_sample_threshold']:,}")
        print(f"  first experience           {view['first_experience'] or '—'}")
        print(f"  last experience            {view['last_experience'] or '—'}")
        print()
        print(f"  {view['note']}")
        conn.close()
        return 0

    # ---- export ------------------------------------------------------
    if args.export:
        os.makedirs(args.export, exist_ok=True)
        experiences = api.export_experiences_csv(
            conn, os.path.join(args.export, "experiences.csv"),
            memory_version=args.memory_version)
        found = api.export_patterns_csv(
            conn, os.path.join(args.export, "patterns.csv"),
            memory_version=args.memory_version)
        evidence = api.export_pattern_evidence_csv(
            conn, os.path.join(args.export, "pattern_evidence.csv"),
            memory_version=args.memory_version)
        api.export_json(conn, os.path.join(args.export, "memory.json"),
                        memory_version=args.memory_version, as_of=args.as_of)
        line("EXPORT")
        print(f"  {experiences:,} experience(s)   -> experiences.csv")
        print(f"  {found:,} pattern(s)      -> patterns.csv")
        print(f"  {evidence:,} evidence link(s) -> pattern_evidence.csv")
        print(f"  summary + patterns        -> memory.json")
        print()
        print("  Every version stamp travels with the export: memory,")
        print("  context, outcome and attribution.")
        conn.close()
        return 0

    print("=" * 72)
    print("MarketLens - trading memory")
    print("Records what happened, under what conditions, and how strong the")
    print("evidence was. Changes no model, strategy, threshold, risk or capital.")
    if not args.apply:
        print("MODE: DRY RUN - nothing is written")
    print("=" * 72)

    since = None if args.rebuild else args.since
    started = time.time()
    experiences = build_experiences(
        conn, memory_version=args.memory_version,
        outcome_version=args.outcome_version,
        attribution_version=args.attribution_version, since=since)
    build_seconds = time.time() - started

    from collections import Counter
    quality = Counter(e.quality.value for e in experiences)
    classes = Counter(e.experience_class.value for e in experiences)

    line("EXPERIENCE")
    print(f"  built                      {len(experiences):,}"
          f"   ({build_seconds:.1f}s)")
    print(f"  validated                  {quality.get('validated', 0):,}")
    print(f"  experimental               {quality.get('experimental', 0):,}")
    print(f"  incomplete                 {quality.get('incomplete', 0):,}")
    print(f"  versions                   memory {args.memory_version} · "
          f"context {CONTEXT_SCHEMA_VERSION} · outcome {args.outcome_version} "
          f"· attribution {args.attribution_version}")
    print()
    print("  Experimental experience is kept and never pooled with production.")
    print("  Incomplete experience is kept too: knowing something could not be")
    print("  measured is worth remembering, and it never enters a")
    print("  point-in-time result.")

    line("CLASSIFICATION")
    for name, count in classes.most_common():
        print(f"  {name:24s} {count:>6,}")

    if not args.apply:
        line("DRY RUN")
        print(f"  {len(experiences):,} experience(s) would be written.")
        print("  Add --apply to write them, then patterns are built.")
        conn.close()
        return 0

    save_experiences(conn, experiences)

    started = time.time()
    found, evidence = pattern_layer.build_all(
        conn, memory_version=args.memory_version)
    available = {row["experience_id"]: row["available_at"]
                 for row in pattern_layer.load_experiences(
                     conn, memory_version=args.memory_version)}
    pattern_layer.save(conn, found, evidence,
                       memory_version=args.memory_version,
                       available_at=available)
    pattern_seconds = time.time() - started

    quality_counts = Counter(p.quality.value for p in found)
    confidence_counts = Counter(p.confidence.value for p in found)
    contradictions = pattern_layer.find_contradictions(found)

    line("PATTERNS")
    print(f"  built                      {len(found):,}"
          f"   ({pattern_seconds:.1f}s)")
    for name, count in quality_counts.most_common():
        print(f"  {name:24s} {count:>6,}")
    print()
    print(f"  confidence: " + ", ".join(f"{k}={v:,}" for k, v
                                        in confidence_counts.most_common()))
    print(f"  contradictions surfaced    {len(contradictions):,}")
    print()
    print(f"  A pattern under {MIN_PATTERN_SAMPLE} experiences is WEAK and quotes no")
    print("  rate. Most patterns here are weak, because the record is short.")
    print("  Contradictions are kept visible rather than resolved: which")
    print("  pattern generalises is a research question, not a tie-break.")

    if args.snapshot:
        snapshot_id = api.write_snapshot(conn, args.snapshot,
                                         memory_version=args.memory_version)
        line("SNAPSHOT")
        print(f"  {snapshot_id} recorded for {args.snapshot}")

    line("INTEGRITY (§73 — every count must be zero)")
    problems = api.integrity_check(conn, memory_version=args.memory_version)
    for name, count in problems.items():
        flag = "" if count == 0 else "   <-- INVESTIGATE"
        print(f"  {name:46s} {count}{flag}")
    if any(problems.values()):
        print()
        print("  Integrity check FAILED. Memory was written; the")
        print("  inconsistency above needs looking at before it is trusted.")
        conn.close()
        return 1

    line("POINT-IN-TIME CHECK")
    view = api.summary(conn, memory_version=args.memory_version)
    print(f"  experiences now knowable   {view['experience_count']:,}")
    print(f"  first / last               {(view['first_experience'] or '—')[:10]}"
          f"  ..  {(view['last_experience'] or '—')[:10]}")
    print()
    print("  memory_as_of(T) recomputes patterns from the experience visible")
    print("  at T. A stored pattern was aggregated over the whole record and")
    print("  would carry later evidence inside its averages.")

    print()
    print("  Next: scripts/build_dashboard.py, or --export for research.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
