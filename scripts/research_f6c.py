#!/usr/bin/env python
"""
scripts/research_f6c.py
--------------------------------------------------
Phase 25.9I-F.6C frozen calibration experiment.

    python scripts/research_f6c.py --plan
    python scripts/research_f6c.py --run
    python scripts/research_f6c.py --show

> THE MODEL MUST EARN DISCRIMINATION.
> THE PROBABILITY MUST EARN TRUST.
> THE HOLDOUT MUST REMAIN UNTOUCHED UNTIL THE RULES SAY OTHERWISE.

THE PLAN IS PINNED, NOT RESTATED
--------------------------------
`PREREGISTRATION_SHA256` below is the hash of
`docs/PHASE_25_9I_F6C_PREREGISTRATION.md` as committed at 2f1977f, before
this file existed. `preflight()` refuses to run if the document no longer
hashes to it. Editing the plan after seeing a result therefore breaks the
harness rather than quietly changing the experiment.

The dataset and the holdout boundary are read from the F.7 handoff, not
declared here, and are checked against the snapshot registry.

THREE WINDOWS, NOT TWO
----------------------
F.6B split each fold into train and test. A calibration experiment needs
a third window, because a calibrator fitted on the data used to score it
would be marking its own homework:

    TRAIN  --purge-->  CALIBRATE  --purge+embargo-->  TEST

That costs calendar. Pre-registration section 5 records the price: d5
drops to two valid folds and d10 to zero, so this phase can only ask the
question on d1 and d3. The boundary is not moved to rescue them.

TWO DIMENSIONS, NEVER COMBINED
------------------------------
Discrimination (can it separate up from down?) and calibration (when it
says P, is P trustworthy?) are scored separately and gated separately,
in that order. A monotone calibrator cannot change ROC-AUC at all --
which is the clearest statement of why calibration is not a rescue.

REUSED, NOT REBUILT
-------------------
  dataset        `scripts/train_models.load_dataset`
  candidate      `research_model_quality.spec_for("C1")`, verbatim
  purge/embargo  `src/modeling/splits`
  fit + baselines + canonical gate
                 `ModelingEngine.train_and_evaluate`, untouched
  probabilities  `src/modeling/algorithms.predict_batch`
  calibration    `src/modeling/calibration`
  marker tables  F.6B's `research_executions` /
                 `research_candidate_results`, same schema

F.6C adds requirements to the canonical gate. It never relaxes one, and
it reports `canonically_qualified` and `f6c_qualified` side by side so
the distinction cannot be lost.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts import research_model_quality as rmq                    # noqa: E402
from scripts.research_f6b import (                                   # noqa: E402
    find_execution, initialize_execution_schema,
)
from scripts.train_models import load_dataset                        # noqa: E402
from src.adaptive import research_snapshot as rs                     # noqa: E402
from src.adaptive.corpus_versioning import (                         # noqa: E402
    current_commit, file_digest, initialize_recovery_schema,
    load_snapshot, verify_snapshot_hash,
)
from src.modeling import algorithms, calibration as cal, splits      # noqa: E402
from src.modeling.engine import ModelingEngine                       # noqa: E402
from src.domain.model_models import TrainingWindow                   # noqa: E402

UTC = timezone.utc

TARGET_DB = os.path.join(ROOT, "data", "marketlens_corpus_v2.db")
HANDOFF_PATH = os.path.join(ROOT, "docs", "PHASE_25_9I_F6B_HANDOFF.json")
PREREGISTRATION = os.path.join(ROOT, "docs",
                               "PHASE_25_9I_F6C_PREREGISTRATION.md")
RESULT_PATH = os.path.join(ROOT, "docs", "PHASE_25_9I_F6C_RESULTS.json")

PHASE = "25.9I-F.6C"

#: The plan's hash, as committed at 2f1977f before this file existed.
PREREGISTRATION_SHA256 = (
    "ba141fd9fdead1bc8b56d524a0596b03f7f8a97bde5b98bc8caaf9c8ede66ede")

#: Pre-registration section 5. d5 and d10 are deferred on measured
#: geometry, not on any result.
HORIZONS: Tuple[str, ...] = ("d1", "d3")
DEFERRED: Dict[str, str] = {
    "d5": "DEFERRED_INSUFFICIENT_FOLDS",
    "d10": "DEFERRED_INSUFFICIENT_HOLDOUT",
    "intraday_5m": "DEFERRED_BY_F7_POLICY",
    "intraday_15m": "DEFERRED_BY_F7_POLICY",
    "intraday_30m": "DEFERRED_BY_F7_POLICY",
    "intraday_60m": "DEFERRED_BY_F7_POLICY",
    "d20": "PROTECTED",
}

#: Pre-registration section 7. Both kept; the fingerprint decides where
#: they are the same evidence.
FEATURE_CONFIGS: Tuple[str, ...] = ("all_numeric", "dense_only")

#: Pre-registration section 8. `raw` is the control arm.
METHODS: Tuple[str, ...] = cal.CALIBRATION_METHODS

#: Pre-registration section 6. Inherited or derived; nothing tuned.
CALIBRATION_DAYS = 7
TEST_DAYS = 7
STEP_DAYS = 7
MIN_TRAIN_ROWS = 30
MIN_CALIBRATION_ROWS = 30
MIN_TEST_ROWS = 20

#: Pre-registration section 11. The no-skill points of the metrics,
#: not chosen values.
NO_SKILL_BALANCED_ACCURACY = 0.5
NO_SKILL_AUC = 0.5
NO_SKILL_BRIER_SKILL = 0.0

#: The one specification, Phase 25.9's, imported verbatim.
CANDIDATE = "C1"

RELIABILITY_BINS = 5


class FrozenPlanViolation(RuntimeError):
    """The plan and the artifacts disagree, or this has already been run."""


class ProtectedHorizonRequested(RuntimeError):
    """Someone pointed the researcher at a sealed horizon."""


# ======================================================================
# Feature configuration identity
# ======================================================================

def feature_fingerprint(names: Sequence[str],
                        columns: Sequence[int]) -> str:
    """
    SHA-256 of the sorted effective column names.

    Two configurations that fingerprint identically in a fold ARE the
    same evidence in that fold. Computed from column identities only --
    no label value enters it, so it can be computed before any outcome.
    """
    payload = json.dumps(sorted(names[c] for c in columns),
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def columns_for(config: str, names: Sequence[str],
                X: Sequence[Sequence[Optional[float]]],
                train_idx: Sequence[int]) -> List[int]:
    if config == "all_numeric":
        return list(range(len(names)))
    if config == "dense_only":
        return rmq.dense_columns(X, train_idx)
    raise ValueError(f"unknown feature configuration {config!r}")


# ======================================================================
# Three-window folds
# ======================================================================

def build_f6c_folds(cutoffs: Sequence[datetime], purge_days: float, *,
                    holdout_start: datetime,
                    embargo_days: float,
                    calibration_days: int = CALIBRATION_DAYS,
                    test_days: int = TEST_DAYS,
                    step_days: int = STEP_DAYS,
                    min_train_rows: int = MIN_TRAIN_ROWS,
                    min_calibration_rows: int = MIN_CALIBRATION_ROWS,
                    min_test_rows: int = MIN_TEST_ROWS
                    ) -> List[Dict[str, Any]]:
    """
    Expanding folds with a calibration window between train and test.

    Purge is applied at BOTH internal boundaries, because a label at T
    is unresolved until T + horizon and that hazard exists at the
    train/calibration boundary exactly as at the calibration/test one.
    Embargo -- Phase 25.9's extra buffer at a TEST boundary -- is applied
    only before the test window, which is the boundary it was written
    for.
    """
    region = [i for i, c in enumerate(cutoffs) if c < holdout_start]
    if not region:
        return []
    start = min(cutoffs[i] for i in region)
    folds: List[Dict[str, Any]] = []

    test_start = start + timedelta(
        days=2 * purge_days + embargo_days + calibration_days + 1)
    while test_start < holdout_start:
        test_end = min(test_start + timedelta(days=test_days), holdout_start)
        calibration_end = test_start - timedelta(days=purge_days + embargo_days)
        calibration_start = calibration_end - timedelta(days=calibration_days)
        train_end = calibration_start - timedelta(days=purge_days)

        candidate_train = [i for i in region if cutoffs[i] < train_end]
        kept, purged = splits.purge(candidate_train, calibration_start,
                                    lambda i: cutoffs[i], purge_days)
        calibration_idx = [i for i in region
                           if calibration_start <= cutoffs[i] < calibration_end]
        calibration_kept, calibration_purged = splits.purge(
            calibration_idx, test_start, lambda i: cutoffs[i], purge_days)
        calibration_kept, calibration_embargoed = splits.embargo(
            calibration_kept, test_start, lambda i: cutoffs[i], embargo_days)
        test_idx = [i for i in region if test_start <= cutoffs[i] < test_end]

        folds.append({
            "label": f"wf{len(folds) + 1}",
            "train_idx": kept,
            "calibration_idx": calibration_kept,
            "test_idx": test_idx,
            "train_end": train_end,
            "calibration_start": calibration_start,
            "calibration_end": calibration_end,
            "test_start": test_start, "test_end": test_end,
            "train_purged": len(purged),
            "calibration_purged": len(calibration_purged),
            "calibration_embargoed": len(calibration_embargoed),
            "valid": (len(kept) >= min_train_rows
                      and len(calibration_kept) >= min_calibration_rows
                      and len(test_idx) >= min_test_rows),
        })
        test_start = test_start + timedelta(days=step_days)
    return folds


def holdout_split(cutoffs: Sequence[datetime], purge_days: float, *,
                  holdout_start: datetime, embargo_days: float,
                  calibration_days: int = CALIBRATION_DAYS
                  ) -> Dict[str, Any]:
    """
    The final split: train, then a calibration window, then the holdout.

    Same three-window shape as a research fold, so a survivor is judged
    the way it was selected.
    """
    region = [i for i, c in enumerate(cutoffs) if c < holdout_start]
    test_idx = [i for i, c in enumerate(cutoffs) if c >= holdout_start]

    calibration_end = holdout_start - timedelta(days=purge_days + embargo_days)
    calibration_start = calibration_end - timedelta(days=calibration_days)
    train_end = calibration_start - timedelta(days=purge_days)

    candidate_train = [i for i in region if cutoffs[i] < train_end]
    kept, purged = splits.purge(candidate_train, calibration_start,
                                lambda i: cutoffs[i], purge_days)
    calibration_idx = [i for i in region
                       if calibration_start <= cutoffs[i] < calibration_end]
    calibration_kept, calibration_purged = splits.purge(
        calibration_idx, holdout_start, lambda i: cutoffs[i], purge_days)
    calibration_kept, calibration_embargoed = splits.embargo(
        calibration_kept, holdout_start, lambda i: cutoffs[i], embargo_days)

    return {
        "label": "holdout",
        "train_idx": kept, "calibration_idx": calibration_kept,
        "test_idx": test_idx,
        "train_end": train_end,
        "calibration_start": calibration_start,
        "calibration_end": calibration_end,
        "test_start": holdout_start,
        "test_end": max(cutoffs[i] for i in test_idx) if test_idx else None,
        "train_purged": len(purged),
        "calibration_purged": len(calibration_purged),
        "calibration_embargoed": len(calibration_embargoed),
        "valid": (bool(test_idx) and len(kept) >= MIN_TRAIN_ROWS
                  and len(calibration_kept) >= MIN_CALIBRATION_ROWS),
    }


# ======================================================================
# Pre-flight
# ======================================================================

def read_handoff(path: str = HANDOFF_PATH) -> Dict[str, Any]:
    if not os.path.exists(path):
        raise FrozenPlanViolation(f"the F.7 handoff is missing: {path}")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def preflight(db_path: str = TARGET_DB) -> Dict[str, Any]:
    """
    Verify the frozen plan, the dataset and F.6B's preservation.

    Raises on any disagreement. A blocked result is preferable to a
    compromised one, so none of these checks warns and continues.
    """
    problems: List[str] = []

    if not os.path.exists(PREREGISTRATION):
        raise FrozenPlanViolation(
            f"the pre-registration is missing: {PREREGISTRATION}")
    prereg_hash = file_digest(PREREGISTRATION)
    if prereg_hash != PREREGISTRATION_SHA256:
        raise FrozenPlanViolation(
            f"the pre-registration has changed since it was frozen: expected "
            f"{PREREGISTRATION_SHA256[:16]}..., found {prereg_hash[:16]}...")

    handoff = read_handoff()
    if not os.path.exists(db_path):
        raise FrozenPlanViolation(f"the research snapshot is missing: {db_path}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        snapshot = load_snapshot(conn, handoff["snapshot"]["snapshot_id"])
        if snapshot is None:
            raise FrozenPlanViolation("the research snapshot is not registered")
        if snapshot.content_hash != handoff["snapshot"]["content_hash"]:
            problems.append("snapshot hash differs from the handoff")
        if not verify_snapshot_hash(conn, snapshot):
            problems.append("the snapshot content no longer hashes to its "
                            "frozen value -- the dataset has changed")
        if not snapshot.state.is_research_input:
            problems.append(f"snapshot state is {snapshot.state.value}")

        derived = rs.deterministic_holdout(conn,
                                           snapshot_id=snapshot.snapshot_id)
        frozen_boundary = handoff["holdout"]["boundary"]
        if derived.boundary.isoformat() != frozen_boundary:
            problems.append("the holdout rule no longer yields the inherited "
                            "boundary")

        # F.6B must still be exactly as it was.
        f6b = find_execution(
            conn, preregistration_hash=handoff["preregistration_hash"],
            snapshot_hash=handoff["snapshot"]["content_hash"])
        if f6b is None or f6b["status"] != "complete":
            problems.append("F.6B's completed execution marker is missing")
        elif f6b["qualified_count"] != 0:
            problems.append("F.6B's recorded qualified count is no longer 0")
        f6b_rows = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(qualified), 0) "
            "FROM research_candidate_results WHERE execution_id = ?",
            (f6b["execution_id"],)).fetchone() if f6b else (0, 0)
        if f6b and f6b_rows != (16, 0):
            problems.append(f"F.6B's candidate rows changed: {f6b_rows}")

        existing = find_execution(
            conn, preregistration_hash=prereg_hash,
            snapshot_hash=snapshot.content_hash)

        stale = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("trained_models", "model_evaluations", "predictions")}
    finally:
        conn.close()

    for horizon in HORIZONS:
        if rs.is_protected(horizon):
            problems.append(f"{horizon} is protected and cannot be researched")
        if horizon not in handoff["purge_days"]:
            problems.append(f"no purge horizon declared for {horizon}")
    if "d20" not in DEFERRED or DEFERRED["d20"] != "PROTECTED":
        problems.append("the protected horizon is not marked PROTECTED")

    if problems:
        raise FrozenPlanViolation("; ".join(problems))

    return {
        "phase": PHASE,
        "preregistration_path": os.path.relpath(PREREGISTRATION, ROOT),
        "preregistration_hash": prereg_hash,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_hash": snapshot.content_hash,
        "corpus_version_id": snapshot.corpus_version_id,
        "dataset_version": snapshot.dataset_version.fingerprint(),
        "as_of_cutoff": snapshot.as_of_cutoff.isoformat(),
        "holdout_boundary": frozen_boundary,
        "holdout_origin": "inherited unchanged from the F.7 handoff",
        "holdout_opened_by_f6b": False,
        "horizons": list(HORIZONS),
        "deferred": dict(DEFERRED),
        "feature_configs": list(FEATURE_CONFIGS),
        "calibration_methods": list(METHODS),
        "purge_days": {h: float(handoff["purge_days"][h]) for h in HORIZONS},
        "embargo_days": float(handoff["embargo_days"]),
        "calibration_days": CALIBRATION_DAYS,
        "test_days": TEST_DAYS,
        "step_days": STEP_DAYS,
        "candidate": CANDIDATE,
        "qualification_gate": handoff["qualification_gate_reference"],
        "no_skill": {"balanced_accuracy": NO_SKILL_BALANCED_ACCURACY,
                     "roc_auc": NO_SKILL_AUC,
                     "brier_skill_score": NO_SKILL_BRIER_SKILL},
        "f6b_preserved": {
            "execution_id": f6b["execution_id"] if f6b else None,
            "status": f6b["status"] if f6b else None,
            "qualified_count": f6b["qualified_count"] if f6b else None,
            "candidate_rows": f6b_rows[0] if f6b else 0,
            "qualified_rows": f6b_rows[1] if f6b else 0,
        },
        "stale_experiment_rows": stale,
        "existing_execution": existing,
        "code_commit": current_commit(ROOT),
        "d20_excluded": True,
    }


# ======================================================================
# Cell enumeration
# ======================================================================

def enumerate_cells(context: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Every research cell, in a fixed order. 2 horizons x 2 configs x 3 arms.

    Raises if the count is not the frozen 12, so a horizon or method
    cannot be appended after a result.
    """
    cells: List[Dict[str, Any]] = []
    for horizon in context["horizons"]:
        for config in context["feature_configs"]:
            for method in context["calibration_methods"]:
                cells.append({
                    "cell_id": f"{horizon}:{config}:{method}",
                    "horizon_id": horizon,
                    "feature_config": config,
                    "calibration_method": method,
                    "candidate": CANDIDATE,
                    "model_family": rmq.spec_for(CANDIDATE).family.value,
                    "task": rmq.spec_for(CANDIDATE).task.value,
                    "purge_days": context["purge_days"][horizon],
                    "embargo_days": context["embargo_days"],
                })
    expected = (len(context["horizons"]) * len(context["feature_configs"])
                * len(context["calibration_methods"]))
    if len(cells) != expected or expected != 12:
        raise FrozenPlanViolation(
            f"the enumerated cell list holds {len(cells)} cells; the frozen "
            f"budget is 12")
    return cells


