"""
scripts/check_d20_readiness.py
-------------------------------------------
Is the single protected D20 test mechanically ready? (Phase 25.9C)

READINESS IS NOT A RESULT. This prints states, counts, dates, hashes and
ledger status. It computes no return, correlation, spread or baseline
comparison, and it never calls the validator's statistic. Safe to run
as often as needed.

READY requires ALL of:
  1. the protected-test ledger verifies (hash chain intact)
  2. the test is registered, NOT consumed, and its spec is unchanged
  3. the frozen pre-registration document exists
  4. no observation is pending (not yet observable, stale cache,
     label not built)
  5. data-quality exclusions within the cap registered in the ledger
  6. resolvable counts meet the frozen sample requirement
  7. today is on or after the earliest theoretical test date

A passed calendar date alone is never READY.

Exit code 0 when READY, 1 when NOT READY.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scripts.validate_d20_reversal as V
from src.impact import label_readiness as R
from src.research import protected_ledger as L

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREREGISTRATION_DOC = os.path.join(REPO_ROOT, "docs", "PHASE_25_9B_PREREGISTRATION.md")


def evaluate_readiness(conn: sqlite3.Connection, now: datetime,
                       ledger_path: str = L.DEFAULT_LEDGER) -> dict:
    reasons = []

    try:
        ledger = L.status(V.EXPERIMENT_ID, ledger_path)
        ledger_ok = True
    except L.LedgerError as error:
        ledger, ledger_ok = {"state": "LEDGER_INVALID", "registration": None}, False
        reasons.append(f"ledger: {error}")

    registration = ledger.get("registration") or {}
    if ledger_ok:
        if ledger["state"] == "UNREGISTERED":
            reasons.append("test is not registered in the protected-test ledger")
        elif ledger["state"] == "CONSUMED":
            reasons.append("test is already CONSUMED; it runs exactly once")
        elif registration.get("spec_fingerprint") != V.fingerprint():
            reasons.append(f"spec fingerprint {V.fingerprint()} differs from registered "
                           f"{registration.get('spec_fingerprint')}")

    if not os.path.exists(PREREGISTRATION_DOC):
        reasons.append("frozen pre-registration document is missing")

    items = R.assess(conn, V.PROTECTED_START, V.PROTECTED_END, now)
    summary = R.summarize(items)
    earliest = R.earliest_theoretical_date(items)

    if summary["total"] == 0:
        reasons.append("no protected observations found")
    if summary["pending"]:
        reasons.append(f"{summary['pending']} observation(s) pending: "
                       + ", ".join(f"{s}={summary['states'].get(s, 0)}"
                                   for s in R.PENDING_STATES if summary['states'].get(s)))
    cap = float(registration.get("max_quality_exclusion_share", 0.0))
    if summary["total"] and summary["quality_exclusions"] > cap * summary["total"]:
        reasons.append(f"{summary['quality_exclusions']} data-quality exclusion(s) exceed "
                       f"the registered cap of {cap:.0%}")
    if summary["resolvable"] < V.MIN_ROWS:
        reasons.append(f"{summary['resolvable']} resolvable observations < {V.MIN_ROWS}")
    if summary["resolvable_dates_with_10"] < V.MIN_DATES:
        reasons.append(f"{summary['resolvable_dates_with_10']} dates with >=10 resolvable < {V.MIN_DATES}")
    mde = V.minimum_detectable_effect(summary["resolvable"])
    if mde > V.MAX_MDE:
        reasons.append(f"minimum detectable |IC| {mde:.3f} > {V.MAX_MDE}")
    if earliest is None or now.date() < earliest:
        reasons.append(f"before the earliest theoretical test date ({earliest})")

    return {
        "ready": not reasons,
        "reasons": reasons,
        "experiment_id": V.EXPERIMENT_ID,
        "method_version": R.METHOD_VERSION,
        "spec_fingerprint": V.fingerprint(),
        "ledger_state": ledger.get("state"),
        "protected_window": [V.PROTECTED_START, V.PROTECTED_END],
        "earliest_theoretical_date": earliest.isoformat() if earliest else None,
        "evaluated_at": now.isoformat(),
        "summary": summary,
        "minimum_detectable_ic": round(mde, 4),
        "dataset_identity": R.dataset_identity(conn, items),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--ledger", default=L.DEFAULT_LEDGER)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--details", action="store_true",
                        help="list pending and excluded observations with reasons")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    now = datetime.now(timezone.utc)
    report = evaluate_readiness(conn, now, args.ledger)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("=" * 72)
        print("D20 protected-test readiness   (no statistic is computed here)")
        print("=" * 72)
        for key in ("experiment_id", "method_version", "spec_fingerprint", "ledger_state",
                    "earliest_theoretical_date", "dataset_identity", "minimum_detectable_ic"):
            print(f"  {key:26s} {report[key]}")
        print(f"  {'protected window':26s} {report['protected_window'][0]} -> "
              f"{report['protected_window'][1]}")
        print("\n  observation states:")
        for state, count in report["summary"]["states"].items():
            print(f"    {state:28s} {count}")
        for key in ("total", "pending", "quality_exclusions", "structural_exclusions",
                    "resolvable", "resolvable_dates_with_10"):
            print(f"  {key:26s} {report['summary'][key]}")
        print(f"\n  VERDICT: {'READY' if report['ready'] else 'NOT READY'}")
        for reason in report["reasons"]:
            print(f"    - {reason}")
    if args.details:
        for item in R.assess(conn, V.PROTECTED_START, V.PROTECTED_END, now):
            if item.state != R.READY:
                print(f"  {item.observation_id}  {item.instrument_id:24s} {item.state:26s} "
                      f"{'; '.join(item.reasons)}")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
