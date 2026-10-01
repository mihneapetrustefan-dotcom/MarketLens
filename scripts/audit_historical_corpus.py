#!/usr/bin/env python
"""
scripts/audit_historical_corpus.py
--------------------------------------------------
Phase 25.9I-F.7 read-only historical corpus audit.

    python scripts/audit_historical_corpus.py
    python scripts/audit_historical_corpus.py --grain day
    python scripts/audit_historical_corpus.py --json
    python scripts/audit_historical_corpus.py --db data/marketlens_corpus_v2.db

Answers one question with evidence:

    WHY DOES CANONICAL HISTORY START WHERE IT STARTS?

It measures the stage-by-stage funnel in a single unit, classifies every
source article by what became of it, compares entity coverage either
side of the measured cutoff, and shows the time distribution so a hard
pipeline cutoff can be told apart from sparse historical coverage.

Nothing is written. The database is opened `mode=ro`. No label value is
read, so the protected horizon is untouched by construction -- it does
not appear here at all.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.adaptive.corpus_funnel import (                            # noqa: E402
    canonical_cutoff, classify_losses, coverage_by_period,
    entity_cutoff, entity_resolution_audit, extraction_audit,
    extraction_cutoff, first_hard_cutoff, fusion_audit, measure_funnel,
    observation_cutoff, price_coverage_audit, processing_times,
    record_counts, recoverable_detail,
)

UTC = timezone.utc
DEFAULT_DB = os.path.join(ROOT, "data", "marketlens.db")


def audit(db_path: str, *, grain: str = "day") -> Dict[str, object]:
    started = time.time()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cutoff = entity_cutoff(conn)
        timings = {
            "entity_resolution": cutoff.isoformat() if cutoff else None,
            "event_extraction": (extraction_cutoff(conn).isoformat()
                                 if extraction_cutoff(conn) else None),
            "canonical_event": (canonical_cutoff(conn).isoformat()
                                if canonical_cutoff(conn) else None),
            "research_observation": (observation_cutoff(conn).isoformat()
                                     if observation_cutoff(conn) else None),
        }
        funnel_all = measure_funnel(conn, period_label="all")
        pre = measure_funnel(conn, period_end=cutoff,
                             period_label="before the entity cutoff") if cutoff else None
        post = measure_funnel(conn, period_start=cutoff,
                              period_label="from the entity cutoff") if cutoff else None
        coverage = coverage_by_period(conn, grain=grain)
        result = {
            "db": db_path,
            "measured_at": datetime.now(UTC).isoformat(),
            "stage_cutoffs": timings,
            "record_counts": record_counts(conn),
            "processing_times": processing_times(conn),
            "funnel_all": funnel_all.describe(),
            "funnel_pre_cutoff": pre.describe() if pre else None,
            "funnel_post_cutoff": post.describe() if post else None,
            "loss_accounting_all": classify_losses(conn, period_label="all").describe(),
            "loss_accounting_pre": (
                classify_losses(conn, period_end=cutoff,
                                period_label="before the entity cutoff").describe()
                if cutoff else None),
            "loss_accounting_post": (
                classify_losses(conn, period_start=cutoff,
                                period_label="from the entity cutoff").describe()
                if cutoff else None),
            "recoverable": recoverable_detail(conn),
            "entity_audit": entity_resolution_audit(conn, cutoff=cutoff),
            "extraction_audit": extraction_audit(conn),
            "fusion_audit": fusion_audit(conn),
            "price_coverage": price_coverage_audit(conn),
            "coverage_by_period": coverage,
            "first_hard_cutoff": {
                "entity_resolved": first_hard_cutoff(coverage,
                                                     field="entity_resolved"),
                "event_reports": first_hard_cutoff(coverage,
                                                   field="event_reports"),
            },
            "runtime_seconds": round(time.time() - started, 3),
        }
    finally:
        conn.close()
    return result


def _print(report: Dict[str, object], *, show_periods: int) -> None:
    print("PHASE 25.9I-F.7 HISTORICAL CORPUS AUDIT (read-only)")
    print(f"  db           : {report['db']}")
    print(f"  measured     : {report['measured_at']}")
    print()

    print("  STAGE CUTOFFS (earliest real record at each stage)")
    for stage, value in report["stage_cutoffs"].items():
        print(f"    {stage:24s} {value}")
    print()

    print("  RECORD COUNTS")
    print(f"    {'table':26s} {'count':>9s}  {'unit':<16s} coverage")
    for key, info in report["record_counts"].items():
        early = str(info["earliest"])[:10] if info["earliest"] else "-"
        late = str(info["latest"])[:10] if info["latest"] else "-"
        print(f"    {key:26s} {info['count']:>9,}  {str(info['unit']):<16s} "
              f"{early} -> {late}")
    print()

    print("  PROCESSING TIMES (when each stage ran, not what it was about)")
    for key, info in report["processing_times"].items():
        print(f"    {key:44s} {str(info['earliest'])[:19]} -> "
              f"{str(info['latest'])[:19]}  "
              f"days={info['distinct_days']} elapsed={info['elapsed_seconds']}s")
    print()

    for label, key in (("ALL ARTICLES", "funnel_all"),
                       ("BEFORE THE ENTITY CUTOFF", "funnel_pre_cutoff"),
                       ("FROM THE ENTITY CUTOFF", "funnel_post_cutoff")):
        data = report.get(key)
        if not data:
            continue
        print(f"  FUNNEL -- {label} (distinct articles reaching each stage)")
        for stage in data["stages"]:
            print(f"    {stage['stage']:24s} in {stage['entering']:>8,}  "
                  f"out {stage['exiting']:>8,}  drop {stage['drop']:>8,}  "
                  f"{str(stage['drop_pct']):>6s}%")
        print(f"    reconciles={data['reconciles']}  "
              f"first material break={data['first_material_break']}")
        print()

    print("  LOSS ACCOUNTING")
    for label, key in (("all", "loss_accounting_all"),
                       ("pre-cutoff", "loss_accounting_pre"),
                       ("post-cutoff", "loss_accounting_post")):
        data = report.get(key)
        if not data:
            continue
        print(f"    [{label}] {data['total_source_articles']:,} source articles, "
              f"reconciles={data['reconciles']}, unclassified={data['unclassified']}")
        for reason, count in data["by_reason"].items():
            print(f"        {reason:30s} {count:>8,}")
    print()

    rec = report["recoverable"]
    print("  RECOVERABLE")
    print(f"    never offered to entity resolution  {rec['skipped']:>8,}")
    print(f"    of those, with mention data         {rec['skipped_with_mentions']:>8,}")
    print(f"    of those, with nothing to resolve   {rec['skipped_without_mentions']:>8,}")
    print(f"    recoverable share                   {rec['recoverable_share']}")
    print()

    ent = report["entity_audit"]
    print("  ENTITY RESOLUTION AUDIT")
    print(f"    cutoff {ent['cutoff']}")
    print(f"    {'':24s} {'pre':>12s} {'post':>12s}")
    for field in ("articles", "with_mentions", "mention_pct", "entity_linked",
                  "entity_pct", "entity_links", "distinct_companies",
                  "company_feed_articles", "distinct_sources"):
        print(f"    {field:24s} {str(ent['pre_cutoff'][field]):>12s} "
              f"{str(ent['post_cutoff'][field]):>12s}")
    print(f"    mention density comparable: {ent['mention_density_comparable']}")
    print(f"    coverage gap (pp)         : {ent['coverage_gap_pct']}")
    print()

    ext = report["extraction_audit"]
    print("  EXTRACTION AUDIT")
    for field in ("extraction_cutoff", "eligible_articles", "attempted_articles",
                  "skipped_articles", "articles_producing_events",
                  "no_event_articles", "event_reports"):
        print(f"    {field:28s} {ext[field]}")
    print(f"    by tier   {ext['by_tier']}")
    print(f"    by status {ext['by_status']}")
    print()

    fus = report["fusion_audit"]
    print("  FUSION AUDIT")
    for field in ("reports_eligible", "reports_fused", "reports_not_fused",
                  "canonical_events", "decisions", "review_cases",
                  "contradictions"):
        print(f"    {field:28s} {fus[field]}")
    print(f"    by decision state {fus['by_decision_state']}")
    print(f"    by corroboration  {fus['by_corroboration']}")
    print()

    price = report["price_coverage"]
    print(f"  PRICE COVERAGE  {str(price['earliest'])[:10]} -> "
          f"{str(price['latest'])[:10]}, {price['rows']:,} rows, "
          f"{price['instruments']} instruments")
    print()

    rows = report["coverage_by_period"]
    shown = rows[:show_periods] if show_periods else rows
    print(f"  TIME DISTRIBUTION ({len(rows)} periods, showing {len(shown)})")
    print(f"    {'period':12s} {'articles':>9s} {'entity':>8s} {'pct':>6s} "
          f"{'reports':>8s} {'canonical':>9s}")
    for row in shown:
        print(f"    {str(row['period']):12s} {row['articles']:>9,} "
              f"{row['entity_resolved']:>8,} {str(row['entity_pct']):>6s} "
              f"{row['event_reports']:>8,} {row['canonical_events']:>9,}")
    print()
    print(f"  FIRST HARD CUTOFF  entity_resolved="
          f"{report['first_hard_cutoff']['entity_resolved']}  "
          f"event_reports={report['first_hard_cutoff']['event_reports']}")
    print(f"  runtime {report['runtime_seconds']}s")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--grain", default="day", choices=("day", "month", "year"))
    parser.add_argument("--periods", type=int, default=70,
                        help="How many time-distribution rows to print (0 = all).")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: database not found: {args.db}")
        return 1

    report = audit(args.db, grain=args.grain)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print(report, show_periods=args.periods)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