def cell_list_hash(cells: Sequence[Dict[str, Any]]) -> str:
    payload = json.dumps([c["cell_id"] for c in cells], separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ======================================================================
# One fold, one (horizon, config): shared model fit, three arms
# ======================================================================

def evaluate_window(horizon_id: str, config: str, fold: Dict[str, Any],
                    names, X, Y, clusters, cutoffs) -> Dict[str, Any]:
    """
    Fit on TRAIN, calibrate on CALIBRATION, score on TEST.

    The model is fitted ONCE and shared by the three calibration arms,
    because they differ only in the probability map applied afterwards.
    That is also why they are not three independent models.
    """
    train_idx = fold["train_idx"]
    calibration_idx = fold["calibration_idx"]
    test_idx = fold["test_idx"]

    columns = columns_for(config, names, X, train_idx)
    fingerprint = feature_fingerprint(names, columns)
    column_names = [names[c] for c in columns]

    X_train = rmq.select([X[i] for i in train_idx], columns)
    Y_train = [Y[i] for i in train_idx]
    X_cal = rmq.select([X[i] for i in calibration_idx], columns)
    Y_cal = [Y[i] for i in calibration_idx]
    X_test = rmq.select([X[i] for i in test_idx], columns)
    Y_test = [Y[i] for i in test_idx]

    spec = rmq.spec_for(CANDIDATE)
    window = TrainingWindow(
        label=fold["label"],
        train_start=min(cutoffs[i] for i in train_idx) if train_idx else None,
        train_end=max(cutoffs[i] for i in train_idx) if train_idx else None,
        test_start=fold["test_start"], test_end=fold["test_end"],
        train_size=len(train_idx), test_size=len(test_idx),
        purged_count=fold["train_purged"] + fold["calibration_purged"],
        embargoed_count=fold["calibration_embargoed"])

    engine = ModelingEngine()
    model, evaluation = engine.train_and_evaluate(
        spec, X_train, Y_train, X_test, Y_test,
        feature_names=column_names, window=window,
        cluster_count=len({clusters[i] for i in test_idx}))

    raw_cal_probs = algorithms.predict_batch(model.parameters, X_cal)
    raw_test_probs = algorithms.predict_batch(model.parameters, X_test)

    # Base rate from TRAIN u CALIBRATION -- the information available
    # before the test window opens, never the test outcomes.
    prior_outcomes = [y for y in (Y_train + Y_cal) if y is not None]
    base_rate = (sum(1 for y in prior_outcomes if y > 0) / len(prior_outcomes)
                 if prior_outcomes else None)

    arms: Dict[str, Any] = {}
    for method in METHODS:
        fit = cal.fit_calibrator(method, raw_cal_probs, Y_cal)
        calibrated = fit.apply(raw_test_probs)
        arms[method] = {
            "calibrator": fit.describe(),
            "report": cal.probability_report(
                calibrated, Y_test, base_rate=base_rate,
                bin_count=RELIABILITY_BINS),
        }

    return {
        "fold": fold["label"],
        "feature_fingerprint": fingerprint,
        "features": len(columns),
        "train_rows": len(train_idx),
        "calibration_rows": len(calibration_idx),
        "test_rows": len(test_idx),
        "test_clusters": len({clusters[i] for i in test_idx}),
        "train_purged": fold["train_purged"],
        "calibration_purged": fold["calibration_purged"],
        "calibration_embargoed": fold["calibration_embargoed"],
        "train_end": fold["train_end"].isoformat(),
        "calibration_start": fold["calibration_start"].isoformat(),
        "calibration_end": fold["calibration_end"].isoformat(),
        "test_start": fold["test_start"].isoformat(),
        "test_end": fold["test_end"].isoformat() if fold["test_end"] else None,
        "base_rate": None if base_rate is None else round(base_rate, 6),
        "canonical": {
            "metrics": dict(evaluation.metrics),
            "baselines": {c.baseline_name: {
                "metric": c.metric_name, "baseline_score": c.baseline_score,
                "model_score": c.model_score, "beats": c.beats_baseline}
                for c in evaluation.baseline_comparisons},
            "beats_all_baselines": evaluation.beats_all_baselines,
            "effective_sample_size": evaluation.effective_sample_size,
            "small_sample": evaluation.small_sample,
            "is_deployable": evaluation.is_deployable,
        },
        "raw_discrimination": {
            "roc_auc": cal.roc_auc(raw_test_probs, Y_test),
            "balanced_accuracy": cal.balanced_accuracy(raw_test_probs, Y_test),
        },
        "arms": arms,
    }


# ======================================================================
# Gates
# ======================================================================

def _majority(wins: int, judged: int) -> bool:
    return wins > judged / 2 if judged else False


def apply_gates(fold_results: Sequence[Dict[str, Any]], method: str
                ) -> Dict[str, Any]:
    """
    Pre-registration section 13, applied in order and stopping at the
    first failure.

    `NOT_REACHED` is kept distinct from `FAIL`: a gate that was never
    evaluated has not been passed.
    """
    judged = len(fold_results)
    gates: Dict[str, Any] = {}
    reasons: List[str] = []

    # 1. sample / data integrity
    clusters = sum(f["test_clusters"] for f in fold_results)
    sample_ok = judged >= rs.MIN_VALID_FOLDS and clusters >= rs.MIN_EFFECTIVE_SAMPLE
    gates["sample"] = {
        "status": "PASS" if sample_ok else "FAIL",
        "folds": judged, "min_folds": rs.MIN_VALID_FOLDS,
        "pooled_test_clusters": clusters,
        "min_effective_sample": rs.MIN_EFFECTIVE_SAMPLE,
    }
    if not sample_ok:
        reasons.append(f"{judged} fold(s), {clusters} pooled test clusters")
        for name in ("discrimination", "absolute_quality", "robustness",
                     "calibration", "holdout"):
            gates[name] = {"status": "NOT_REACHED"}
        return {"gates": gates, "failed": ["FAILED_SAMPLE"],
                "survived": False, "reasons": reasons}

    # 2. discrimination -- canonical baselines AND the no-skill floors
    baseline_wins = sum(1 for f in fold_results
                        if f["canonical"]["beats_all_baselines"] is True)
    baseline_judged = sum(1 for f in fold_results
                          if f["canonical"]["beats_all_baselines"] is not None)
    auc_wins = sum(1 for f in fold_results
                   if (f["arms"][method]["report"]["discrimination"]["roc_auc"]
                       or 0.0) > NO_SKILL_AUC)
    balanced_wins = sum(
        1 for f in fold_results
        if (f["arms"][method]["report"]["discrimination"]["balanced_accuracy"]
            or 0.0) > NO_SKILL_BALANCED_ACCURACY)

    baseline_ok = _majority(baseline_wins, baseline_judged)
    auc_ok = _majority(auc_wins, judged)
    balanced_ok = _majority(balanced_wins, judged)

    gates["discrimination"] = {
        "status": "PASS" if baseline_ok else "FAIL",
        "baseline_wins": baseline_wins, "baseline_judged": baseline_judged,
    }
    gates["absolute_quality"] = {
        "status": "PASS" if (auc_ok and balanced_ok) else "FAIL",
        "auc_above_no_skill": f"{auc_wins}/{judged}",
        "balanced_above_no_skill": f"{balanced_wins}/{judged}",
        "no_skill_point": NO_SKILL_AUC,
    }
    if not baseline_ok:
        reasons.append(f"beats baselines in {baseline_wins}/{baseline_judged} "
                       f"folds; a strict majority is required")
    if not auc_ok:
        reasons.append(f"ROC-AUC above {NO_SKILL_AUC} in only "
                       f"{auc_wins}/{judged} folds")
    if not balanced_ok:
        reasons.append(f"balanced accuracy above "
                       f"{NO_SKILL_BALANCED_ACCURACY} in only "
                       f"{balanced_wins}/{judged} folds")

    if not (baseline_ok and auc_ok and balanced_ok):
        for name in ("robustness", "calibration", "holdout"):
            gates[name] = {"status": "NOT_REACHED"}
        failed = []
        if not baseline_ok:
            failed.append("FAILED_BASELINE")
        if not (auc_ok and balanced_ok):
            failed.append("FAILED_ABSOLUTE_QUALITY")
        return {"gates": gates, "failed": failed, "survived": False,
                "reasons": reasons}

    # 3. robustness -- the strict-majority requirement already enforces
    #    that no single fold can carry a candidate.
    gates["robustness"] = {
        "status": "PASS",
        "rule": "strict majority across folds; no single fold can carry a "
                "candidate",
    }

    # 4. calibration
    skill_wins = sum(
        1 for f in fold_results
        if (f["arms"][method]["report"]["calibration"]["brier_skill_score"]
            or -1.0) > NO_SKILL_BRIER_SKILL)
    calibration_ok = _majority(skill_wins, judged)
    gates["calibration"] = {
        "status": "PASS" if calibration_ok else "FAIL",
        "brier_skill_above_zero": f"{skill_wins}/{judged}",
        "no_skill_point": NO_SKILL_BRIER_SKILL,
    }
    if not calibration_ok:
        reasons.append(f"Brier skill score above 0 in only "
                       f"{skill_wins}/{judged} folds")
        gates["holdout"] = {"status": "NOT_REACHED"}
        return {"gates": gates, "failed": ["FAILED_CALIBRATION"],
                "survived": False, "reasons": reasons}

    gates["holdout"] = {"status": "PENDING"}
    return {"gates": gates, "failed": [], "survived": True, "reasons": reasons}


def stability(fold_results: Sequence[Dict[str, Any]], method: str
              ) -> Dict[str, Any]:
    def series(path: Tuple[str, str]) -> List[float]:
        out = []
        for f in fold_results:
            value = f["arms"][method]["report"][path[0]][path[1]]
            if value is not None:
                out.append(value)
        return out

    summary: Dict[str, Any] = {}
    for label, path in (("roc_auc", ("discrimination", "roc_auc")),
                        ("balanced_accuracy",
                         ("discrimination", "balanced_accuracy")),
                        ("brier", ("calibration", "brier")),
                        ("brier_skill_score",
                         ("calibration", "brier_skill_score"))):
        values = series(path)
        summary[label] = {
            "folds": len(values),
            "mean": round(sum(values) / len(values), 6) if values else None,
            "worst": round(min(values), 6) if values else None,
            "best": round(max(values), 6) if values else None,
            "spread": round(max(values) - min(values), 6) if values else None,
        }
    return summary


# ======================================================================
# Plan
# ======================================================================

def build_plan(db_path: str = TARGET_DB) -> Dict[str, Any]:
    context = preflight(db_path)
    cells = enumerate_cells(context)
    holdout_start = datetime.fromisoformat(context["holdout_boundary"])

    per_horizon: List[Dict[str, Any]] = []
    model_fits = 0
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for horizon in context["horizons"]:
            names, X, Y, cutoffs, clusters, _, quality = load_dataset(
                conn, f"{horizon}.abnormal_return")
            folds = build_f6c_folds(
                cutoffs, context["purge_days"][horizon],
                holdout_start=holdout_start,
                embargo_days=context["embargo_days"])
            valid = [f for f in folds if f["valid"]]
            final = holdout_split(cutoffs, context["purge_days"][horizon],
                                  holdout_start=holdout_start,
                                  embargo_days=context["embargo_days"])
            fingerprints = {
                config: [feature_fingerprint(
                    names, columns_for(config, names, X, f["train_idx"]))
                    for f in valid]
                for config in context["feature_configs"]}
            collapsed = sum(
                1 for i in range(len(valid))
                if fingerprints["all_numeric"][i] == fingerprints["dense_only"][i])
            model_fits += len(valid) * len(context["feature_configs"])
            per_horizon.append({
                "horizon": horizon,
                "rows": len(X), "features": len(names),
                "clusters": len(set(clusters)),
                "research_region_rows": sum(1 for c in cutoffs
                                            if c < holdout_start),
                "holdout_rows": len(final["test_idx"]),
                "folds_built": len(folds), "folds_valid": len(valid),
                "fold_detail": [
                    {"label": f["label"], "train": len(f["train_idx"]),
                     "calibration": len(f["calibration_idx"]),
                     "test": len(f["test_idx"]),
                     "positive_rate_test": cal.positive_rate(
                         [Y[i] for i in f["test_idx"]]),
                     "positive_rate_calibration": cal.positive_rate(
                         [Y[i] for i in f["calibration_idx"]]),
                     "valid": f["valid"]}
                    for f in folds],
                "holdout_valid": final["valid"],
                "holdout_train": len(final["train_idx"]),
                "holdout_calibration": len(final["calibration_idx"]),
                "feature_fingerprints": fingerprints,
                "folds_where_configs_collapse": collapsed,
                "quality": dict(quality),
            })
    finally:
        conn.close()

    return {
        "context": context,
        "cells": cells,
        "cell_list_hash": cell_list_hash(cells),
        "total_cells": len(cells),
        "per_horizon": per_horizon,
        "expected_model_fits": model_fits,
        "expected_calibrator_fits": model_fits * 2,
        "expected_holdout_model_fits_max": (
            len(context["horizons"]) * len(context["feature_configs"])),
        "abstention": "NOT TESTED (pre-registration section 16)",
        "market_data_requests": 0,
        "paid_api_calls": 0,
    }


# ======================================================================
# Run
# ======================================================================

def run(db_path: str = TARGET_DB, *, verbose: bool = True) -> Dict[str, Any]:
    plan = build_plan(db_path)
    context = plan["context"]
    cells = plan["cells"]

    existing = context.get("existing_execution")
    if existing and existing["status"] == "complete":
        raise FrozenPlanViolation(
            f"this frozen experiment has already been executed as "
            f"{existing['execution_id']} ({existing['completed_at']}). "
            f"Re-running it would not be independent evidence -- use --show.")

    execution_id = f"f6c-{uuid.uuid4().hex[:16]}"
    started_at = datetime.now(UTC)
    wall = time.time()
    holdout_start = datetime.fromisoformat(context["holdout_boundary"])

    conn = sqlite3.connect(db_path)
    try:
        initialize_recovery_schema(conn)
        initialize_execution_schema(conn)
        conn.execute("""
            INSERT INTO research_executions
                (execution_id, phase, preregistration_path,
                 preregistration_hash, snapshot_id, snapshot_hash,
                 corpus_version_id, holdout_boundary, code_commit, started_at,
                 status, cells_json, completed_cells_json, note)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (execution_id, PHASE, context["preregistration_path"],
              context["preregistration_hash"], context["snapshot_id"],
              context["snapshot_hash"], context["corpus_version_id"],
              context["holdout_boundary"], context["code_commit"],
              started_at.isoformat(), "running",
              json.dumps([c["cell_id"] for c in cells]), "[]",
              f"cell_list_hash={plan['cell_list_hash']}"))
        conn.commit()
    finally:
        conn.close()

    if verbose:
        print(f"PHASE {PHASE} EXECUTION {execution_id}")
        print(f"  snapshot : {context['snapshot_id']} "
              f"({context['snapshot_hash'][:16]}...)")
        print(f"  prereg   : {context['preregistration_hash'][:16]}...")
        print(f"  holdout  : {context['holdout_boundary']} "
              f"({context['holdout_origin']})")
        print(f"  cells    : {len(cells)}  hash={plan['cell_list_hash'][:16]}...")
        print(f"  commit   : {context['code_commit'][:12]}")
        print()

    # ---- stage 1: research region ----
    results: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []
    status = "complete"
    datasets: Dict[str, Any] = {}

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for horizon in context["horizons"]:
            if rs.is_protected(horizon):
                raise ProtectedHorizonRequested(horizon)
            names, X, Y, cutoffs, clusters, _, quality = load_dataset(
                conn, f"{horizon}.abnormal_return")
            datasets[horizon] = (names, X, Y, cutoffs, clusters)
            purge_days = context["purge_days"][horizon]
            folds = [f for f in build_f6c_folds(
                cutoffs, purge_days, holdout_start=holdout_start,
                embargo_days=context["embargo_days"]) if f["valid"]]

            for config in context["feature_configs"]:
                try:
                    fold_results = [
                        evaluate_window(horizon, config, fold, names, X, Y,
                                        clusters, cutoffs)
                        for fold in folds]
                except Exception as error:               # noqa: BLE001
                    errors.append(f"{horizon}:{config}: "
                                  f"{type(error).__name__}: {error}")
                    status = "failed"
                    break
                for method in context["calibration_methods"]:
                    cell_id = f"{horizon}:{config}:{method}"
                    verdict = apply_gates(fold_results, method)
                    results[cell_id] = {
                        "cell_id": cell_id, "horizon": horizon,
                        "feature_config": config, "calibration_method": method,
                        "candidate": CANDIDATE,
                        "model_family": rmq.spec_for(CANDIDATE).family.value,
                        "folds": [
                            {"fold": f["fold"],
                             "feature_fingerprint": f["feature_fingerprint"],
                             "features": f["features"],
                             "train_rows": f["train_rows"],
                             "calibration_rows": f["calibration_rows"],
                             "test_rows": f["test_rows"],
                             "test_clusters": f["test_clusters"],
                             "base_rate": f["base_rate"],
                             "canonical": f["canonical"],
                             "raw_discrimination": f["raw_discrimination"],
                             "calibrator": f["arms"][method]["calibrator"],
                             "report": f["arms"][method]["report"]}
                            for f in fold_results],
                        "stability": stability(fold_results, method),
                        "gates": verdict["gates"],
                        "failed_gates": verdict["failed"],
                        "survived_pre_holdout": verdict["survived"],
                        "gate_reasons": verdict["reasons"],
                        "feature_fingerprints": [f["feature_fingerprint"]
                                                 for f in fold_results],
                    }
                    if verbose:
                        tail = "; ".join(verdict["reasons"])[:72]
                        print(f"    {cell_id:<28} "
                              f"{'SURVIVES' if verdict['survived'] else ','.join(verdict['failed']):<34}"
                              f" {tail}", flush=True)
                if status == "failed":
                    break
            if status == "failed":
                break
    finally:
        conn.close()

    # ---- survivor list, frozen BEFORE the holdout is touched ----
    survivors = sorted(c for c, r in results.items()
                       if r["survived_pre_holdout"])
    survivor_hash = hashlib.sha256(
        json.dumps(survivors, separators=(",", ":")).encode()).hexdigest()
    holdout_opened = False

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE research_executions SET note = ? "
                     "WHERE execution_id = ?",
                     (f"cell_list_hash={plan['cell_list_hash']}; "
                      f"survivors={len(survivors)}; "
                      f"survivor_hash={survivor_hash}", execution_id))
        conn.commit()
    finally:
        conn.close()

    if verbose:
        print()
        print(f"  pre-holdout survivors: {len(survivors)} {survivors}")
        print(f"  survivor list hash   : {survivor_hash[:16]}...")

    # ---- stage 2: the holdout, survivors only, once ----
    if survivors and status != "failed":
        holdout_opened = True
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            for cell_id in survivors:
                record = results[cell_id]
                horizon = record["horizon"]
                names, X, Y, cutoffs, clusters = datasets[horizon]
                final = holdout_split(
                    cutoffs, context["purge_days"][horizon],
                    holdout_start=holdout_start,
                    embargo_days=context["embargo_days"])
                if not final["valid"]:
                    record["holdout"] = {
                        "evaluated": False,
                        "reason": "the holdout split is not usable for this "
                                  "horizon"}
                    record["gates"]["holdout"] = {"status": "FAIL"}
                    record["failed_gates"].append(
                        "FAILED_OTHER_CANONICAL_GATE")
                    continue
                outcome = evaluate_window(horizon, record["feature_config"],
                                          final, names, X, Y, clusters, cutoffs)
                arm = outcome["arms"][record["calibration_method"]]
                report = arm["report"]
                canonical = outcome["canonical"]
                passes = (
                    canonical["is_deployable"] is True
                    and (report["discrimination"]["roc_auc"] or 0.0) > NO_SKILL_AUC
                    and (report["discrimination"]["balanced_accuracy"] or 0.0)
                    > NO_SKILL_BALANCED_ACCURACY
                    and (report["calibration"]["brier_skill_score"] or -1.0)
                    > NO_SKILL_BRIER_SKILL)
                record["holdout"] = {
                    "evaluated": True,
                    "train_rows": outcome["train_rows"],
                    "calibration_rows": outcome["calibration_rows"],
                    "test_rows": outcome["test_rows"],
                    "test_clusters": outcome["test_clusters"],
                    "base_rate": outcome["base_rate"],
                    "canonical": canonical,
                    "calibrator": arm["calibrator"],
                    "report": report,
                    "passes_f6c_holdout": passes,
                }
                record["gates"]["holdout"] = {
                    "status": "PASS" if passes else "FAIL"}
                if not passes:
                    record["failed_gates"].append("FAILED_HOLDOUT")
        finally:
            conn.close()

    for record in results.values():
        record.setdefault("holdout", None)
        record["canonically_qualified"] = bool(
            record.get("holdout")
            and record["holdout"].get("evaluated")
            and record["holdout"]["canonical"]["is_deployable"] is True)
        record["f6c_qualified"] = bool(
            record["survived_pre_holdout"]
            and record.get("holdout")
            and record["holdout"].get("evaluated")
            and record["holdout"].get("passes_f6c_holdout"))
        record["verdict"] = ("QUALIFIED_DIRECTIONAL_RESEARCH_CANDIDATE"
                             if record["f6c_qualified"]
                             else (record["failed_gates"][0]
                                   if record["failed_gates"]
                                   else "NOT_QUALIFIED"))

    completed = sorted(results)
    if status != "failed" and len(completed) != len(cells):
        status = "partial"
    completed_at = datetime.now(UTC)
    qualified = [c for c, r in results.items() if r["f6c_qualified"]]
    calibration_passed = [
        c for c, r in results.items()
        if r["gates"].get("calibration", {}).get("status") == "PASS"]

    result = {
        "execution_id": execution_id, "phase": PHASE, "status": status,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "runtime_seconds": round(time.time() - wall, 2),
        "frozen_plan": {
            k: context[k] for k in (
                "preregistration_path", "preregistration_hash", "snapshot_id",
                "snapshot_hash", "corpus_version_id", "dataset_version",
                "as_of_cutoff", "holdout_boundary", "holdout_origin",
                "horizons", "deferred", "feature_configs",
                "calibration_methods", "purge_days", "embargo_days",
                "calibration_days", "test_days", "step_days", "candidate",
                "qualification_gate", "no_skill", "code_commit")},
        "cell_list_hash": plan["cell_list_hash"],
        "cells": cells,
        "cells_evaluated": completed,
        "cells_unevaluated": [c["cell_id"] for c in cells
                              if c["cell_id"] not in results],
        "per_horizon": plan["per_horizon"],
        "candidates": [results[c] for c in completed],
        "pre_holdout_survivors": survivors,
        "survivor_list_hash": survivor_hash,
        "holdout_opened": holdout_opened,
        "qualified_total": len(qualified),
        "qualified_cells": qualified,
        "calibration_passing_cells": sorted(calibration_passed),
        "f6b_preserved": context["f6b_preserved"],
        "abstention": "NOT TESTED",
        "expected_return_capable": False,
        "target_capable": False,
        "models_promoted": 0,
        "thresholds_changed": 0,
        "broker_writes": 0,
        "market_data_requests": 0,
        "errors": errors,
    }

    conn = sqlite3.connect(db_path)
    try:
        for cell_id in completed:
            record = results[cell_id]
            conn.execute("""
                INSERT OR REPLACE INTO research_candidate_results
                    (execution_id, cell_id, horizon_id, candidate, snapshot_id,
                     snapshot_hash, preregistration_hash, code_commit,
                     model_family, task, feature_config, survival_status,
                     holdout_verdict, qualified, failed_gates_json,
                     metrics_json, folds_json, notes_json, evaluated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                execution_id, cell_id, record["horizon"],
                f'{CANDIDATE}:{record["calibration_method"]}',
                context["snapshot_id"], context["snapshot_hash"],
                context["preregistration_hash"], context["code_commit"],
                record["model_family"], rmq.spec_for(CANDIDATE).task.value,
                record["feature_config"],
                "SURVIVES" if record["survived_pre_holdout"] else "NOT_QUALIFIED",
                json.dumps((record.get("holdout") or {}).get(
                    "passes_f6c_holdout")),
                1 if record["f6c_qualified"] else 0,
                json.dumps(record["failed_gates"]),
                json.dumps({"gates": record["gates"],
                            "stability": record["stability"]}, default=str),
                json.dumps(record["folds"], default=str),
                json.dumps(record["gate_reasons"]),
                completed_at.isoformat()))
        conn.execute("""
            UPDATE research_executions SET completed_at = ?, status = ?,
                   completed_cells_json = ?, qualified_count = ?
            WHERE execution_id = ?
        """, (completed_at.isoformat(), status, json.dumps(completed),
              len(qualified), execution_id))
        conn.commit()
    finally:
        conn.close()

    with open(RESULT_PATH, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, sort_keys=True, default=str)
    result["result_path"] = os.path.relpath(RESULT_PATH, ROOT)
    return result


def invalidate(db_path: str, *, reason: str) -> Dict[str, Any]:
    """
    Mark the recorded execution INVALIDATED, with the reason, so a rerun
    is legitimate.

    Used when a genuine implementation defect is found: the result of a
    broken run is not evidence, but deleting it would hide that the run
    happened. The marker is kept and annotated, the candidate rows are
    kept, and the rerun gets a new execution id -- so the record shows
    one invalidated attempt and one valid execution rather than a single
    tidy history.
    """
    if not reason.strip():
        raise FrozenPlanViolation("an invalidation must state its reason")
    conn = sqlite3.connect(db_path)
    try:
        initialize_execution_schema(conn)
        rows = conn.execute(
            "SELECT execution_id, status FROM research_executions "
            "WHERE phase = ? AND status != 'invalidated' "
            "ORDER BY started_at DESC", (PHASE,)).fetchall()
        if not rows:
            return {"invalidated": [], "reason": reason}
        for execution_id, _status in rows:
            conn.execute(
                "UPDATE research_executions SET status = 'invalidated', "
                "note = COALESCE(note, '') || ? WHERE execution_id = ?",
                (f"; INVALIDATED: {reason}", execution_id))
        conn.commit()
    finally:
        conn.close()
    return {"invalidated": [r[0] for r in rows], "reason": reason}


def show(db_path: str = TARGET_DB) -> Dict[str, Any]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='research_executions'").fetchone():
            return {"found": False, "reason": "no execution recorded"}
        existing = find_execution(
            conn, preregistration_hash=file_digest(PREREGISTRATION),
            snapshot_hash=read_handoff()["snapshot"]["content_hash"])
        if not existing:
            return {"found": False,
                    "reason": "no execution exists for this frozen plan"}
        rows = conn.execute("""
            SELECT cell_id, horizon_id, feature_config, survival_status,
                   qualified, failed_gates_json
            FROM research_candidate_results WHERE execution_id = ?
            ORDER BY cell_id""", (existing["execution_id"],)).fetchall()
    finally:
        conn.close()
    existing["found"] = True
    existing["candidates"] = [
        {"cell_id": r[0], "horizon": r[1], "feature_config": r[2],
         "survival_status": r[3], "qualified": bool(r[4]),
         "failed_gates": json.loads(r[5] or "[]")} for r in rows]
    return existing


# ======================================================================
# CLI
# ======================================================================

def _print_plan(plan: Dict[str, Any]) -> None:
    context = plan["context"]
    print(f"PHASE {PHASE} FROZEN PLAN (nothing fitted, nothing written)")
    print(f"  snapshot        : {context['snapshot_id']}")
    print(f"  snapshot hash   : {context['snapshot_hash']}")
    print(f"  prereg          : {context['preregistration_path']}")
    print(f"  prereg hash     : {context['preregistration_hash']}")
    print(f"  pinned hash     : {PREREGISTRATION_SHA256}")
    print(f"  holdout         : {context['holdout_boundary']} "
          f"-- {context['holdout_origin']}")
    print(f"  horizons        : {context['horizons']}")
    print(f"  deferred        : {context['deferred']}")
    print(f"  feature configs : {context['feature_configs']}")
    print(f"  calibration     : {context['calibration_methods']} "
          f"(raw = control arm)")
    print(f"  purge / embargo : {context['purge_days']} / "
          f"{context['embargo_days']}")
    print(f"  windows         : calib {context['calibration_days']}d, "
          f"test {context['test_days']}d, step {context['step_days']}d")
    print(f"  no-skill points : {context['no_skill']}")
    print(f"  gate            : {context['qualification_gate']}")
    print(f"  F.6B preserved  : {context['f6b_preserved']}")
    print(f"  prior execution : "
          f"{(context['existing_execution'] or {}).get('status', 'none')}")
    print()
    print(f"  TOTAL ALLOWED RESEARCH CELLS = {plan['total_cells']}  "
          f"hash={plan['cell_list_hash'][:16]}...")
    for cell in plan["cells"]:
        print(f"    {cell['cell_id']:<30} {cell['model_family']:<21}"
              f"{cell['task']:<17} purge={cell['purge_days']:.0f}")
    print()
    print("  HORIZON ELIGIBILITY AND FOLD GEOMETRY")
    for item in plan["per_horizon"]:
        print(f"    {item['horizon']}: rows={item['rows']} "
              f"clusters={item['clusters']} region={item['research_region_rows']} "
              f"holdout={item['holdout_rows']} "
              f"folds={item['folds_valid']}/{item['folds_built']} "
              f"holdout_valid={item['holdout_valid']}")
        for f in item["fold_detail"]:
            print(f"      {f['label']}: train={f['train']:>4} "
                  f"calib={f['calibration']:>4} test={f['test']:>4} "
                  f"pos_test={f['positive_rate_test']} "
                  f"pos_calib={f['positive_rate_calibration']} "
                  f"valid={f['valid']}")
        print(f"      configs collapse in "
              f"{item['folds_where_configs_collapse']}/{item['folds_valid']} "
              f"valid folds")
    print()
    print(f"  COMPUTE  model fits={plan['expected_model_fits']} "
          f"calibrator fits={plan['expected_calibrator_fits']} "
          f"holdout model fits (max)={plan['expected_holdout_model_fits_max']}")
    print(f"  abstention: {plan['abstention']}")
    print(f"  market-data requests={plan['market_data_requests']} "
          f"paid API calls={plan['paid_api_calls']}")


def _print_result(result: Dict[str, Any]) -> None:
    print()
    print("RESULT")
    print(f"  execution : {result['execution_id']}  "
          f"status={result['status'].upper()}")
    print(f"  runtime   : {result['runtime_seconds']}s")
    print(f"  cells     : {len(result['cells_evaluated'])} evaluated, "
          f"{len(result['cells_unevaluated'])} unevaluated")
    for error in result["errors"]:
        print(f"  ERROR     : {error}")
    print()
    print("  CANDIDATE TABLE")
    print(f"    {'cell':<30}{'AUC':>8}{'bal.acc':>9}{'brier':>9}{'BSS':>9}"
          f"  {'gates failed':<30} qualified")
    for record in result["candidates"]:
        st = record["stability"]
        print(f"    {record['cell_id']:<30}"
              f"{str(st['roc_auc']['mean']):>8}"
              f"{str(st['balanced_accuracy']['mean']):>9}"
              f"{str(st['brier']['mean']):>9}"
              f"{str(st['brier_skill_score']['mean']):>9}"
              f"  {','.join(record['failed_gates'])[:29]:<30} "
              f"{record['f6c_qualified']}")
    print()
    print(f"  pre-holdout survivors = {len(result['pre_holdout_survivors'])} "
          f"{result['pre_holdout_survivors']}")
    print(f"  survivor list hash    = {result['survivor_list_hash'][:16]}...")
    print(f"  HOLDOUT OPENED        = {result['holdout_opened']}")
    print(f"  calibration passing   = {len(result['calibration_passing_cells'])} "
          f"{result['calibration_passing_cells']}")
    print(f"  QUALIFIED DIRECTIONAL RESEARCH CANDIDATES = "
          f"{result['qualified_total']}")
    print(f"  expected_return_capable={result['expected_return_capable']}  "
          f"target_capable={result['target_capable']}  "
          f"abstention={result['abstention']}")
    print(f"  models promoted={result['models_promoted']}  "
          f"thresholds changed={result['thresholds_changed']}  "
          f"broker writes={result['broker_writes']}")
    if result.get("result_path"):
        print(f"  written to {result['result_path']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=TARGET_DB)
    parser.add_argument("--plan", action="store_true",
                        help="Verify the frozen plan and report the budget. "
                             "Writes nothing. The default.")
    parser.add_argument("--run", action="store_true",
                        help="Execute the frozen experiment once.")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--invalidate", metavar="REASON", default=None,
                        help="Mark the recorded execution INVALIDATED with "
                             "this reason, so a defect fix can be rerun. The "
                             "marker and its rows are kept, not deleted.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        if args.invalidate:
            report = invalidate(args.db, reason=args.invalidate)
            print(json.dumps(report, indent=2))
            return 0
        if args.show:
            print(json.dumps(show(args.db), indent=2, default=str))
            return 0
        if args.run:
            result = run(args.db, verbose=not args.json)
            if args.json:
                print(json.dumps(result, indent=2, default=str))
            else:
                _print_result(result)
            return 0 if result["status"] == "complete" else 3
        plan = build_plan(args.db)
        if args.json:
            print(json.dumps(plan, indent=2, default=str))
        else:
            _print_plan(plan)
        return 0
    except FrozenPlanViolation as violation:
        print(f"STOP: {violation}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
