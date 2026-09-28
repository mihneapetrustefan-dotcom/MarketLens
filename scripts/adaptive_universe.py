"""
scripts/adaptive_universe.py
-----------------------------------------
What the dynamic selector WOULD choose (Phase 25.9I-B). Read-only.

    python scripts/adaptive_universe.py --as-of 2026-08-27T13:25:00+00:00
    python scripts/adaptive_universe.py --as-of ... --shadow
    python scripts/adaptive_universe.py --as-of ... --explain us_and_intl-nvda
    python scripts/adaptive_universe.py --as-of ... --persist --db ...

DRY RUN IS THE DEFAULT, AND THE DEFAULT IS THE POINT
--------------------------------------------------------
Nothing is written unless `--persist` is passed, and even then only the
five `adaptive_*` tables are touched. No capture universe is changed, no
subscription is opened, no order is built. The operational capture
system continues to use `config/capture_universe_v1.json` exactly as
before, because nothing here is wired into it.

The `adaptive_universe_enabled` flag is REPORTED, never required: this
command is a diagnostic, so it must work with the flag off, which is
every current deployment.

`--shadow` compares the dynamic selection with the fixed capture
universe and reports overlap, additions, drops and reasons. It makes no
claim about which is better -- that question needs future returns, and
using them here would be the lookahead this phase forbids.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.adaptive import flags as adaptive_flags                  # noqa: E402
from src.adaptive.admission import AdmissionConfig                # noqa: E402
from src.adaptive.eligibility import EligibilityConfig            # noqa: E402
from src.adaptive.scoring import ScoringConfig, explain           # noqa: E402
from src.adaptive.selector import (                               # noqa: E402
    SessionUniverseSelector, compare_with_fixed,
)
from src.adaptive.universe import (                               # noqa: E402
    DEFAULT_ARTIFACT, MarketHistory, ParentUniverse, ParentUniverseError,
)

DEFAULT_DB = os.path.join(ROOT, "data", "marketlens.db")
CAPTURE_UNIVERSE = os.path.join(ROOT, "config", "capture_universe_v1.json")


def fixed_universe_ids(path: str = CAPTURE_UNIVERSE):
    """The operational capture universe, read without being modified."""
    with open(path, encoding="utf-8") as handle:
        definition = json.load(handle)
    return [m["instrument_id"] for m in definition.get("instruments", ())
            if not m["instrument_id"].startswith("benchmark-")]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--parent", default=DEFAULT_ARTIFACT)
    parser.add_argument("--max-capacity", type=int, default=None)
    parser.add_argument("--pass-threshold", type=float, default=None)
    parser.add_argument("--max-per-sector", type=int, default=None)
    parser.add_argument("--max-data-age-days", type=float, default=None)
    parser.add_argument("--explain", default=None,
                        help="instrument_id whose score breakdown to print")
    parser.add_argument("--shadow", action="store_true",
                        help="compare with the fixed capture universe")
    parser.add_argument("--persist", action="store_true",
                        help="write the snapshot to the adaptive_* tables")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    raw = args.as_of if "T" in args.as_of else args.as_of + "T13:25:00+00:00"
    as_of = datetime.fromisoformat(raw)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)

    try:
        parent = ParentUniverse.load(args.parent)
    except ParentUniverseError as error:
        print("parent universe refused: %s" % error)
        return 2

    admission = AdmissionConfig(
        **{k: v for k, v in (
            ("max_capacity", args.max_capacity),
            ("pass_threshold", args.pass_threshold),
            ("max_per_sector", args.max_per_sector)) if v is not None})
    eligibility = EligibilityConfig(
        **({"max_data_age_days": args.max_data_age_days}
           if args.max_data_age_days is not None else {}))
    selector = SessionUniverseSelector(parent, eligibility=eligibility,
                                       scoring=ScoringConfig(),
                                       admission=admission)

    with MarketHistory(args.db, as_of) as history:
        selection = selector.select(history, as_of)

    report = {
        "flag_adaptive_universe_enabled":
            adaptive_flags.resolve().adaptive_universe_enabled,
        "mode": "persisted" if args.persist else "dry-run",
        "parent_universe": parent.describe(),
        "eligibility_config": eligibility.describe(),
        "scoring_config": ScoringConfig().describe(),
        "admission_config": admission.describe(),
        "selection": selection.describe(),
    }
    if args.shadow:
        report["shadow"] = compare_with_fixed(selection, fixed_universe_ids())
    if args.explain:
        assessment = selection.assessment(args.explain)
        report["explain"] = (
            {"instrument_id": args.explain, "verdict": assessment.verdict.value,
             "eligibility": assessment.eligibility.status.value,
             "reasons": list(assessment.reasons),
             "breakdown": explain(assessment.score) if assessment.score else [],
             "measurements": assessment.eligibility.measurements}
            if assessment else {"instrument_id": args.explain,
                                "error": "not in the parent universe"})

    if args.persist:
        from src.adaptive.repository import AdaptiveUniverseRepository
        conn = sqlite3.connect(args.db)
        try:
            repository = AdaptiveUniverseRepository(conn)
            repository.register_parent_universe(parent)
            report["persisted_new_snapshot"] = repository.save_selection(selection)
        finally:
            conn.close()

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0

    selected = selection.describe()
    print("ADAPTIVE SESSION UNIVERSE (%s)" % report["mode"])
    print("  as_of                : %s" % as_of.isoformat())
    print("  adaptive_universe flag: %s (production control stays off)"
          % report["flag_adaptive_universe_enabled"])
    print("  parent universe      : %s v%s, %d members, %d sectors, sha %s"
          % (os.path.basename(args.parent), parent.version,
             len(parent.members), len(parent.sectors),
             parent.members_sha256[:12]))
    print("  benchmarks           : %s (never consume capacity)"
          % ", ".join(selection.benchmarks))
    print("ELIGIBILITY  %s" % selection.eligibility_counts())
    print("VERDICTS     %s" % selection.verdict_counts())
    print("CAPACITY     selected %d of %d, unused %d, waitlist %d"
          % (selected["selected_count"], selection.universe.max_capacity,
             selected["unused_capacity"], len(selection.waitlist)))
    print("SECTORS      %s" % selected["sector_distribution"])
    print("SELECTED")
    for instrument_id in selection.universe.members:
        assessment = selection.assessment(instrument_id)
        print("  %-28s score %.4f  completeness %.2f  %s"
              % (instrument_id, assessment.overall,
                 assessment.score.completeness if assessment.score else 0.0,
                 assessment.sector_id))
    if selection.waitlist:
        print("WAITLIST (passed quality, lost capacity)")
        for instrument_id in selection.waitlist[:10]:
            print("  %s" % instrument_id)
        if len(selection.waitlist) > 10:
            print("  ... and %d more" % (len(selection.waitlist) - 10))
    if args.shadow:
        shadow = report["shadow"]
        print("SHADOW vs config/capture_universe_v1.json")
        print("  fixed %d | dynamic %d | overlap %d"
              % (shadow["fixed_count"], shadow["dynamic_count"],
                 shadow["overlap_count"]))
        print("  added by dynamic   : %s" % ", ".join(shadow["added_by_dynamic"][:8]))
        print("  dropped by dynamic : %s" % ", ".join(shadow["dropped_by_dynamic"][:8]))
        print("  %s" % shadow["note"])
    if args.explain:
        detail = report["explain"]
        print("EXPLAIN %s" % detail["instrument_id"])
        for line in detail.get("breakdown", []):
            print("  %s" % line)
        for reason in detail.get("reasons", []):
            print("  reason: %s" % reason)
    if args.persist:
        print("PERSISTED snapshot %s (new=%s)"
              % (selection.snapshot_id, report.get("persisted_new_snapshot")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
