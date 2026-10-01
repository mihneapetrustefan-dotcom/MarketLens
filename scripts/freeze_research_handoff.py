#!/usr/bin/env python
"""
scripts/freeze_research_handoff.py
--------------------------------------------------
Phase 25.9I-F.7: freeze the research environment F.6B must consume.

    python scripts/freeze_research_handoff.py --inspect
    python scripts/freeze_research_handoff.py --freeze

`--inspect` measures and prints; it writes nothing. `--freeze` applies
the inherited holdout rule, checks the design is adequate, marks the
corpus and snapshot ELIGIBLE_FOR_RESEARCH, and writes the handoff to
`docs/PHASE_25_9I_F6B_HANDOFF.json`.

THIS SCRIPT RUNS NO MODEL
-------------------------
It fits nothing, scores nothing, and opens no holdout. Its entire
purpose is to fix the choices F.6B would otherwise be tempted to make
after seeing a result:

  - the holdout boundary, from a fraction that predates this dataset;
  - which horizons are eligible, from thresholds that are not relaxed;
  - the candidate budget, the purge and embargo policy, the baselines
    and the qualification gate, all by reference to existing code.

`HoldoutDecision` refuses to construct if told an outcome was observed,
and `ResearchHandoff` refuses to construct unless the protected horizon
is excluded. Those two refusals are the enforcement; this script is
only the sequence.

ADEQUACY IS A VERDICT, NOT A TARGET
-----------------------------------
If the recovered dataset still cannot support a legal fold design, the
answer is DATASET STILL INSUFFICIENT and `--freeze` exits non-zero
without marking anything eligible. It does not try a different
fraction.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.adaptive import research_snapshot as rs                     # noqa: E402
from src.adaptive.corpus_versioning import (                         # noqa: E402
    file_digest, initialize_recovery_schema, load_corpus_version,
    load_snapshot, record_corpus_version, record_snapshot,
    verify_corpus_hash, verify_snapshot_hash,
)
from src.domain.corpus_recovery_models import ArtifactState          # noqa: E402

UTC = timezone.utc

TARGET_DB = os.path.join(ROOT, "data", "marketlens_corpus_v2.db")
CORPUS_V2 = "canonical_corpus_v2"
SNAPSHOT_V2 = "research_snapshot_v2"

PREREGISTRATION = os.path.join(ROOT, "docs",
                               "PHASE_25_9I_F6B_PREREGISTRATION.md")
HANDOFF_PATH = os.path.join(ROOT, "docs", "PHASE_25_9I_F6B_HANDOFF.json")

#: Pre-registration §4 and §5. The four daily horizons, and the same
#: 16-cell budget F.6 used -- a larger corpus is not a licence for a
#: wider search.
PRE_REGISTERED_HORIZONS = ("d1", "d3", "d5", "d10")
CANDIDATE_BUDGET = 16

#: Pre-registration §4. Data-eligible, deferred on policy: the only
#: purge available to them is the 14-day fallback written for d10.
DEFERRED_ON_POLICY = ("intraday_5m", "intraday_15m",
                      "intraday_30m", "intraday_60m")


def _ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def inspect(db_path: str) -> Dict[str, object]:
    """Measure the environment without changing or claiming anything."""
    with _ro(db_path) as conn:
        earliest, latest = rs.information_range(conn)
        holdout = rs.deterministic_holdout(conn, snapshot_id=SNAPSHOT_V2)
        feasibility = rs.assess_feasibility(conn, holdout=holdout)
        eligible, reasons = rs.eligible_horizons(
            conn, holdout=holdout, feasibility=feasibility)
        adequacy = rs.holdout_adequacy(conn, holdout=holdout,
                                       feasibility=feasibility)
        corpus = load_corpus_version(conn, CORPUS_V2)
        snapshot = load_snapshot(conn, SNAPSHOT_V2)
        samples = {
            horizon: rs.effective_sample(conn, horizon,
                                         before=holdout.boundary)
            for horizon in eligible}
        stale = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("trained_models", "model_evaluations",
                          "model_baseline_comparisons", "predictions")}
        corpus_hash_ok = verify_corpus_hash(conn, corpus) if corpus else None
        snapshot_hash_ok = (verify_snapshot_hash(conn, snapshot)
                            if snapshot else None)

    return {
        "db": db_path,
        "measured_at": datetime.now(UTC).isoformat(),
        "information_range": {
            "earliest": earliest.isoformat() if earliest else None,
            "latest": latest.isoformat() if latest else None,
            "span_days": (latest - earliest).days if earliest and latest else None,
        },
        "holdout": holdout.describe(),
        "feasibility": [f.describe() for f in feasibility],
        "data_eligible_horizons": eligible,
        "eligibility_reasons": reasons,
        "pre_registered_horizons": list(PRE_REGISTERED_HORIZONS),
        "deferred_on_policy": list(DEFERRED_ON_POLICY),
        "adequacy": adequacy,
        "effective_sample": samples,
        "corpus": corpus.describe() if corpus else None,
        "snapshot": snapshot.describe() if snapshot else None,
        "corpus_hash_verified": corpus_hash_ok,
        "snapshot_hash_verified": snapshot_hash_ok,
        "stale_experiment_rows": stale,
        "stale_experiment_rows_total": sum(stale.values()),
        "preregistration": {
            "path": os.path.relpath(PREREGISTRATION, ROOT),
            "exists": os.path.exists(PREREGISTRATION),
            "sha256": (file_digest(PREREGISTRATION)
                       if os.path.exists(PREREGISTRATION) else None),
        },
    }


def freeze(db_path: str) -> Dict[str, object]:
    """
    Mark the corpus and snapshot eligible and write the handoff.

    Refuses on three conditions, each of which would make the handoff a
    lie rather than a contract:

      * the design is not adequate -> DATASET STILL INSUFFICIENT;
      * a content hash no longer matches what was frozen;
      * a stale model evaluation survives in the new database, where it
        could be read as the new snapshot's sample capacity.
    """
    report = inspect(db_path)
    problems: List[str] = []

    if report["adequacy"]["verdict"] != "ADEQUATE":
        problems.append(f"adequacy verdict is "
                        f"{report['adequacy']['verdict']}")
    if report["corpus_hash_verified"] is not True:
        problems.append("corpus content hash does not verify")
    if report["snapshot_hash_verified"] is not True:
        problems.append("snapshot content hash does not verify")
    if report["stale_experiment_rows_total"]:
        problems.append(f"{report['stale_experiment_rows_total']} stale "
                        f"experiment rows present in the new database")
    if not report["preregistration"]["exists"]:
        problems.append("the pre-registration document is missing")
    missing = [h for h in PRE_REGISTERED_HORIZONS
               if h not in report["data_eligible_horizons"]]
    if missing:
        problems.append(f"pre-registered horizons not data-eligible: {missing}")

    report["problems"] = problems
    report["frozen"] = False
    if problems:
        return report

    at = datetime.now(UTC)
    conn = sqlite3.connect(db_path)
    try:
        initialize_recovery_schema(conn)
        corpus = load_corpus_version(conn, CORPUS_V2)
        snapshot = load_snapshot(conn, SNAPSHOT_V2)

        if corpus.state is ArtifactState.FROZEN:
            corpus = corpus.transition_to(ArtifactState.ELIGIBLE_FOR_RESEARCH,
                                          at=at)
            record_corpus_version(conn, corpus)
        if snapshot.state is ArtifactState.FROZEN:
            snapshot = snapshot.transition_to(
                ArtifactState.ELIGIBLE_FOR_RESEARCH, at=at)
            record_snapshot(conn, snapshot)
        conn.commit()

        holdout = rs.deterministic_holdout(conn, snapshot_id=SNAPSHOT_V2,
                                           decided_at=at)
        handoff = rs.build_handoff(
            conn, snapshot=snapshot, holdout=holdout,
            preregistration_path=os.path.relpath(PREREGISTRATION, ROOT),
            preregistration_hash=file_digest(PREREGISTRATION),
            candidate_budget=CANDIDATE_BUDGET,
            pre_registered_horizons=PRE_REGISTERED_HORIZONS)
    finally:
        conn.close()

    payload = handoff.describe()
    payload["version_reading"] = {
        "snapshot_id": SNAPSHOT_V2,
        "corpus_version_id": CORPUS_V2,
        "dataset_version": payload["snapshot"]["dataset_version"],
        "note": "dataset_version reads v1 and that is correct, not stale: F.7 "
                "changed no feature definition, label definition, event "
                "taxonomy, benchmark or preprocessing step. What changed is "
                "the corpus, and that is carried by corpus_version_id. The "
                "snapshot's own identity is v2, so it cannot be confused with "
                "research_snapshot_v1 -- the fingerprint differs on both the "
                "snapshot id and the corpus version.",
        "horizons_listed_in_snapshot": payload["snapshot"]["horizons"],
        "horizons_note": "the snapshot inventory lists every horizon with "
                         "labels present, d20 among them, exactly as the F.5 "
                         "and F.6 inventories do. It is not eligible, carries "
                         "no effective-sample entry, and d20_excluded is true.",
    }
    payload["deferred_on_policy"] = {
        "horizons": list(DEFERRED_ON_POLICY),
        "reason": "data-eligible at 57 distinct sessions, but the only purge "
                  "horizon available to them is the 14-day fallback written "
                  "for d10; an intraday purge must be pre-registered on its "
                  "own evidence",
    }
    payload["f6b_must_not"] = [
        "change the holdout boundary after seeing any model outcome",
        "widen the candidate budget beyond 16 cells",
        "pool horizons",
        "read a stored effective_sample_size as the snapshot's capacity",
        "treat multi_source as independently corroborated "
        "(independent_source_count is 0 for every event)",
        "research, inspect, infer or consume the protected d20 horizon",
        "promote a model",
    ]
    with open(HANDOFF_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)

    report["frozen"] = True
    report["handoff_path"] = os.path.relpath(HANDOFF_PATH, ROOT)
    report["handoff_is_research_ready"] = handoff.is_research_ready
    report["handoff"] = payload
    report["corpus_state"] = corpus.state.value
    report["snapshot_state"] = snapshot.state.value
    return report


def _print(report: Dict[str, object]) -> None:
    rng = report["information_range"]
    print("PHASE 25.9I-F.7 RESEARCH HANDOFF")
    print(f"  db                : {report['db']}")
    print(f"  information range : {rng['earliest']} -> {rng['latest']} "
          f"({rng['span_days']} days)")
    hold = report["holdout"]
    print()
    print("  HOLDOUT (inherited 20% rule, applied mechanically)")
    print(f"    boundary          {hold['boundary']}")
    print(f"    research days     {hold['research_days']}")
    print(f"    holdout days      {hold['holdout_days']} "
          f"({hold['holdout_fraction']})")
    print(f"    outcome observed  {hold['observed_model_outcome']}")
    print()
    print("  FOLD FEASIBILITY")
    print(f"    {'horizon':14s} {'rows':>6s} {'clusters':>9s} {'purge':>6s} "
          f"{'1 fold':>7s} {'3 folds':>8s} {'max':>5s} viable")
    for item in report["feasibility"]:
        print(f"    {item['horizon_id']:14s} {item['observation_count']:>6,} "
              f"{item['cluster_count']:>9,} {item['purge_days']:>6.0f} "
              f"{item['days_for_first_fold']:>7.0f} "
              f"{item['days_for_min_folds']:>8.0f} {item['max_folds']:>5d} "
              f"{item['is_viable']}")
    print()
    print("  ELIGIBILITY")
    for horizon, reason in report["eligibility_reasons"].items():
        print(f"    {horizon:14s} {reason}")
    print(f"    pre-registered for F.6B : {report['pre_registered_horizons']}")
    print(f"    deferred on policy      : {report['deferred_on_policy']}")
    print()
    adequacy = report["adequacy"]
    print(f"  ADEQUACY          {adequacy['verdict']}")
    print(f"    max folds       {adequacy['max_folds']}")
    print(f"    purge/embargo   represented={adequacy['purge_represented']}/"
          f"{adequacy['embargo_represented']}")
    print()
    print("  INTEGRITY")
    print(f"    corpus hash verified   {report['corpus_hash_verified']}")
    print(f"    snapshot hash verified {report['snapshot_hash_verified']}")
    print(f"    stale experiment rows  {report['stale_experiment_rows']}")
    pre = report["preregistration"]
    print(f"    pre-registration       {pre['path']}")
    print(f"    sha256                 {pre['sha256']}")
    print()
    if report.get("problems"):
        for problem in report["problems"]:
            print(f"  BLOCKED: {problem}")
    if report.get("frozen"):
        print(f"  FROZEN: corpus={report['corpus_state']} "
              f"snapshot={report['snapshot_state']}")
        print(f"  handoff written to {report['handoff_path']}")
        print(f"  research ready: {report['handoff_is_research_ready']}")
    else:
        print("  nothing was written")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=TARGET_DB)
    parser.add_argument("--inspect", action="store_true",
                        help="Measure and print; write nothing. The default.")
    parser.add_argument("--freeze", action="store_true",
                        help="Mark eligible and write the handoff.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: database not found: {args.db}")
        print("Run scripts/recover_historical_corpus.py --run first.")
        return 1

    report = freeze(args.db) if args.freeze else inspect(args.db)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print(report)

    if args.freeze and not report.get("frozen"):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
