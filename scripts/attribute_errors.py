#!/usr/bin/env python3
"""
scripts/attribute_errors.py
---------------------------------
Diagnose where each measured outcome deviated, with evidence (Phase 20).

WHAT IT DOES AND WILL NOT DO
--------------------------------
It reads Phase 19's outcomes, runs eleven deterministic detectors over
each, ranks the findings by causal depth, and records the numbers that
produced each conclusion.

It changes nothing. No model, strategy, threshold, weight, risk limit
or capital figure is modified by any path in this script or the package
behind it. Phase 21 is where memory begins; learning is later still.

IT IS ALLOWED TO SAY IT DOES NOT KNOW
-----------------------------------------
NO_ERROR, EXPECTED_LOSS, UNKNOWN and INSUFFICIENT_EVIDENCE are all
first-class results. Six of the nine layers have no evidence source in
this database — sizing, risk, execution, portfolio, regime, and the
signal layer for predictions — and their detectors report exactly which
table is missing rather than returning a clean bill of health.

WHERE IT SITS
-----------------
After outcome measurement, which is itself last:

    ... generate signals -> measure outcomes -> [ attribute errors ]

    python scripts/attribute_errors.py --apply
    python scripts/attribute_errors.py --apply --recompute
    python scripts/attribute_errors.py --export data/exports/attribution.csv
    python scripts/attribute_errors.py --compare-versions v1 v2
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.attribution import analytics, api
from src.attribution.pipeline import (
    compare_versions, queue_for_review, run, save,
)
from src.data_access.attribution_schema import initialize_attribution_schema
from src.domain.attribution_models import ATTRIBUTION_METHOD_VERSION
from src.domain.outcome_models import OUTCOME_METHOD_VERSION

DEFAULT_DB = os.path.join("data", "marketlens.db")


def line(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 68 - len(title)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--method-version", default=ATTRIBUTION_METHOD_VERSION,
                        help="Bump to record a rule change as NEW rows beside "
                             "the old ones rather than over them.")
    parser.add_argument("--outcome-version", default=OUTCOME_METHOD_VERSION,
                        help="Which Phase 19 measurements to diagnose.")
    parser.add_argument("--subject-kind", default=None,
                        choices=["signal", "prediction"])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--recompute", action="store_true",
                        help="Re-diagnose subjects already attributed under "
                             "this methodology. Replaces; never appends.")
    parser.add_argument("--export", default=None, metavar="PATH",
                        help="Write the research export to CSV and exit.")
    parser.add_argument("--compare-versions", nargs=2, metavar=("LEFT", "RIGHT"),
                        help="Show where two methodologies disagree.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write. Without this it is a dry run.")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"No database at {args.db}.")
        return 1

    conn = sqlite3.connect(args.db)
    initialize_attribution_schema(conn)

    # ---- export ------------------------------------------------------
    if args.export:
        rows = api.export_csv(conn, args.export,
                              method_version=args.method_version)
        evidence_path = os.path.splitext(args.export)[0] + "_evidence.csv"
        evidence = api.export_evidence_csv(conn, evidence_path,
                                           method_version=args.method_version)
        line("EXPORT")
        print(f"  {rows:,} attribution row(s) -> {args.export}")
        print(f"  {evidence:,} evidence row(s) -> {evidence_path}")
        print("  Hypotheticals are excluded; `observability` is a column "
              "regardless.")
        conn.close()
        return 0

    # ---- version comparison -----------------------------------------
    if args.compare_versions:
        left, right = args.compare_versions
        rows = compare_versions(conn, left, right)
        line(f"WHERE {left} AND {right} DISAGREE")
        if not rows:
            print("  No subject changed its primary error type.")
        for row in rows[:40]:
            print(f"  {row['subject_id']} {row['horizon']:>4}  "
                  f"{row['left_type']} ({row['left_confidence']})"
                  f"  ->  {row['right_type']} ({row['right_confidence']})")
        if len(rows) > 40:
            print(f"  ... and {len(rows) - 40:,} more")
        print()
        print(f"  {len(rows):,} subject(s) changed conclusion. Neither version "
              f"was modified;")
        print("  both remain queryable.")
        conn.close()
        return 0

    print("=" * 72)
    print("MarketLens - error attribution")
    print("Diagnoses WHERE a deviation occurred, with the numbers behind it.")
    print("Changes no model, no strategy, no threshold, no risk, no capital.")
    if not args.apply:
        print("MODE: DRY RUN - nothing is written")
    print("=" * 72)

    started = time.time()
    attributions, reviews, report = run(
        conn, method_version=args.method_version,
        outcome_method_version=args.outcome_version,
        subject_kind=args.subject_kind, limit=args.limit,
        recompute=args.recompute)
    elapsed = time.time() - started

    data = report.as_dict()
    line("SUBJECTS")
    print(f"  outcomes considered       {data['outcomes_seen']:,}")
    print(f"  already attributed        {data['skipped_existing']:,}")
    print(f"  methodology version       {data['method_version']}"
          f"  (outcomes {args.outcome_version})")

    line("VERDICTS")
    print(f"  a deviation was found     {data['attributed']:,}")
    print(f"  no error                  {data['no_error']:,}")
    print(f"  expected loss             {data['expected_loss']:,}")
    print(f"  unknown, needs review     {data['unknown']:,}")
    print(f"  insufficient evidence     {data['insufficient_evidence']:,}")
    print(f"  total findings recorded   {data['findings']:,}"
          f"   ({elapsed:.1f}s)")
    print()
    print("  NO_ERROR and EXPECTED_LOSS are results, not gaps. A loss inside")
    print("  the normal distribution is not a mistake, and forcing every")
    print("  outcome into a fault would make this layer a machine for")
    print("  rationalising noise.")

    if data["by_type"]:
        line("FINDINGS BY TYPE")
        for name, count in sorted(data["by_type"].items(), key=lambda kv: -kv[1]):
            print(f"  {name:24s} {count:>6,}")

    if data["unjudgeable_layers"]:
        line("LAYERS THAT COULD NOT BE ASSESSED")
        for name, count in sorted(data["unjudgeable_layers"].items(),
                                  key=lambda kv: -kv[1]):
            print(f"  {count:>6,}  {name}")
        print()
        print("  These are not clean bills of health. Each names the table")
        print("  that is missing, and each starts producing findings the")
        print("  moment its input exists.")

    if not args.apply:
        line("DRY RUN")
        print(f"  {len(attributions):,} attribution(s) and {len(reviews):,} "
              f"review case(s) would be written.")
        print("  Add --apply to write them.")
        conn.close()
        return 0

    written = save(conn, attributions)
    for case in reviews:
        queue_for_review(conn, case["outcome"], case["reason"],
                         case["candidates"], case["severity"],
                         args.method_version)
    line("WRITTEN")
    print(f"  {written:,} attribution(s)")
    print(f"  {len(reviews):,} case(s) queued for human review")
    print("  Identity is (subject_kind, subject_id, horizon, method_version,")
    print("  error_type), so a second run replaces rather than adds.")

    summary = analytics.coverage(conn, method_version=args.method_version)
    line("COVERAGE")
    print(f"  subjects with a verdict    {summary['subjects']:,}")
    print(f"  assessable                 {summary['assessable']:,}"
          f"   ({(summary['coverage'] or 0):.1%})")
    print(f"  evidence rows              {summary['evidence_rows']:,}")
    print(f"  review queue open          {summary['review_open']:,}")

    line("INTEGRITY (§67 — every count must be zero)")
    problems = analytics.integrity_check(conn, method_version=args.method_version)
    for name, count in problems.items():
        flag = "" if count == 0 else "   <-- INVESTIGATE"
        print(f"  {name:38s} {count}{flag}")
    if any(problems.values()):
        print()
        print("  Integrity check FAILED. The attributions were written; the")
        print("  inconsistency above needs looking at before they are trusted.")
        conn.close()
        return 1

    print()
    print("  Next: scripts/build_dashboard.py, or --export for research.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
