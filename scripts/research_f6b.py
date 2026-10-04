#!/usr/bin/env python
"""
scripts/research_f6b.py
--------------------------------------------------
Phase 25.9I-F.6B frozen multi-horizon model research.

    python scripts/research_f6b.py --plan
    python scripts/research_f6b.py --run
    python scripts/research_f6b.py --show

> THE EXPERIMENT IS DEFINED BEFORE THE RESULT IS KNOWN.
>
> NO MODEL QUALIFYING IS A VALID RESULT.

THE PLAN IS NOT IN THIS FILE
----------------------------
Everything that could be chosen to suit a result is read from the
frozen Phase 25.9I-F.7 handoff, `docs/PHASE_25_9I_F6B_HANDOFF.json`:
the snapshot and its hash, the holdout boundary, the eligible horizons,
the purge and embargo policy, the candidate budget and the
qualification gate. This module contributes the sequence, not the
policy. If the handoff and the artifacts it names disagree in any way,
`preflight()` refuses to run.

TWO STAGES, AS PRE-REGISTERED
-----------------------------
Pre-registration section 7 defines a two-stage contract, and surviving
the first is not qualification:

  1. RESEARCH REGION. Expanding walk-forward folds strictly before the
     holdout. A candidate survives only if it beats every mandatory
     baseline in a STRICT MAJORITY of folds, its pooled effective
     sample clears 30, and -- for probabilistic candidates -- its Brier
     beats the base rate in a majority of folds. That is
     `research_model_quality.verdict`, reused.

  2. HOLDOUT. Survivors only, ONE evaluation, ONCE. Fit on the whole
     research region, evaluate on the held-out region, and take the
     canonical `ModelEvaluation.is_deployable` as the verdict. A
     candidate that never survived stage 1 is never shown the holdout.

REUSED, NOT REBUILT
-------------------
  dataset        `scripts/train_models.load_dataset`
  folds          `scripts/research_multi_horizon.build_folds`
  candidates     `scripts/research_model_quality.CANDIDATES` / `spec_for`
  fold scoring   `research_model_quality.evaluate_fold`
  survival rule  `research_model_quality.verdict`
  fit + gate     `ModelingEngine.train_and_evaluate`, whose mandatory
                 baselines, primary metric and deployability rule are
                 untouched

No second qualification gate, no second registry, no second research
stack. Nothing here defines a threshold of its own.

RUN ONCE
--------
A COMPLETE execution marker for the same (pre-registration hash,
snapshot hash) pair blocks a second run. Re-running a frozen experiment
and keeping the better outcome is the failure this phase exists to
prevent, so `--run` refuses and `--show` prints what was already found.

NO PROMOTION, NO BROKER, NO PROTECTED HORIZON
---------------------------------------------
This module promotes nothing, writes no order, opens no IBKR
connection and makes no billable market-data request. The protected
horizon is refused before any query: it is not in the handoff's
eligible list, and `research_horizon()` raises if asked for it.
"""

from __future__ import annotations

import argparse
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
from scripts.research_multi_horizon import build_folds               # noqa: E402
from scripts.train_models import load_dataset                        # noqa: E402
from src.adaptive import research_snapshot as rs                     # noqa: E402
from src.adaptive.corpus_versioning import (                         # noqa: E402
    current_commit, file_digest, initialize_recovery_schema,
    load_snapshot, verify_snapshot_hash,
)
from src.modeling.engine import (                                    # noqa: E402
    ModelingEngine, primary_metric_name,
)
from src.domain.model_models import (                                # noqa: E402
    ModelFamily, TrainingWindow,
)

UTC = timezone.utc

TARGET_DB = os.path.join(ROOT, "data", "marketlens_corpus_v2.db")
HANDOFF_PATH = os.path.join(ROOT, "docs", "PHASE_25_9I_F6B_HANDOFF.json")
RESULT_PATH = os.path.join(ROOT, "docs", "PHASE_25_9I_F6B_RESULTS.json")

#: Survival status produced by the reused Phase 25.9 contract.
SURVIVES = "SURVIVES RESEARCH REGION"


class FrozenPlanViolation(RuntimeError):
    """
    The frozen plan and the artifacts disagree, or the experiment has
    already been run. Either way the honest move is to stop rather than
    produce a result that cannot be trusted.
    """


class ProtectedHorizonRequested(RuntimeError):
    """Someone pointed the researcher at a sealed horizon."""


# ======================================================================
# Execution marker
# ======================================================================

