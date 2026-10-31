"""
scripts/research_multi_horizon.py
-----------------------------------------------
Phase 25.9I-F.6 multi-horizon research harness.

Implements `docs/PHASE_25_9I_F6_PREREGISTRATION.md` exactly. That
document was committed (ed016e8) before this script ran against any
horizon, so no choice below was made after seeing a result.

    python scripts/research_multi_horizon.py --plan
    python scripts/research_multi_horizon.py --run
    python scripts/research_multi_horizon.py --run --json

REUSES, DOES NOT REBUILD
----------------------------
  - dataset        `scripts/train_models.load_dataset`
  - purge/embargo  `src/modeling/splits.purge`, `.embargo`
  - candidates     `scripts/research_model_quality.spec_for` (C0..C3),
                   verbatim -- the same four Phase 25.9 pre-registered
  - fold scoring   `research_model_quality.evaluate_fold`
  - fit + gate     `ModelingEngine.train_and_evaluate`, whose mandatory
                   baselines, primary metric and deployability rule are
                   untouched

Nothing here defines a second qualification gate, a second registry or
a second research stack. Where a Phase 25.9 function does the job, it
is imported rather than reimplemented.

WHAT IS NEW, AND ONLY THIS
------------------------------
Phase 25.9's harness is hard-wired to `d5.abnormal_return` with one
purge horizon. This runs the identical procedure across the four
research-ready DAILY horizons -- d1, d3, d5, d10 -- each with its own
labels, its own calendar-span purge and its own independent verdict.
Horizons are never pooled.

THE HOLDOUT IS NOT OPENED HERE
----------------------------------
Pre-registration §4 and §10. Every fold lives strictly inside the
research region (cutoff < 2026-08-15T01:23:31Z). If no candidate
survives, the holdout is never read, exactly as in Phase 25.9 -- and it
still has not been.

INTRADAY AND D20 ARE NOT TOUCHED
------------------------------------
Intraday horizons are deferred at 29 of 30 required sessions. d20 is
sealed and is not a horizon this script can be pointed at: it is
rejected before any query runs.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts import research_model_quality as rmq                  # noqa: E402
from scripts.train_models import load_dataset                      # noqa: E402
from src.adaptive.horizon_inventory import is_protected            # noqa: E402
from src.adaptive.sample_adequacy import (                         # noqa: E402
    MIN_EFFECTIVE_SAMPLE, MIN_VALID_FOLDS, assess_all,
)
from src.modeling.splits import embargo, purge                     # noqa: E402

RESEARCH_DB = os.path.join(ROOT, "data", "marketlens.db")

#: Pre-registration §4. Frozen where Phase 25.9 put it, deliberately not
#: recomputed against the rebuilt corpus.
HOLDOUT_START = datetime(2026, 8, 15, 1, 23, 31, tzinfo=timezone.utc)

#: Pre-registration §3. Daily horizons only; intraday deferred at 29 of
#: 30 sessions; d20 sealed.
HORIZONS: Tuple[str, ...] = ("d1", "d3", "d5", "d10")
DEFERRED_INTRADAY: Tuple[str, ...] = ("intraday_5m", "intraday_15m",
                                      "intraday_30m", "intraday_60m")

#: Calendar span of each label's trading-day horizon (pre-registration
#: §7). Each is >= the trading days it protects, so a weekend cannot
#: carry a label across a fold boundary. Over-purging removes rows; it
#: cannot admit leakage.
PURGE_DAYS: Dict[str, float] = {"d1": 3.0, "d3": 5.0, "d5": 7.0, "d10": 14.0}

EMBARGO_DAYS = 6.0
TEST_DAYS = 7
STEP_DAYS = 7
MIN_TRAIN_ROWS = 30
MIN_TEST_ROWS = 20

CANDIDATES = rmq.CANDIDATES          # ("C0", "C1", "C2", "C3"), verbatim


class ProtectedHorizonRequested(RuntimeError):
    """Someone pointed the researcher at a sealed horizon."""


def build_folds(cutoffs: Sequence[datetime], purge_days: float, *,
                holdout_start: Optional[datetime] = None,
                embargo_days: float = EMBARGO_DAYS,
                test_days: int = TEST_DAYS,
                step_days: int = STEP_DAYS,
                min_train_rows: int = MIN_TRAIN_ROWS,
                min_test_rows: int = MIN_TEST_ROWS
                ) -> List[Dict[str, Any]]:
    """
    Expanding day-granular folds INSIDE the research region only.

    Structurally identical to `research_model_quality.build_folds`; the
    only difference is that the purge horizon is a parameter, because
    d1 and d10 protect very different spans.

    `holdout_start` defaults to this module's frozen F.6 instant, so
    every existing caller behaves exactly as before. Phase 25.9I-F.6B
    passes the boundary from its own frozen handoff rather than
    reimplementing fold construction -- the policy differs between the
    two experiments, the mechanics must not.
    """
    boundary = holdout_start or HOLDOUT_START
    research = [i for i, c in enumerate(cutoffs) if c < boundary]
    if not research:
        return []
    start = min(cutoffs[i] for i in research)
    folds: List[Dict[str, Any]] = []
    test_start = start + timedelta(days=purge_days + embargo_days + test_days)
    while test_start < boundary:
        test_end = min(test_start + timedelta(days=test_days), boundary)

        candidate_train = [i for i in research if cutoffs[i] < test_start]
        kept, purged = purge(candidate_train, test_start,
                             lambda i: cutoffs[i], purge_days)
        kept, embargoed = embargo(kept, test_start,
                                  lambda i: cutoffs[i], embargo_days)
        test_idx = [i for i in research if test_start <= cutoffs[i] < test_end]

        folds.append({
            "label": f"wf{len(folds) + 1}",
            "train_idx": kept, "test_idx": test_idx,
            "test_start": test_start, "test_end": test_end,
            "purged": len(purged), "embargoed": len(embargoed),
            "valid": len(kept) >= min_train_rows
            and len(test_idx) >= min_test_rows,
        })
        test_start = test_start + timedelta(days=step_days)
    return folds


def span_requirement(cutoffs: Sequence[datetime], horizon_id: str, *,
                     holdout_start: Optional[datetime] = None
                     ) -> Dict[str, Any]:
    """
    How much more research-region CALENDAR SPAN a horizon needs.

    Brief section 32: a horizon that cannot be modelled must say what
    would make it eligible. The binding constraint here is not sample
    size -- 159 to 236 independent clusters clear a floor of 30 -- it is
    the number of DAYS the research region covers, because a fold
    cannot open until purge + embargo + test days have elapsed, and
    three folds need two further steps after that.

    Reaching the span only buys a FAIR TEST. It promises nothing about
    whether an edge exists.
    """
    boundary = holdout_start or HOLDOUT_START
    research = [c for c in cutoffs if c < boundary]
    if not research:
        return {"research_span_days": 0, "days_for_first_fold": None,
                "days_for_three_folds": None, "shortfall_days": None}
    span = (boundary - min(research)).days
    first = PURGE_DAYS[horizon_id] + EMBARGO_DAYS + TEST_DAYS
    three = first + 2 * STEP_DAYS
    return {
        "research_span_days": span,
        "days_for_first_fold": first,
        "days_for_three_folds": three,
        "shortfall_days": max(0, three - span),
        "note": ("the binding constraint is calendar span, not sample size; "
                 "reaching it buys a fair test, not an edge"),
    }


def research_horizon(conn: sqlite3.Connection, horizon_id: str,
                     *, verbose: bool = True) -> Dict[str, Any]:
    """
    One horizon, four candidates, the pre-registered contract.

    Returns a record even when nothing can be evaluated -- an
    unexplained horizon is not an acceptable output (brief §54).
    """
    if is_protected(horizon_id):
        raise ProtectedHorizonRequested(
            f"{horizon_id} is sealed and cannot be researched")

    label = f"{horizon_id}.abnormal_return"
    names, X, Y, cutoffs, clusters, observation_ids, quality = load_dataset(
        conn, label)

    if not X:
        return {"horizon": horizon_id, "verdict": "UNSUPPORTED",
                "reason": "no matured labels exist for this horizon",
                "candidates": []}

    # `evaluate_fold` reads fold cutoffs through this module-level list
    # in the Phase 25.9 harness. Set it for the horizon being run.
    rmq._CUTOFFS = list(cutoffs)

    folds = build_folds(cutoffs, PURGE_DAYS[horizon_id])
    valid = [f for f in folds if f["valid"]]
    research_rows = sum(1 for c in cutoffs if c < HOLDOUT_START)
    research_clusters = len({clusters[i] for i, c in enumerate(cutoffs)
                             if c < HOLDOUT_START})
    maturity = span_requirement(cutoffs, horizon_id)

    header = {
        "horizon": horizon_id, "label": label,
        "rows": len(X), "features": len(names),
        "clusters": len({c for c in clusters}),
        "research_region_rows": research_rows,
        "research_region_clusters": research_clusters,
        "purge_days": PURGE_DAYS[horizon_id],
        "folds_built": len(folds), "folds_valid": len(valid),
        "span_requirement": maturity,
        "fold_detail": [
            {"label": f["label"],
             "test_start": f["test_start"].date().isoformat(),
             "test_end": f["test_end"].date().isoformat(),
             "train": len(f["train_idx"]), "test": len(f["test_idx"]),
             "purged": f["purged"], "embargoed": f["embargoed"],
             "valid": f["valid"]}
            for f in folds],
    }

    if research_clusters < MIN_EFFECTIVE_SAMPLE:
        return {**header, "verdict": "INSUFFICIENT DATA",
                "reason": (f"research-region effective sample "
                           f"{research_clusters} is below the floor of "
                           f"{MIN_EFFECTIVE_SAMPLE}"),
                "candidates": []}

    if len(valid) < MIN_VALID_FOLDS:
        return {**header, "verdict": "INSUFFICIENT DATA",
                "reason": (f"only {len(valid)} valid fold(s); the contract "
                           f"requires {MIN_VALID_FOLDS}"),
                "candidates": []}

    if verbose:
        print("  %s: %d rows, %d clusters, %d valid folds"
              % (horizon_id, len(X), header["clusters"], len(valid)))

    candidates: List[Dict[str, Any]] = []
    for candidate in CANDIDATES:
        started = time.perf_counter()
        results = []
        for fold in valid:
            try:
                results.append(rmq.evaluate_fold(
                    candidate, fold, X, Y, clusters, names))
            except Exception as error:                      # noqa: BLE001
                results.append({"candidate": candidate, "fold": fold["label"],
                                "error": f"{type(error).__name__}: {error}"})
        outcome = rmq.verdict(candidate, results, len(valid))
        outcome["runtime_seconds"] = round(time.perf_counter() - started, 3)
        outcome["folds"] = results
        candidates.append(outcome)
        if verbose:
            print("    %-3s %-14s %s" % (candidate, outcome.get("verdict"),
                                         "; ".join(outcome.get("reasons", []))[:96]))

    # DEFECT FIXED 2026-10-04 (Phase 25.9I-F.6B pre-flight).
    #
    # This read `c.get("qualified")`, but `rmq.verdict()` returns
    # `status`, never a `qualified` key -- so the list was ALWAYS empty
    # and this function would have reported NONE QUALIFIED whatever the
    # candidates did. F.6's recorded result is unaffected: zero folds
    # formed on every horizon, so the INSUFFICIENT DATA return above
    # fired first and no candidate was ever scored. A test pins that.
    #
    # Surviving the research region is also not qualification -- the
    # pre-registration reserves one holdout evaluation for survivors.
    # That stage lives in `scripts/research_f6b.py`, so this harness
    # reports survival and does not claim more.
    survivors = [c for c in candidates
                 if c.get("status") == "SURVIVES RESEARCH REGION"]
    return {**header,
            "verdict": ("SURVIVORS IN RESEARCH REGION" if survivors
                        else "NONE SURVIVED"),
            "survivor_candidates": [c["candidate"] for c in survivors],
            "candidates": candidates}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Phase 25.9I-F.6 multi-horizon research. Read-only; "
                    "promotes nothing and never opens the holdout.")
    parser.add_argument("--db", default=RESEARCH_DB)
    parser.add_argument("--plan", action="store_true",
                        help="print the plan and sample adequacy; run nothing")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        adequacy = assess_all(
            conn, list(HORIZONS) + list(DEFERRED_INTRADAY) + ["d20"],
            holdout_start=HOLDOUT_START)

        if args.plan or not args.run:
            payload = {
                "preregistration": "docs/PHASE_25_9I_F6_PREREGISTRATION.md",
                "holdout_start": HOLDOUT_START.isoformat(),
                "holdout_opened": False,
                "horizons_researched": list(HORIZONS),
                "horizons_deferred": list(DEFERRED_INTRADAY),
                "horizons_protected": ["d20"],
                "candidates": list(CANDIDATES),
                "candidate_horizon_cells": len(CANDIDATES) * len(HORIZONS),
                "sample_adequacy": [a.describe() for a in adequacy],
            }
            if args.json:
                print(json.dumps(payload, indent=2, default=str))
            else:
                print("PHASE 25.9I-F.6 RESEARCH PLAN (pre-registered)")
                print("  holdout frozen at %s -- NOT opened by this script"
                      % HOLDOUT_START.date())
                print("  researching: %s" % ", ".join(HORIZONS))
                print("  deferred   : %s" % ", ".join(DEFERRED_INTRADAY))
                print("  protected  : d20")
                print("  budget     : %d candidate-horizon cells"
                      % payload["candidate_horizon_cells"])
                print("\n  %-14s %6s %9s %8s %-28s"
                      % ("horizon", "rows", "clusters", "sessions", "readiness"))
                for a in adequacy:
                    print("  %-14s %6d %9d %8d %-28s"
                          % (a.horizon_id, a.raw_rows, a.effective_sample,
                             a.unique_sessions, a.readiness.value))
            return 0

        started = time.perf_counter()
        results = []
        if not args.json:
            print("RUNNING PRE-REGISTERED RESEARCH (%d cells)"
                  % (len(CANDIDATES) * len(HORIZONS)))
        for horizon in HORIZONS:
            results.append(research_horizon(conn, horizon,
                                            verbose=not args.json))
    finally:
        conn.close()

    runtime = round(time.perf_counter() - started, 2)
    qualified = [r for r in results if r.get("qualified_candidates")]
    report = {
        "preregistration": "docs/PHASE_25_9I_F6_PREREGISTRATION.md",
        "holdout_start": HOLDOUT_START.isoformat(),
        "holdout_opened": False,
        "runtime_seconds": runtime,
        "horizons": results,
        "horizons_deferred": list(DEFERRED_INTRADAY),
        "horizons_protected": ["d20"],
        "qualified_total": sum(len(r.get("qualified_candidates", []))
                               for r in results),
        "models_promoted": 0,
        "thresholds_changed": 0,
    }
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print("\nRESULT")
    for record in results:
        print("  %-5s %-18s %s"
              % (record["horizon"], record["verdict"],
                 record.get("reason", "")[:80]))
    print("\nWHAT WOULD MAKE EACH HORIZON ELIGIBLE")
    print("  %-5s %11s %11s %11s" % ("horiz", "span_days", "need_3wf",
                                     "shortfall"))
    for record in results:
        need = record.get("span_requirement") or {}
        if need.get("days_for_three_folds") is None:
            continue
        print("  %-5s %11d %11g %11g"
              % (record["horizon"], need["research_span_days"],
                 need["days_for_three_folds"], need["shortfall_days"]))
    print("  sample size is NOT the constraint: 159-236 independent clusters")
    print("  against a floor of 30. Calendar span is.")

    print("\n  qualified research candidates: %d" % report["qualified_total"])
    print("  models promoted: 0 | thresholds changed: 0 | holdout opened: NO")
    print("  runtime: %.1fs" % runtime)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
