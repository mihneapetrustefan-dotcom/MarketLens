"""
scripts/research_model_quality.py
-------------------------------------------
Phase 25.9 model-quality research harness.

Implements docs/PHASE_25_9_PREREGISTRATION.md exactly. That document was
committed (c09ff19) before this script existed and before any candidate
was evaluated. Where this script differs from it, the difference is
named in DEVIATIONS below and was decided before any result was seen.

REUSES, DOES NOT REBUILD
----------------------------
  - dataset:     scripts/train_models.load_dataset
  - purge/embargo: src/modeling/splits.purge, embargo
  - fit + score: ModelingEngine.train_and_evaluate (mandatory baselines,
                 primary metric, deployability rule -- all unchanged)

DEVIATIONS FROM THE PRE-REGISTRATION (decided before evaluation)
--------------------------------------------------------------------
  1. Purge horizon 7 calendar days, not 5. The label is d5 = five
     TRADING days, which spans up to seven calendar days across a
     weekend. splits.WalkForwardSplitter warns that a horizon shorter
     than the label "silently under-protects". Seven can only purge
     more rows; it cannot admit leakage.

  2. Walk-forward is built in DAYS, not with WalkForwardSplitter, whose
     unit is months with a 36-month default. The research region is 39
     days; month-granular windows cannot form a single fold. purge and
     embargo are still the library functions.

THE PROTECTED WINDOW IS NOT OPENED UNLESS A CANDIDATE SURVIVES
-----------------------------------------------------------------
Pre-registration §3.4. If nothing clears the research region, this
script exits without ever reading a protected row's label.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.train_models import load_dataset
from src.domain.model_models import (
    ModelFamily, ModelSpecification, PredictionTask, TrainingWindow,
)
from src.modeling import algorithms
from src.modeling.engine import ModelingEngine, primary_metric_name
from src.modeling.splits import embargo, purge

# ---------------- pre-registered constants ----------------

LABEL = "d5.abnormal_return"
PROTECTED_START = datetime(2026, 8, 15, 1, 23, 31, tzinfo=timezone.utc)
LABEL_HORIZON_DAYS = 7.0      # deviation 1: calendar span of 5 trading days
EMBARGO_DAYS = 6.0            # same as scripts/train_models.py
TEST_DAYS = 7
STEP_DAYS = 7
MIN_TRAIN_ROWS = 30
MIN_TEST_ROWS = 20
MIN_VALID_FOLDS = 3           # pre-registration §5.2
MIN_EFFECTIVE_SAMPLE = 30     # ModelEvaluation.MIN_EFFECTIVE_SAMPLE
C3_MIN_COVERAGE = 0.90        # pre-registration §6


def spec_for(candidate: str) -> ModelSpecification:
    common = dict(label_name=LABEL, feature_set_version="v1",
                  dataset_version="v1", version="v1")
    if candidate == "C0":
        return ModelSpecification(
            model_id="ridge_abnormal_return", name="C0 incumbent ridge",
            task=PredictionTask.ABNORMAL_RETURN,
            family=ModelFamily.RIDGE_REGRESSION, feature_set_id="all_numeric_v1",
            hyperparameters={"alpha": 1.0}, **common)
    if candidate == "C1":
        return ModelSpecification(
            model_id="logistic_direction", name="C1 logistic direction",
            task=PredictionTask.DIRECTION,
            family=ModelFamily.LOGISTIC_REGRESSION, feature_set_id="all_numeric_v1",
            hyperparameters={"l2": 0.01, "learning_rate": 0.1,
                             "iterations": 300, "threshold": 0.0}, **common)
    if candidate == "C2":
        return ModelSpecification(
            model_id="ridge_abnormal_return_shrunk", name="C2 ridge alpha=10",
            task=PredictionTask.ABNORMAL_RETURN,
            family=ModelFamily.RIDGE_REGRESSION, feature_set_id="all_numeric_v1",
            hyperparameters={"alpha": 10.0}, **common)
    if candidate == "C3":
        return ModelSpecification(
            model_id="logistic_direction_dense", name="C3 logistic dense features",
            task=PredictionTask.DIRECTION,
            family=ModelFamily.LOGISTIC_REGRESSION, feature_set_id="dense_v1",
            hyperparameters={"l2": 0.01, "learning_rate": 0.1,
                             "iterations": 300, "threshold": 0.0}, **common)
    raise ValueError(candidate)


CANDIDATES = ("C0", "C1", "C2", "C3")


# ---------------- folds ----------------

def build_folds(cutoffs: Sequence[datetime]) -> List[Dict[str, Any]]:
    """
    Expanding day-granular folds inside the research region only.

    A row is research-eligible only if its cutoff is before the
    protected start. Test windows never reach into protected data.
    """
    research = [i for i, c in enumerate(cutoffs) if c < PROTECTED_START]
    if not research:
        return []
    start = min(cutoffs[i] for i in research)
    folds = []
    test_start = start + timedelta(days=LABEL_HORIZON_DAYS + EMBARGO_DAYS
                                   + TEST_DAYS)
    while True:
        test_end = min(test_start + timedelta(days=TEST_DAYS), PROTECTED_START)
        if test_start >= PROTECTED_START:
            break

        candidates_train = [i for i in research if cutoffs[i] < test_start]
        kept, purged = purge(candidates_train, test_start,
                             lambda i: cutoffs[i], LABEL_HORIZON_DAYS)
        kept, embargoed = embargo(kept, test_start,
                                  lambda i: cutoffs[i], EMBARGO_DAYS)
        test_idx = [i for i in research
                    if test_start <= cutoffs[i] < test_end]

        folds.append({
            "label": f"wf{len(folds) + 1}",
            "train_idx": kept, "test_idx": test_idx,
            "test_start": test_start, "test_end": test_end,
            "purged": len(purged), "embargoed": len(embargoed),
            "valid": len(kept) >= MIN_TRAIN_ROWS and len(test_idx) >= MIN_TEST_ROWS,
        })
        test_start = test_start + timedelta(days=STEP_DAYS)
    return folds


# ---------------- per-candidate feature view ----------------

def dense_columns(X: Sequence[Sequence[Optional[float]]],
                  train_idx: Sequence[int]) -> List[int]:
    """C3's rule, computed from THIS fold's training rows only."""
    if not train_idx:
        return []
    width = len(X[0])
    keep = []
    for column in range(width):
        present = sum(1 for i in train_idx if X[i][column] is not None)
        if present / len(train_idx) >= C3_MIN_COVERAGE:
            keep.append(column)
    return keep


def select(rows, columns):
    return [[row[c] for c in columns] for row in rows]


# ---------------- calibration ----------------

def brier(probabilities, outcomes) -> Optional[float]:
    pairs = [(p, 1.0 if y > 0 else 0.0)
             for p, y in zip(probabilities, outcomes) if p is not None]
    if not pairs:
        return None
    return round(sum((p - y) ** 2 for p, y in pairs) / len(pairs), 6)


# ---------------- evaluation ----------------

def evaluate_fold(candidate: str, fold: Dict[str, Any], X, Y, clusters,
                  feature_names) -> Dict[str, Any]:
    train_idx, test_idx = fold["train_idx"], fold["test_idx"]
    columns = list(range(len(feature_names)))
    if candidate == "C3":
        columns = dense_columns(X, train_idx)

    X_train = select([X[i] for i in train_idx], columns)
    Y_train = [Y[i] for i in train_idx]
    X_test = select([X[i] for i in test_idx], columns)
    Y_test = [Y[i] for i in test_idx]
    names = [feature_names[c] for c in columns]
    test_clusters = len({clusters[i] for i in test_idx})

    spec = spec_for(candidate)
    window = TrainingWindow(
        label=fold["label"],
        train_start=min(fold_cutoff(train_idx)) if train_idx else None,
        train_end=max(fold_cutoff(train_idx)) if train_idx else None,
        test_start=fold["test_start"], test_end=fold["test_end"],
        train_size=len(train_idx), test_size=len(test_idx),
        embargoed_count=fold["embargoed"])
    engine = ModelingEngine()
    model, evaluation = engine.train_and_evaluate(
        spec, X_train, Y_train, X_test, Y_test,
        feature_names=names, window=window, cluster_count=test_clusters)

    metric = primary_metric_name(spec.task)
    result = {
        "candidate": candidate, "fold": fold["label"],
        "train_rows": len(train_idx), "test_rows": len(test_idx),
        "test_clusters": test_clusters, "features": len(columns),
        "metric": metric, "model_score": evaluation.metrics.get(metric),
        "baselines": {c.baseline_name: c.baseline_score
                      for c in evaluation.baseline_comparisons},
        "beats_all_baselines": evaluation.beats_all_baselines,
        "abstention_rate": evaluation.abstention_rate,
    }

    if spec.family is ModelFamily.LOGISTIC_REGRESSION:
        probs = algorithms.predict_batch(model.parameters, X_test)
        base_rate = (sum(1 for y in Y_train if y is not None and y > 0)
                     / max(1, sum(1 for y in Y_train if y is not None)))
        result["brier"] = brier(probs, Y_test)
        result["brier_base_rate"] = brier([base_rate] * len(Y_test), Y_test)
    return result


_CUTOFFS: List[datetime] = []


def fold_cutoff(indices):
    return [_CUTOFFS[i] for i in indices]


# ---------------- contract ----------------

def verdict(candidate: str, results: List[Dict[str, Any]],
            valid_folds: int) -> Dict[str, Any]:
    """Pre-registration §5, applied mechanically."""
    reasons: List[str] = []
    if valid_folds < MIN_VALID_FOLDS:
        return {"candidate": candidate, "status": "INSUFFICIENT DATA",
                "reasons": [f"only {valid_folds} valid fold(s); "
                            f"{MIN_VALID_FOLDS} required"]}

    wins = sum(1 for r in results if r["beats_all_baselines"] is True)
    judged = sum(1 for r in results if r["beats_all_baselines"] is not None)
    majority = wins > judged / 2 if judged else False
    if not majority:
        reasons.append(f"beats baselines in {wins}/{judged} folds; a strict "
                       f"majority is required")

    clusters = sum(r["test_clusters"] for r in results)
    if clusters < MIN_EFFECTIVE_SAMPLE:
        reasons.append(f"pooled effective sample {clusters} < "
                       f"{MIN_EFFECTIVE_SAMPLE}")

    if any("brier" in r for r in results):
        worse = [r["fold"] for r in results
                 if r.get("brier") is not None
                 and r.get("brier_base_rate") is not None
                 and r["brier"] >= r["brier_base_rate"]]
        if len(worse) * 2 >= len(results):
            reasons.append(f"Brier not below base rate in {len(worse)}/"
                           f"{len(results)} folds")

    status = "SURVIVES RESEARCH REGION" if not reasons else "NOT QUALIFIED"
    return {"candidate": candidate, "status": status, "fold_wins": wins,
            "folds_judged": judged, "pooled_effective_sample": clusters,
            "reasons": reasons}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    feature_names, X, Y, cutoffs, clusters, obs_ids, _q = load_dataset(conn, LABEL)
    _CUTOFFS.extend(cutoffs)

    research_rows = sum(1 for c in cutoffs if c < PROTECTED_START)
    protected_rows = len(cutoffs) - research_rows
    folds = build_folds(cutoffs)
    valid = [f for f in folds if f["valid"]]

    print("=" * 72)
    print("Phase 25.9 model-quality research  (pre-registration c09ff19)")
    print("=" * 72)
    print(f"label                {LABEL}")
    print(f"rows                 {len(X)}  (research {research_rows}, "
          f"protected {protected_rows})")
    print(f"features             {len(feature_names)}")
    print(f"purge / embargo      {LABEL_HORIZON_DAYS}d / {EMBARGO_DAYS}d")
    print(f"folds formed         {len(folds)}  valid {len(valid)}  "
          f"(need {MIN_VALID_FOLDS})")
    for f in folds:
        print(f"  {f['label']}  test {f['test_start']:%m-%d}..{f['test_end']:%m-%d}  "
              f"train {len(f['train_idx']):>4}  test {len(f['test_idx']):>4}  "
              f"purged {f['purged']:>4}  embargoed {f['embargoed']:>4}  "
              f"{'VALID' if f['valid'] else 'invalid'}")

    report: Dict[str, Any] = {
        "preregistration": "c09ff19", "label": LABEL,
        "rows": len(X), "research_rows": research_rows,
        "protected_rows": protected_rows,
        "folds_formed": len(folds), "folds_valid": len(valid),
        "deviations": ["purge 7 calendar days (d5 = five trading days)",
                       "walk-forward in days; WalkForwardSplitter is monthly"],
        "candidates": {}, "verdicts": [], "protected_opened": False,
    }

    for candidate in CANDIDATES:
        results = [evaluate_fold(candidate, f, X, Y, clusters, feature_names)
                   for f in valid]
        v = verdict(candidate, results, len(valid))
        report["candidates"][candidate] = results
        report["verdicts"].append(v)

        print(f"\n--- {candidate}: {spec_for(candidate).name} ---")
        for r in results:
            base = ", ".join(f"{k.replace('baseline_', '')}={val}"
                             for k, val in r["baselines"].items())
            extra = (f"  brier {r['brier']} vs base {r['brier_base_rate']}"
                     if "brier" in r else "")
            print(f"  {r['fold']}  {r['metric']}={r['model_score']}  [{base}]  "
                  f"beats={r['beats_all_baselines']}  "
                  f"clusters={r['test_clusters']}{extra}")
        print(f"  VERDICT: {v['status']}")
        for reason in v.get("reasons", []):
            print(f"    - {reason}")

    survivors = [v for v in report["verdicts"]
                 if v["status"] == "SURVIVES RESEARCH REGION"]
    print("\n" + "=" * 72)
    if not survivors:
        print("No candidate survived the research region.")
        print("PROTECTED WINDOW NOT OPENED (pre-registration §3.4).")
    else:
        print(f"{len(survivors)} candidate(s) survived; protected evaluation "
              f"is a separate, single, frozen step.")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, default=str)
        print(f"\nreport written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