def initialize_execution_schema(conn: sqlite3.Connection) -> None:
    """
    Durable record of which frozen experiment was run against which
    snapshot, and of every candidate result including the failures.

    Additive only. Failed candidates are data (brief section 31): a
    future autonomous phase has to be able to see that a specification
    has already lost on this snapshot, so it does not spend a budget
    rediscovering it.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS research_executions (
            execution_id           TEXT PRIMARY KEY,
            phase                  TEXT NOT NULL,
            preregistration_path   TEXT NOT NULL,
            preregistration_hash   TEXT NOT NULL,
            snapshot_id            TEXT NOT NULL,
            snapshot_hash          TEXT NOT NULL,
            corpus_version_id      TEXT NOT NULL,
            holdout_boundary       TEXT NOT NULL,
            code_commit            TEXT NOT NULL,
            started_at             TEXT NOT NULL,
            completed_at           TEXT,
            status                 TEXT NOT NULL,
            cells_json             TEXT NOT NULL DEFAULT '[]',
            completed_cells_json   TEXT NOT NULL DEFAULT '[]',
            qualified_count        INTEGER,
            note                   TEXT
        );

        CREATE TABLE IF NOT EXISTS research_candidate_results (
            execution_id      TEXT NOT NULL,
            cell_id           TEXT NOT NULL,
            horizon_id        TEXT NOT NULL,
            candidate         TEXT NOT NULL,
            snapshot_id       TEXT NOT NULL,
            snapshot_hash     TEXT NOT NULL,
            preregistration_hash TEXT NOT NULL,
            code_commit       TEXT NOT NULL,
            model_family      TEXT,
            task              TEXT,
            feature_config    TEXT,
            survival_status   TEXT,
            holdout_verdict   TEXT,
            qualified         INTEGER,
            failed_gates_json TEXT NOT NULL DEFAULT '[]',
            metrics_json      TEXT NOT NULL DEFAULT '{}',
            folds_json        TEXT NOT NULL DEFAULT '[]',
            notes_json        TEXT NOT NULL DEFAULT '[]',
            evaluated_at      TEXT NOT NULL,
            PRIMARY KEY (execution_id, cell_id)
        );

        CREATE INDEX IF NOT EXISTS idx_candidate_results_horizon
            ON research_candidate_results(horizon_id);
        CREATE INDEX IF NOT EXISTS idx_candidate_results_qualified
            ON research_candidate_results(qualified);
    """)


def find_execution(conn: sqlite3.Connection, *, preregistration_hash: str,
                   snapshot_hash: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("""
        SELECT execution_id, status, started_at, completed_at,
               qualified_count, cells_json, completed_cells_json, note
        FROM research_executions
        WHERE preregistration_hash = ? AND snapshot_hash = ?
        ORDER BY started_at DESC LIMIT 1
    """, (preregistration_hash, snapshot_hash)).fetchone()
    if not row:
        return None
    return {
        "execution_id": row[0], "status": row[1], "started_at": row[2],
        "completed_at": row[3], "qualified_count": row[4],
        "cells": json.loads(row[5] or "[]"),
        "completed_cells": json.loads(row[6] or "[]"),
        "note": row[7] or "",
    }


# ======================================================================
# Pre-flight
# ======================================================================

def read_handoff(path: str = HANDOFF_PATH) -> Dict[str, Any]:
    if not os.path.exists(path):
        raise FrozenPlanViolation(f"the frozen handoff is missing: {path}")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def preflight(db_path: str = TARGET_DB,
              handoff_path: str = HANDOFF_PATH) -> Dict[str, Any]:
    """
    Verify the frozen plan against the artifacts it names.

    Raises `FrozenPlanViolation` on any disagreement. A blocked result
    is preferable to an invalid scientific result, so none of these
    checks has a warning-and-continue path.
    """
    handoff = read_handoff(handoff_path)
    problems: List[str] = []

    prereg_path = os.path.join(ROOT, handoff["preregistration_path"])
    if not os.path.exists(prereg_path):
        raise FrozenPlanViolation(
            f"the pre-registration is missing: {prereg_path}")
    prereg_hash = file_digest(prereg_path)
    if prereg_hash != handoff["preregistration_hash"]:
        problems.append(
            f"pre-registration hash changed: handoff records "
            f"{handoff['preregistration_hash'][:16]}..., file is "
            f"{prereg_hash[:16]}...")

    if not os.path.exists(db_path):
        raise FrozenPlanViolation(f"the research snapshot is missing: {db_path}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        snapshot = load_snapshot(conn, handoff["snapshot"]["snapshot_id"])
        if snapshot is None:
            raise FrozenPlanViolation(
                f"{handoff['snapshot']['snapshot_id']} is not registered in "
                f"{db_path}")
        if snapshot.content_hash != handoff["snapshot"]["content_hash"]:
            problems.append("snapshot hash in the registry differs from the "
                            "handoff")
        if not verify_snapshot_hash(conn, snapshot):
            problems.append("snapshot content no longer hashes to its frozen "
                            "value -- the dataset has changed")
        if not snapshot.state.is_research_input:
            problems.append(f"snapshot state is {snapshot.state.value}, not "
                            f"eligible_for_research")

        # The holdout must still be where it was frozen, derived by the
        # same rule from the same data.
        derived = rs.deterministic_holdout(
            conn, snapshot_id=snapshot.snapshot_id)
        frozen_boundary = handoff["holdout"]["boundary"]
        if derived.boundary.isoformat() != frozen_boundary:
            problems.append(
                f"the holdout rule no longer yields the frozen boundary: "
                f"frozen {frozen_boundary}, derived "
                f"{derived.boundary.isoformat()}")
        if handoff["holdout"]["observed_model_outcome"]:
            problems.append("the frozen holdout records that an outcome was "
                            "observed before it was chosen")

        stale = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("trained_models", "model_evaluations",
                          "model_baseline_comparisons", "predictions")}

        existing = None
        if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='research_executions'").fetchone():
            existing = find_execution(
                conn, preregistration_hash=handoff["preregistration_hash"],
                snapshot_hash=handoff["snapshot"]["content_hash"])
    finally:
        conn.close()

    if not handoff.get("d20_excluded"):
        problems.append("the handoff does not exclude the protected horizon")
    for horizon in handoff["eligible_horizons"]:
        if rs.is_protected(horizon):
            problems.append(f"{horizon} is protected and cannot be eligible")
        if horizon not in handoff["purge_days"]:
            problems.append(f"no purge horizon declared for {horizon}")

    if problems:
        raise FrozenPlanViolation("; ".join(problems))

    return {
        "handoff_path": os.path.relpath(handoff_path, ROOT),
        "handoff": handoff,
        "preregistration_path": handoff["preregistration_path"],
        "preregistration_hash": prereg_hash,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_hash": snapshot.content_hash,
        "snapshot_state": snapshot.state.value,
        "corpus_version_id": snapshot.corpus_version_id,
        "dataset_version": snapshot.dataset_version.fingerprint(),
        "universe_version": snapshot.universe_version,
        "event_version": snapshot.event_version,
        "as_of_cutoff": snapshot.as_of_cutoff.isoformat(),
        "holdout_boundary": handoff["holdout"]["boundary"],
        "holdout_rule": handoff["holdout"]["rule"],
        "holdout_derived_matches_frozen": True,
        "eligible_horizons": list(handoff["eligible_horizons"]),
        "deferred_horizons": list(
            handoff.get("deferred_on_policy", {}).get("horizons", [])),
        "purge_days": dict(handoff["purge_days"]),
        "embargo_days": handoff["embargo_days"],
        "candidate_budget": handoff["candidate_budget"],
        "qualification_gate": handoff["qualification_gate_reference"],
        "effective_sample": dict(handoff["effective_sample"]),
        "code_commit": current_commit(ROOT),
        "stale_experiment_rows": stale,
        "existing_execution": existing,
        "d20_excluded": True,
    }


# ======================================================================
# The frozen cell list
# ======================================================================

def enumerate_cells(context: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Every research cell, in a fixed order, derived from the frozen plan.

    Listed before anything is evaluated and never appended to. The
    budget is a number in the handoff, and a mismatch is a stop
    condition rather than something to reconcile.
    """
    cells: List[Dict[str, Any]] = []
    for horizon in context["eligible_horizons"]:
        for candidate in rmq.CANDIDATES:
            spec = rmq.spec_for(candidate)
            cells.append({
                "cell_id": f"{horizon}:{candidate}",
                "horizon_id": horizon,
                "candidate": candidate,
                "model_family": spec.family.value,
                "task": spec.task.value,
                "feature_config": ("dense_only" if candidate == "C3"
                                   else "all_numeric"),
                "hyperparameters": dict(spec.hyperparameters),
                "purge_days": context["purge_days"][horizon],
                "embargo_days": context["embargo_days"],
            })

    if len(cells) != context["candidate_budget"]:
        raise FrozenPlanViolation(
            f"the enumerated cell list holds {len(cells)} cells but the frozen "
            f"budget is {context['candidate_budget']}")
    return cells


# ======================================================================
# Stage 2 -- the single holdout evaluation
# ======================================================================

def holdout_evaluation(candidate: str, X, Y, clusters, feature_names,
                       cutoffs: Sequence[datetime], *,
                       holdout_start: datetime, purge_days: float,
                       embargo_days: float) -> Dict[str, Any]:
    """
    One evaluation, once, for a candidate that survived stage 1.

    Train on the research region with the same purge and embargo applied
    at the boundary; test on everything at or after it. The verdict is
    the canonical `ModelEvaluation.is_deployable` -- True, False, or
    None when it cannot be judged, and None is not False.
    """
    from src.modeling import splits

    research = [i for i, c in enumerate(cutoffs) if c < holdout_start]
    test_idx = [i for i, c in enumerate(cutoffs) if c >= holdout_start]

    kept, purged = splits.purge(research, holdout_start,
                                lambda i: cutoffs[i], purge_days)
    kept, embargoed = splits.embargo(kept, holdout_start,
                                     lambda i: cutoffs[i], embargo_days)

    if not kept or not test_idx:
        return {"candidate": candidate, "evaluated": False,
                "reason": "no usable train or test rows at the holdout boundary",
                "is_deployable": None}

    columns = list(range(len(feature_names)))
    if candidate == "C3":
        columns = rmq.dense_columns(X, kept)

    X_train = rmq.select([X[i] for i in kept], columns)
    Y_train = [Y[i] for i in kept]
    X_test = rmq.select([X[i] for i in test_idx], columns)
    Y_test = [Y[i] for i in test_idx]
    names = [feature_names[c] for c in columns]
    test_clusters = len({clusters[i] for i in test_idx})

    spec = rmq.spec_for(candidate)
    window = TrainingWindow(
        label="holdout",
        train_start=min(cutoffs[i] for i in kept),
        train_end=max(cutoffs[i] for i in kept),
        test_start=holdout_start,
        test_end=max(cutoffs[i] for i in test_idx),
        train_size=len(kept), test_size=len(test_idx),
        purged_count=len(purged), embargoed_count=len(embargoed))

    engine = ModelingEngine()
    model, evaluation = engine.train_and_evaluate(
        spec, X_train, Y_train, X_test, Y_test,
        feature_names=names, window=window, cluster_count=test_clusters)

    metric = primary_metric_name(spec.task)
    record = {
        "candidate": candidate,
        "evaluated": True,
        "train_rows": len(kept), "test_rows": len(test_idx),
        "purged": len(purged), "embargoed": len(embargoed),
        "test_clusters": test_clusters,
        "features": len(columns),
        "metric": metric,
        "metrics": dict(evaluation.metrics),
        "model_score": evaluation.metrics.get(metric),
        "baselines": {c.baseline_name: {
            "metric": c.metric_name, "baseline_score": c.baseline_score,
            "model_score": c.model_score, "beats": c.beats_baseline}
            for c in evaluation.baseline_comparisons},
        "beats_all_baselines": evaluation.beats_all_baselines,
        "effective_sample_size": evaluation.effective_sample_size,
        "small_sample": evaluation.small_sample,
        "abstention_rate": evaluation.abstention_rate,
        "is_deployable": evaluation.is_deployable,
        "honest_summary": evaluation.honest_summary(),
    }

    if spec.family is ModelFamily.LOGISTIC_REGRESSION:
        from src.modeling import algorithms
        probs = algorithms.predict_batch(model.parameters, X_test)
        positives = [y for y in Y_train if y is not None]
        base_rate = (sum(1 for y in positives if y > 0) / len(positives)
                     if positives else 0.0)
        record["brier"] = rmq.brier(probs, Y_test)
        record["brier_base_rate"] = rmq.brier([base_rate] * len(Y_test), Y_test)
    return record


def failed_gates(survival: Dict[str, Any],
                 holdout: Optional[Dict[str, Any]]) -> List[str]:
    """
    Which canonical gates this candidate failed, in repository terms.

    `FAILED_OTHER_CANONICAL_GATE` is deliberately possible: a gate that
    answers None has not been passed, and collapsing that into a pass
    would be the softest possible lie.
    """
    gates: List[str] = []
    status = survival.get("status")
    if status == "INSUFFICIENT DATA":
        gates.append("FAILED_SAMPLE")
        return gates

    reasons = " ".join(survival.get("reasons", []))
    if "beats baselines in" in reasons:
        gates.append("FAILED_BASELINE")
    if "pooled effective sample" in reasons:
        gates.append("FAILED_SAMPLE")
    if "Brier not below base rate" in reasons:
        gates.append("FAILED_CALIBRATION")
    if status != SURVIVES and not gates:
        gates.append("FAILED_OTHER_CANONICAL_GATE")

    if holdout is None:
        return gates
    if not holdout.get("evaluated"):
        gates.append("FAILED_OTHER_CANONICAL_GATE")
        return gates
    if holdout.get("is_deployable") is False:
        if holdout.get("small_sample"):
            gates.append("FAILED_SAMPLE")
        if holdout.get("beats_all_baselines") is False:
            gates.append("FAILED_BASELINE")
        if not gates:
            gates.append("FAILED_OTHER_CANONICAL_GATE")
    elif holdout.get("is_deployable") is None:
        gates.append("FAILED_OTHER_CANONICAL_GATE")
    return gates


def fold_stability(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Dispersion across folds, so a candidate carried by one window is
    visibly fragile rather than hidden behind a mean.
    """
    scores = [r["model_score"] for r in results
              if r.get("model_score") is not None]
    wins = [r.get("beats_all_baselines") for r in results]
    if not scores:
        return {"folds": len(results), "scored_folds": 0,
                "mean": None, "worst": None, "best": None,
                "spread": None, "sign_consistent": None,
                "baseline_wins": sum(1 for w in wins if w is True)}
    mean = sum(scores) / len(scores)
    variance = sum((s - mean) ** 2 for s in scores) / len(scores)
    return {
        "folds": len(results),
        "scored_folds": len(scores),
        "mean": round(mean, 6),
        "worst": round(min(scores), 6),
        "best": round(max(scores), 6),
        "spread": round(max(scores) - min(scores), 6),
        "stdev": round(variance ** 0.5, 6),
        "sign_consistent": all(s >= 0 for s in scores) or all(s < 0 for s in scores),
        "baseline_wins": sum(1 for w in wins if w is True),
        "baseline_judged": sum(1 for w in wins if w is not None),
    }


# ======================================================================
# One horizon
# ======================================================================

def research_horizon(conn: sqlite3.Connection, horizon_id: str, *,
                     context: Dict[str, Any],
                     verbose: bool = True) -> Dict[str, Any]:
    """
    One horizon, the frozen candidates, the frozen two-stage contract.

    Returns a record even when nothing can be evaluated: an unexplained
    horizon is not an acceptable output.
    """
    if rs.is_protected(horizon_id):
        raise ProtectedHorizonRequested(
            f"{horizon_id} is sealed and cannot be researched")
    if horizon_id not in context["eligible_horizons"]:
        raise FrozenPlanViolation(
            f"{horizon_id} is not in the frozen eligible list")

    holdout_start = datetime.fromisoformat(context["holdout_boundary"])
    purge_days = float(context["purge_days"][horizon_id])
    embargo_days = float(context["embargo_days"])

    label = f"{horizon_id}.abnormal_return"
    names, X, Y, cutoffs, clusters, observation_ids, quality = load_dataset(
        conn, label)

    if not X:
        return {"horizon": horizon_id, "label": label,
                "verdict": "UNSUPPORTED",
                "reason": "no matured labels exist for this horizon",
                "candidates": []}

    rmq._CUTOFFS = list(cutoffs)

    folds = build_folds(cutoffs, purge_days, holdout_start=holdout_start,
                        embargo_days=embargo_days)
    valid = [f for f in folds if f["valid"]]

    research_rows = sum(1 for c in cutoffs if c < holdout_start)
    research_clusters = len({clusters[i] for i, c in enumerate(cutoffs)
                             if c < holdout_start})
    holdout_rows = sum(1 for c in cutoffs if c >= holdout_start)
    holdout_clusters = len({clusters[i] for i, c in enumerate(cutoffs)
                            if c >= holdout_start})

    header = {
        "horizon": horizon_id, "label": label,
        "snapshot_id": context["snapshot_id"],
        "snapshot_hash": context["snapshot_hash"],
        "corpus_version_id": context["corpus_version_id"],
        "feature_version": context["dataset_version"],
        "as_of_cutoff": context["as_of_cutoff"],
        "preregistration_hash": context["preregistration_hash"],
        "code_commit": context["code_commit"],
        "rows": len(X), "features": len(names),
        "clusters": len(set(clusters)),
        "instruments": len(set(clusters)),
        "sessions": len({c.date() for c in cutoffs}),
        "quality": dict(quality),
        "research_region_rows": research_rows,
        "research_region_clusters": research_clusters,
        "holdout_rows": holdout_rows,
        "holdout_clusters": holdout_clusters,
        "purge_days": purge_days,
        "embargo_days": embargo_days,
        "folds_built": len(folds), "folds_valid": len(valid),
        "fold_detail": [
            {"label": f["label"],
             "train_start": (min(cutoffs[i] for i in f["train_idx"]).isoformat()
                             if f["train_idx"] else None),
             "train_end": (max(cutoffs[i] for i in f["train_idx"]).isoformat()
                           if f["train_idx"] else None),
             "test_start": f["test_start"].isoformat(),
             "test_end": f["test_end"].isoformat(),
             "train_rows": len(f["train_idx"]),
             "test_rows": len(f["test_idx"]),
             "test_clusters": len({clusters[i] for i in f["test_idx"]}),
             "purged": f["purged"], "embargoed": f["embargoed"],
             "valid": f["valid"]}
            for f in folds],
    }

    if research_clusters < rs.MIN_EFFECTIVE_SAMPLE:
        return {**header, "verdict": "INSUFFICIENT DATA",
                "reason": (f"research-region effective sample "
                           f"{research_clusters} is below the floor of "
                           f"{rs.MIN_EFFECTIVE_SAMPLE}"),
                "candidates": []}

    if len(valid) < rs.MIN_VALID_FOLDS:
        return {**header, "verdict": "INSUFFICIENT DATA",
                "reason": (f"only {len(valid)} valid fold(s); the contract "
                           f"requires {rs.MIN_VALID_FOLDS}"),
                "candidates": []}

    if verbose:
        print(f"  {horizon_id}: {len(X)} rows, {header['clusters']} clusters, "
              f"{len(valid)} valid folds, holdout {holdout_rows} rows",
              flush=True)

    candidates: List[Dict[str, Any]] = []
    for candidate in rmq.CANDIDATES:
        started = time.perf_counter()
        fold_results: List[Dict[str, Any]] = []
        errors: List[str] = []
        for fold in valid:
            try:
                fold_results.append(rmq.evaluate_fold(
                    candidate, fold, X, Y, clusters, names))
            except Exception as error:                        # noqa: BLE001
                message = f"{type(error).__name__}: {error}"
                errors.append(f"{fold['label']}: {message}")
                fold_results.append({"candidate": candidate,
                                     "fold": fold["label"],
                                     "beats_all_baselines": None,
                                     "error": message})

        survival = rmq.verdict(candidate, fold_results, len(valid))
        stability = fold_stability(fold_results)

        # The frozen calibration check is Brier against the base rate,
        # which `verdict` already applied on research-region folds.
        #
        # Expected calibration error is NOT computed. It needs the
        # per-row predicted probabilities, and the reused
        # `rmq.evaluate_fold` does not return them. Retrofitting that
        # retention after seeing the results would be adding a
        # diagnostic because the primary result disappointed, which the
        # brief forbids. So it is absent and says so, rather than being
        # reported as an empty field that looks computed.
        calibration: Dict[str, Any] = {
            "preregistered_check": "brier vs base rate (research folds)",
            "expected_calibration_error": "NOT_COMPUTED",
            "status": "NOT_APPLICABLE",
        }
        if any("brier" in r for r in fold_results):
            worse = [r["fold"] for r in fold_results
                     if r.get("brier") is not None
                     and r.get("brier_base_rate") is not None
                     and r["brier"] >= r["brier_base_rate"]]
            calibration["status"] = ("FAILED" if len(worse) * 2 >= len(fold_results)
                                     else "PASSED")
            calibration["folds_worse_than_base_rate"] = len(worse)
            calibration["folds"] = len(fold_results)

        holdout: Optional[Dict[str, Any]] = None
        if survival.get("status") == SURVIVES:
            holdout = holdout_evaluation(
                candidate, X, Y, clusters, names, cutoffs,
                holdout_start=holdout_start, purge_days=purge_days,
                embargo_days=embargo_days)

        qualified = bool(holdout and holdout.get("is_deployable") is True)
        gates = failed_gates(survival, holdout)

        record = {
            "cell_id": f"{horizon_id}:{candidate}",
            "candidate": candidate,
            "horizon": horizon_id,
            "model_family": rmq.spec_for(candidate).family.value,
            "task": rmq.spec_for(candidate).task.value,
            "feature_config": "dense_only" if candidate == "C3" else "all_numeric",
            "survival_status": survival.get("status"),
            "survival_reasons": survival.get("reasons", []),
            "fold_wins": survival.get("fold_wins"),
            "folds_judged": survival.get("folds_judged"),
            "pooled_effective_sample": survival.get("pooled_effective_sample"),
            "stability": stability,
            "calibration": calibration,
            "uncertainty": {
                "status": "UNAVAILABLE",
                "reason": "no prediction-interval machinery is in the frozen "
                          "plan; fold dispersion is reported under stability "
                          "and is not an interval",
                "fold_spread": stability.get("spread"),
            },
            "holdout": holdout,
            "qualified": qualified,
            "verdict": ("QUALIFIED_RESEARCH_CANDIDATE" if qualified
                        else (gates[0] if gates else "NOT_QUALIFIED")),
            "failed_gates": gates,
            "errors": errors,
            "runtime_seconds": round(time.perf_counter() - started, 3),
            "folds": fold_results,
        }
        candidates.append(record)

        if verbose:
            tail = "; ".join(survival.get("reasons", []))[:88]
            print(f"    {candidate:<3} {record['verdict']:<32} {tail}",
                  flush=True)

    qualified = [c for c in candidates if c["qualified"]]
    survivors = [c for c in candidates
                 if c["survival_status"] == SURVIVES]
    return {**header,
            "verdict": ("QUALIFIED" if qualified else "NONE QUALIFIED"),
            "survivor_candidates": [c["candidate"] for c in survivors],
            "qualified_candidates": [c["candidate"] for c in qualified],
            "candidates": candidates}


# ======================================================================
# Plan and run
# ======================================================================

def build_plan(db_path: str = TARGET_DB) -> Dict[str, Any]:
    """The compute budget, measured, before anything is fitted."""
    context = preflight(db_path)
    cells = enumerate_cells(context)
    holdout_start = datetime.fromisoformat(context["holdout_boundary"])

    per_horizon: List[Dict[str, Any]] = []
    total_fits = 0
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for horizon in context["eligible_horizons"]:
            names, X, Y, cutoffs, clusters, _, quality = load_dataset(
                conn, f"{horizon}.abnormal_return")
            folds = build_folds(
                cutoffs, float(context["purge_days"][horizon]),
                holdout_start=holdout_start,
                embargo_days=float(context["embargo_days"]))
            valid = [f for f in folds if f["valid"]]
            research_clusters = len({clusters[i] for i, c in enumerate(cutoffs)
                                     if c < holdout_start})
            fits = len(valid) * len(rmq.CANDIDATES)
            total_fits += fits
            per_horizon.append({
                "horizon": horizon,
                "rows": len(X),
                "features": len(names),
                "clusters": len(set(clusters)),
                "sessions": len({c.date() for c in cutoffs}),
                "research_region_rows": sum(1 for c in cutoffs
                                            if c < holdout_start),
                "research_region_clusters": research_clusters,
                "holdout_rows": sum(1 for c in cutoffs if c >= holdout_start),
                "purge_days": context["purge_days"][horizon],
                "folds_built": len(folds),
                "folds_valid": len(valid),
                "candidates": len(rmq.CANDIDATES),
                "research_fits": fits,
                "quality": dict(quality),
            })
    finally:
        conn.close()

    return {
        "context": context,
        "cells": cells,
        "total_cells": len(cells),
        "budget": context["candidate_budget"],
        "per_horizon": per_horizon,
        "expected_research_fits": total_fits,
        "expected_holdout_fits_max": len(cells),
        "expected_total_fits_max": total_fits + len(cells),
        "storage_note": "results are written to research_executions and "
                        "research_candidate_results in the snapshot database, "
                        "plus one JSON artifact; no model object is persisted",
        "market_data_requests": 0,
        "paid_api_calls": 0,
    }


def run(db_path: str = TARGET_DB, *, verbose: bool = True) -> Dict[str, Any]:
    """
    Execute the frozen experiment once.

    Refuses when a COMPLETE execution already exists for this
    (pre-registration, snapshot) pair. On an unexpected failure the
    marker is persisted as FAILED with the cells that did complete, so a
    partial run can never read as COMPLETE.
    """
    plan = build_plan(db_path)
    context = plan["context"]
    cells = plan["cells"]

    existing = context.get("existing_execution")
    if existing and existing["status"] == "complete":
        raise FrozenPlanViolation(
            f"this frozen experiment has already been executed as "
            f"{existing['execution_id']} ({existing['completed_at']}). "
            f"Re-running it would not be independent evidence -- use --show.")

    execution_id = f"f6b-{uuid.uuid4().hex[:16]}"
    started_at = datetime.now(UTC)
    wall = time.time()

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
        """, (execution_id, "25.9I-F.6B", context["preregistration_path"],
              context["preregistration_hash"], context["snapshot_id"],
              context["snapshot_hash"], context["corpus_version_id"],
              context["holdout_boundary"], context["code_commit"],
              started_at.isoformat(), "running",
              json.dumps([c["cell_id"] for c in cells]), "[]", ""))
        conn.commit()
    finally:
        conn.close()

    if verbose:
        print(f"PHASE 25.9I-F.6B EXECUTION {execution_id}")
        print(f"  snapshot   : {context['snapshot_id']} "
              f"({context['snapshot_hash'][:16]}...)")
        print(f"  prereg     : {context['preregistration_hash'][:16]}...")
        print(f"  holdout    : {context['holdout_boundary']} -- survivors only")
        print(f"  cells      : {len(cells)} (budget {context['candidate_budget']})")
        print(f"  commit     : {context['code_commit'][:12]}")
        print()

    horizons: List[Dict[str, Any]] = []
    completed: List[str] = []
    errors: List[str] = []
    status = "complete"

    read_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for horizon in context["eligible_horizons"]:
            try:
                record = research_horizon(read_conn, horizon,
                                          context=context, verbose=verbose)
            except Exception as error:                        # noqa: BLE001
                errors.append(f"{horizon}: {type(error).__name__}: {error}")
                status = "failed"
                break
            horizons.append(record)
            completed.extend(c["cell_id"] for c in record.get("candidates", []))
    finally:
        read_conn.close()

    if status != "failed" and len(completed) != len(cells):
        # A horizon that returned INSUFFICIENT DATA or UNSUPPORTED
        # evaluates no cell. That is a legitimate frozen outcome, not a
        # partial execution -- but it must be visible as such.
        unevaluated = [c["cell_id"] for c in cells if c["cell_id"] not in completed]
    else:
        unevaluated = []

    completed_at = datetime.now(UTC)
    qualified = [c for h in horizons for c in h.get("candidates", [])
                 if c.get("qualified")]

    result = {
        "execution_id": execution_id,
        "phase": "25.9I-F.6B",
        "status": status,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "runtime_seconds": round(time.time() - wall, 2),
        "frozen_plan": {
            "preregistration_path": context["preregistration_path"],
            "preregistration_hash": context["preregistration_hash"],
            "snapshot_id": context["snapshot_id"],
            "snapshot_hash": context["snapshot_hash"],
            "corpus_version_id": context["corpus_version_id"],
            "holdout_boundary": context["holdout_boundary"],
            "holdout_rule": context["holdout_rule"],
            "eligible_horizons": context["eligible_horizons"],
            "deferred_horizons": context["deferred_horizons"],
            "purge_days": context["purge_days"],
            "embargo_days": context["embargo_days"],
            "candidate_budget": context["candidate_budget"],
            "qualification_gate": context["qualification_gate"],
            "code_commit": context["code_commit"],
        },
        "cells": cells,
        "cells_evaluated": completed,
        "cells_unevaluated": unevaluated,
        "horizons": horizons,
        "qualified_total": len(qualified),
        "qualified_cells": [c["cell_id"] for c in qualified],
        "models_promoted": 0,
        "thresholds_changed": 0,
        "broker_writes": 0,
        "market_data_requests": 0,
        "errors": errors,
    }

    conn = sqlite3.connect(db_path)
    try:
        for horizon in horizons:
            for candidate in horizon.get("candidates", []):
                conn.execute("""
                    INSERT OR REPLACE INTO research_candidate_results
                        (execution_id, cell_id, horizon_id, candidate,
                         snapshot_id, snapshot_hash, preregistration_hash,
                         code_commit, model_family, task, feature_config,
                         survival_status, holdout_verdict, qualified,
                         failed_gates_json, metrics_json, folds_json,
                         notes_json, evaluated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    execution_id, candidate["cell_id"], horizon["horizon"],
                    candidate["candidate"], context["snapshot_id"],
                    context["snapshot_hash"], context["preregistration_hash"],
                    context["code_commit"], candidate["model_family"],
                    candidate["task"], candidate["feature_config"],
                    candidate["survival_status"],
                    (candidate["holdout"] or {}).get("is_deployable")
                    if candidate["holdout"] else None,
                    1 if candidate["qualified"] else 0,
                    json.dumps(candidate["failed_gates"]),
                    json.dumps({"stability": candidate["stability"],
                                "calibration": candidate["calibration"],
                                "holdout": candidate["holdout"]}, default=str),
                    json.dumps(candidate["folds"], default=str),
                    json.dumps(candidate["survival_reasons"]),
                    completed_at.isoformat()))
        conn.execute("""
            UPDATE research_executions
            SET completed_at = ?, status = ?, completed_cells_json = ?,
                qualified_count = ?, note = ?
            WHERE execution_id = ?
        """, (completed_at.isoformat(), status, json.dumps(completed),
              len(qualified), "; ".join(errors), execution_id))
        conn.commit()
    finally:
        conn.close()

    with open(RESULT_PATH, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, sort_keys=True, default=str)
    result["result_path"] = os.path.relpath(RESULT_PATH, ROOT)
    return result


def show(db_path: str = TARGET_DB) -> Dict[str, Any]:
    """Report an execution that already happened, without re-running it."""
    handoff = read_handoff()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='research_executions'").fetchone():
            return {"found": False,
                    "reason": "no execution has ever been recorded"}
        existing = find_execution(
            conn, preregistration_hash=handoff["preregistration_hash"],
            snapshot_hash=handoff["snapshot"]["content_hash"])
        if not existing:
            return {"found": False,
                    "reason": "no execution exists for this frozen plan"}
        rows = conn.execute("""
            SELECT cell_id, horizon_id, candidate, survival_status,
                   qualified, failed_gates_json
            FROM research_candidate_results WHERE execution_id = ?
            ORDER BY horizon_id, candidate
        """, (existing["execution_id"],)).fetchall()
    finally:
        conn.close()
    existing["found"] = True
    existing["candidates"] = [
        {"cell_id": r[0], "horizon": r[1], "candidate": r[2],
         "survival_status": r[3], "qualified": bool(r[4]),
         "failed_gates": json.loads(r[5] or "[]")} for r in rows]
    return existing


# ======================================================================
# CLI
# ======================================================================

def _print_plan(plan: Dict[str, Any]) -> None:
    context = plan["context"]
    print("PHASE 25.9I-F.6B FROZEN RESEARCH PLAN (nothing fitted, nothing written)")
    print(f"  snapshot        : {context['snapshot_id']}  "
          f"state={context['snapshot_state']}")
    print(f"  snapshot hash   : {context['snapshot_hash']}")
    print(f"  corpus version  : {context['corpus_version_id']}")
    print(f"  feature version : {context['dataset_version']}")
    print(f"  as-of cutoff    : {context['as_of_cutoff']}")
    print(f"  prereg          : {context['preregistration_path']}")
    print(f"  prereg hash     : {context['preregistration_hash']}")
    print(f"  holdout         : {context['holdout_boundary']} "
          f"(derived == frozen: {context['holdout_derived_matches_frozen']})")
    print(f"  eligible        : {context['eligible_horizons']}")
    print(f"  deferred        : {context['deferred_horizons']}")
    print(f"  purge / embargo : {context['purge_days']} / {context['embargo_days']}")
    print(f"  gate            : {context['qualification_gate']}")
    print(f"  stale rows      : {context['stale_experiment_rows']}")
    print(f"  prior execution : "
          f"{(context['existing_execution'] or {}).get('status', 'none')}")
    print()
    print(f"  TOTAL ALLOWED RESEARCH CELLS = {plan['total_cells']} "
          f"(frozen budget {plan['budget']})")
    print(f"    {'cell':<12} {'horizon':<8} {'family':<22} {'task':<18} "
          f"{'features':<12} purge")
    for cell in plan["cells"]:
        print(f"    {cell['cell_id']:<12} {cell['horizon_id']:<8} "
              f"{cell['model_family']:<22} {cell['task']:<18} "
              f"{cell['feature_config']:<12} {cell['purge_days']:.0f}")
    print()
    print("  DATASET AND FOLD DESIGN")
    print(f"    {'horizon':<8} {'rows':>6} {'clus':>5} {'sess':>5} "
          f"{'region':>7} {'rclus':>6} {'hold':>6} {'built':>6} {'valid':>6} "
          f"{'fits':>5}")
    for item in plan["per_horizon"]:
        print(f"    {item['horizon']:<8} {item['rows']:>6,} "
              f"{item['clusters']:>5,} {item['sessions']:>5} "
              f"{item['research_region_rows']:>7,} "
              f"{item['research_region_clusters']:>6,} "
              f"{item['holdout_rows']:>6,} {item['folds_built']:>6} "
              f"{item['folds_valid']:>6} {item['research_fits']:>5}")
    print()
    print(f"  COMPUTE BUDGET")
    print(f"    expected research fits      {plan['expected_research_fits']}")
    print(f"    expected holdout fits (max) {plan['expected_holdout_fits_max']}")
    print(f"    expected total fits (max)   {plan['expected_total_fits_max']}")
    print(f"    billable market-data calls  {plan['market_data_requests']}")
    print(f"    paid API calls              {plan['paid_api_calls']}")


def _print_result(result: Dict[str, Any]) -> None:
    print()
    print("RESULT")
    print(f"  execution    : {result['execution_id']}  "
          f"status={result['status'].upper()}")
    print(f"  runtime      : {result['runtime_seconds']}s")
    print(f"  cells        : {len(result['cells_evaluated'])} evaluated, "
          f"{len(result['cells_unevaluated'])} unevaluated")
    for error in result["errors"]:
        print(f"  ERROR        : {error}")
    print()
    print("  HORIZON RESULTS")
    for horizon in result["horizons"]:
        print(f"    {horizon['horizon']:<6} {horizon['verdict']:<18} "
              f"folds={horizon.get('folds_valid')} "
              f"survivors={horizon.get('survivor_candidates', [])} "
              f"qualified={horizon.get('qualified_candidates', [])}")
        if horizon.get("reason"):
            print(f"           {horizon['reason']}")
    print()
    print("  CANDIDATE TABLE")
    print(f"    {'cell':<12} {'family':<20} {'survival':<26} "
          f"{'gates':<34} qualified")
    for horizon in result["horizons"]:
        for candidate in horizon.get("candidates", []):
            print(f"    {candidate['cell_id']:<12} "
                  f"{candidate['model_family']:<20} "
                  f"{str(candidate['survival_status']):<26} "
                  f"{','.join(candidate['failed_gates'])[:33]:<34} "
                  f"{candidate['qualified']}")
    print()
    print(f"  QUALIFIED RESEARCH CANDIDATES = {result['qualified_total']}")
    print(f"  models promoted = {result['models_promoted']}   "
          f"thresholds changed = {result['thresholds_changed']}   "
          f"broker writes = {result['broker_writes']}")
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
    parser.add_argument("--show", action="store_true",
                        help="Report an execution that already happened.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        if args.show:
            report = show(args.db)
            print(json.dumps(report, indent=2, default=str) if args.json
                  else json.dumps(report, indent=2, default=str))
            return 0 if report.get("found") else 1

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
