#!/usr/bin/env python
"""
scripts/research_f6d.py
--------------------------------------------------
Phase 25.9I-F.6D feature information and predictive content audit.

    python scripts/research_f6d.py --plan
    python scripts/research_f6d.py --run
    python scripts/research_f6d.py --show

> A MORE COMPLEX MODEL CANNOT CREATE INFORMATION THAT IS NOT IN THE FEATURES.
>
> FEATURE DISCOVERY IS NOT MODEL QUALIFICATION.

THE PLAN IS PINNED
------------------
`PREREGISTRATION_SHA256` is the hash of
`docs/PHASE_25_9I_F6D_PREREGISTRATION.md` as committed at 2e3cc2d, before
this file existed. `preflight()` refuses to run if the document no longer
hashes to it, so editing the plan after seeing a result breaks the
harness rather than quietly changing the experiment.

WHAT THIS MEASURES, AND WHAT IT REFUSES TO
------------------------------------------
Per feature per horizon: the rank relationship inside each disjoint
evaluation window, its stability across windows, a block-aware
permutation null, Benjamini-Hochberg correction over a family declared
in advance, and a concentration check against the largest-contributing
instrument.

It fits no model family, tunes nothing and qualifies nothing. The group
and interaction probes are fixed low-capacity ridge probes whose only
job is to ask whether combination information appears to exist; a good
probe score authorises a FUTURE hypothesis and never a trading model.

D20
---
`market.return_60d` is one of the 26 legitimate features and IS audited
against d1, d3 and d5. What is protected is the frozen hypothesis
relating it to the **d20** abnormal return, so d20 is not an eligible
horizon and that pair is never computed. `guard_no_protected_horizon`
enforces it.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.research_f6b import (                                   # noqa: E402
    find_execution, initialize_execution_schema,
)
from scripts.research_multi_horizon import build_folds               # noqa: E402
from scripts.train_models import load_dataset                        # noqa: E402
from src.adaptive import research_snapshot as rs                     # noqa: E402
from src.adaptive.corpus_versioning import (                         # noqa: E402
    current_commit, file_digest, initialize_recovery_schema,
    load_snapshot, verify_snapshot_hash,
)
from src.modeling import algorithms                                  # noqa: E402
from src.modeling import feature_information as fi                   # noqa: E402
from src.domain.model_models import ModelFamily                      # noqa: E402

UTC = timezone.utc

TARGET_DB = os.path.join(ROOT, "data", "marketlens_corpus_v2.db")
F6B_HANDOFF = os.path.join(ROOT, "docs", "PHASE_25_9I_F6B_HANDOFF.json")
PREREGISTRATION = os.path.join(ROOT, "docs",
                               "PHASE_25_9I_F6D_PREREGISTRATION.md")
RESULT_PATH = os.path.join(ROOT, "docs", "PHASE_25_9I_F6D_RESULTS.json")

PHASE = "25.9I-F.6D"

#: The plan's hash, as committed at 2e3cc2d before this file existed.
PREREGISTRATION_SHA256 = (
    "4563b3ef1087f5e2a79597e6438b4ec525759d2d235242fdd2e84b11c0cce137")

#: Pre-registration section 6. d10 deferred on an empty holdout, the same
#: rule F.6C applied.
HORIZONS: Tuple[str, ...] = ("d1", "d3", "d5")
DEFERRED: Dict[str, str] = {
    "d10": "DEFERRED_INSUFFICIENT_HOLDOUT",
    "intraday_5m": "DEFERRED_BY_F7_POLICY",
    "intraday_15m": "DEFERRED_BY_F7_POLICY",
    "intraday_30m": "DEFERRED_BY_F7_POLICY",
    "intraday_60m": "DEFERRED_BY_F7_POLICY",
    "d20": "PROTECTED",
}

#: Pre-registration sections 4 and 5. Verified against the snapshot.
EXPECTED_NUMERIC_FEATURES = 26
EXPECTED_REGISTRY_FEATURES = 33
FEATURE_INVENTORY_SHA256 = (
    "45dddece5017387d5c6590ad14da701dfcca372e2548dcb8576d4460af671147")
HYPOTHESIS_FAMILY_SHA256 = (
    "293060c829933584b9983855df5f31d1aa49a099805e9278cd798314e72d414c")

#: Pre-registration section 10 and 11.
PERMUTATIONS = 500
PERMUTATION_SEED = 20261004
FDR_Q = 0.05

#: Pre-registration section 12. Structurally justified, named in advance.
INTERACTIONS: Tuple[Tuple[str, str], ...] = (
    ("market.return_5d", "volatility.realized_20d"),
    ("event.confidence", "liquidity.relative_volume_20d"),
    ("market.return_20d", "market.distance_from_ma20"),
)

PROBE_FAMILY = ModelFamily.RIDGE_REGRESSION
PROBE_ALPHA = 1.0


class FrozenPlanViolation(RuntimeError):
    """The plan and the artifacts disagree, or this has already been run."""


class ProtectedHorizonRequested(RuntimeError):
    """Someone pointed the audit at a sealed horizon."""


def guard_no_protected_horizon(horizon: str) -> None:
    """
    The sharpest boundary in this phase.

    `market.return_60d` is a legitimate feature. The protected artifact
    is the hypothesis relating it to the d20 abnormal return, so the
    horizon is refused rather than the feature.
    """
    if rs.is_protected(horizon) or horizon == "d20":
        raise ProtectedHorizonRequested(
            f"{horizon} is sealed; pairing any feature with it would compute "
            f"a protected statistic")


# ======================================================================
# Pre-flight
# ======================================================================

def preflight(db_path: str = TARGET_DB) -> Dict[str, Any]:
    """Verify the frozen plan, the dataset, and both predecessors."""
    problems: List[str] = []

    if not os.path.exists(PREREGISTRATION):
        raise FrozenPlanViolation(f"pre-registration missing: {PREREGISTRATION}")
    prereg_hash = file_digest(PREREGISTRATION)
    if prereg_hash != PREREGISTRATION_SHA256:
        raise FrozenPlanViolation(
            f"the pre-registration changed since it was frozen: expected "
            f"{PREREGISTRATION_SHA256[:16]}..., found {prereg_hash[:16]}...")

    if not os.path.exists(db_path):
        raise FrozenPlanViolation(f"snapshot missing: {db_path}")
    with open(F6B_HANDOFF, encoding="utf-8") as handle:
        handoff = json.load(handle)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        snapshot = load_snapshot(conn, handoff["snapshot"]["snapshot_id"])
        if snapshot is None:
            raise FrozenPlanViolation("the research snapshot is not registered")
        if snapshot.content_hash != handoff["snapshot"]["content_hash"]:
            problems.append("snapshot hash differs from the handoff")
        if not verify_snapshot_hash(conn, snapshot):
            problems.append("snapshot content no longer hashes to its frozen "
                            "value -- the dataset has changed")

        derived = rs.deterministic_holdout(conn,
                                           snapshot_id=snapshot.snapshot_id)
        if derived.boundary.isoformat() != handoff["holdout"]["boundary"]:
            problems.append("the holdout rule no longer yields the inherited "
                            "boundary")

        # Both predecessors must be exactly as recorded.
        predecessors: Dict[str, Any] = {}
        for phase, expected_rows in (("25.9I-F.6B", 16), ("25.9I-F.6C", 12)):
            row = conn.execute(
                "SELECT execution_id, status, qualified_count FROM "
                "research_executions WHERE phase = ? AND status = 'complete' "
                "ORDER BY started_at DESC LIMIT 1", (phase,)).fetchone()
            if not row:
                problems.append(f"{phase}'s completed execution is missing")
                continue
            if row[2] != 0:
                problems.append(f"{phase}'s qualified count is no longer 0")
            rows = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(qualified), 0) FROM "
                "research_candidate_results WHERE execution_id = ?",
                (row[0],)).fetchone()
            if rows != (expected_rows, 0):
                problems.append(f"{phase}'s candidate rows changed: {rows}")
            predecessors[phase] = {
                "execution_id": row[0], "status": row[1],
                "qualified_count": row[2], "candidate_rows": rows[0],
                "qualified_rows": rows[1]}

        existing = find_execution(conn, preregistration_hash=prereg_hash,
                                  snapshot_hash=snapshot.content_hash)

        registry = sorted(r[0] for r in conn.execute(
            "SELECT DISTINCT qualified_name FROM research_features"))
        names, X, _Y, _c, _cl, _o, _q = load_dataset(
            conn, f"{HORIZONS[0]}.abnormal_return")
    finally:
        conn.close()

    inventory_hash = hashlib.sha256(
        json.dumps(sorted(names), separators=(",", ":")).encode()).hexdigest()
    if inventory_hash != FEATURE_INVENTORY_SHA256:
        problems.append(
            f"the numeric feature inventory changed: expected "
            f"{FEATURE_INVENTORY_SHA256[:16]}..., found {inventory_hash[:16]}...")
    if len(names) != EXPECTED_NUMERIC_FEATURES:
        problems.append(f"{len(names)} numeric features; the plan froze "
                        f"{EXPECTED_NUMERIC_FEATURES}")
    if len(registry) != EXPECTED_REGISTRY_FEATURES:
        problems.append(f"{len(registry)} registry features; the plan froze "
                        f"{EXPECTED_REGISTRY_FEATURES}")

    family = [f"{f}|{h}" for h in HORIZONS for f in sorted(names)]
    family_hash = hashlib.sha256(
        json.dumps(family, separators=(",", ":")).encode()).hexdigest()
    if family_hash != HYPOTHESIS_FAMILY_SHA256:
        problems.append("the hypothesis family changed")

    for horizon in HORIZONS:
        guard_no_protected_horizon(horizon)
    if DEFERRED.get("d20") != "PROTECTED":
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
        "holdout_boundary": handoff["holdout"]["boundary"],
        "holdout_origin": "inherited unchanged from the F.7 handoff",
        "horizons": list(HORIZONS),
        "deferred": dict(DEFERRED),
        "numeric_features": sorted(names),
        "registry_features": registry,
        "feature_inventory_hash": inventory_hash,
        "hypothesis_family_hash": family_hash,
        "hypothesis_family_size": len(family),
        "purge_days": {h: float(handoff["purge_days"][h]) for h in HORIZONS},
        "embargo_days": float(handoff["embargo_days"]),
        "permutations": PERMUTATIONS,
        "permutation_seed": PERMUTATION_SEED,
        "fdr_q": FDR_Q,
        "interactions": [list(pair) for pair in INTERACTIONS],
        "min_windows": rs.MIN_VALID_FOLDS,
        "min_clusters": rs.MIN_EFFECTIVE_SAMPLE,
        "predecessors_preserved": predecessors,
        # Without this the run-once guard in `run()` reads None forever
        # and a completed experiment can be executed again as if it were
        # independent evidence. It was computed above and dropped on the
        # way out; a test now holds it in place.
        "existing_execution": existing,
        "code_commit": current_commit(ROOT),
        "d20_excluded": True,
    }


# ======================================================================
# Inventory, missingness and timing
# ======================================================================

def audit_inventory(conn: sqlite3.Connection,
                    context: Dict[str, Any]) -> Dict[str, Any]:
    """
    The real inventory: every registered feature, its namespace, its
    availability and its timing status.

    Timing is the gate that can disqualify a feature outright, so it is
    measured from stored `as_of` values rather than assumed from the
    pipeline's intent.
    """
    total_observations = conn.execute(
        "SELECT COUNT(*) FROM research_observations").fetchone()[0]
    numeric = set(context["numeric_features"])
    rows: List[Dict[str, Any]] = []

    for (name,) in conn.execute(
            "SELECT DISTINCT qualified_name FROM research_features ORDER BY 1"):
        values = conn.execute(
            "SELECT value_json FROM research_features WHERE qualified_name = ?",
            (name,)).fetchall()
        usable = nulls = other = 0
        for (raw,) in values:
            try:
                parsed = json.loads(raw) if raw else None
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, bool) or not isinstance(parsed, (int, float)):
                if parsed is None:
                    nulls += 1
                else:
                    other += 1
            else:
                usable += 1

        late = conn.execute("""
            SELECT COUNT(*) FROM research_features f
            JOIN research_observations o ON o.observation_id = f.observation_id
            WHERE f.qualified_name = ? AND f.as_of IS NOT NULL
              AND f.as_of > o.information_cutoff""", (name,)).fetchone()[0]
        contemporaneous = conn.execute(
            "SELECT COUNT(*) FROM research_features WHERE qualified_name = ? "
            "AND is_contemporaneous", (name,)).fetchone()[0]

        kind = ("numeric" if name in numeric
                else "categorical" if other > 0
                else "no_usable_value")
        rows.append({
            "feature": name,
            "group": name.split(".")[0],
            "kind": kind,
            "in_design_matrix": name in numeric,
            "rows": len(values),
            "usable_numeric": usable,
            "nulls": nulls,
            "non_numeric": other,
            "availability_pct": round(100.0 * usable / total_observations, 1)
            if total_observations else None,
            "contemporaneous_rows": contemporaneous,
            "as_of_after_cutoff": late,
            "timing": "OK" if late == 0 else
                      f"TEMPORAL_INTEGRITY_FAILED: {late} value(s) dated after "
                      f"their observation's information cutoff",
        })

    groups: Dict[str, List[str]] = collections.OrderedDict()
    for name in context["numeric_features"]:
        groups.setdefault(name.split(".")[0], []).append(name)

    return {
        "total_observations": total_observations,
        "registry_count": len(rows),
        "numeric_count": sum(1 for r in rows if r["kind"] == "numeric"),
        "categorical_count": sum(1 for r in rows if r["kind"] == "categorical"),
        "no_usable_value_count": sum(1 for r in rows
                                     if r["kind"] == "no_usable_value"),
        "timing_failures": [r["feature"] for r in rows if r["timing"] != "OK"],
        "features": rows,
        "groups": {g: fs for g, fs in groups.items()},
        "group_count": len(groups),
        "namespaces_absent_from_snapshot": sorted(
            {"market", "sector", "entity", "event", "macro", "volatility",
             "liquidity", "sentiment", "regime", "relationship", "news",
             "peer", "technical", "fundamental", "cross_sectional",
             "historical"} - set(groups)),
    }


# ======================================================================
# Windows
# ======================================================================

def evaluation_windows(cutoffs, purge_days: float, holdout_start: datetime
                       ) -> List[Dict[str, Any]]:
    """
    The disjoint test windows of the canonical fold design.

    A univariate audit needs no training window -- the question is
    whether the relationship holds inside each window and is stable
    across them -- so only the test indices are used. Probes use the
    training indices as well.
    """
    folds = build_folds(cutoffs, purge_days, holdout_start=holdout_start)
    return [f for f in folds if f["valid"]]


# ======================================================================
# Univariate audit
# ======================================================================

def audit_feature(feature: str, column_index: int, names, X, Y, clusters,
                  windows: Sequence[Dict[str, Any]], *, horizon: str,
                  inventory_row: Dict[str, Any],
                  redundancy: Dict[str, Any]) -> fi.InformationResult:
    """One feature on one horizon, across the disjoint windows."""
    paired: List[Tuple[List[Optional[float]], List[Optional[float]]]] = []
    pooled_feature: List[Optional[float]] = []
    pooled_outcome: List[Optional[float]] = []
    pooled_clusters: List[str] = []

    for window in windows:
        idx = window["test_idx"]
        column = [X[i][column_index] for i in idx]
        outcome = [Y[i] for i in idx]
        paired.append((column, outcome))
        pooled_feature.extend(column)
        pooled_outcome.extend(outcome)
        pooled_clusters.extend(clusters[i] for i in idx)

    per_window = tuple(fi.spearman(f, o) for f, o in paired)
    stability = fi.sign_consistency(per_window)
    null = fi.block_permutation_pvalue(paired, permutations=PERMUTATIONS,
                                       seed=PERMUTATION_SEED)
    usable_pairs = sum(1 for f, o in zip(pooled_feature, pooled_outcome)
                       if f is not None and o is not None)
    usable_clusters = len({c for c, f, o in zip(pooled_clusters, pooled_feature,
                                                pooled_outcome)
                           if f is not None and o is not None})

    present = [v for v in pooled_feature if v is not None]
    distinct = len(set(present))

    return fi.InformationResult(
        feature=feature, group=feature.split(".")[0], horizon=horizon,
        windows=len(windows), pooled_pairs=usable_pairs,
        pooled_clusters=usable_clusters,
        availability=inventory_row["availability_pct"],
        distinct_values=distinct,
        constant_value=present[0] if distinct == 1 else None,
        per_window_rho=per_window, stability=stability, null=null,
        trimmed_mean_rho=fi.trimmed_spearman(pooled_feature, pooled_outcome),
        redundancy=redundancy["classification"].get(feature,
                                                    fi.Redundancy.UNKNOWN.value),
        timing=inventory_row["timing"])


def concentration_check(feature_index: int, X, Y, clusters,
                        windows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Recompute with the largest-contributing instrument removed.

    One ticker must not be allowed to produce a corpus-wide finding. The
    instrument removed is the one with the most rows across the pooled
    windows -- chosen by row count, which is outcome-blind.
    """
    counts: collections.Counter = collections.Counter()
    for window in windows:
        for i in window["test_idx"]:
            if X[i][feature_index] is not None and Y[i] is not None:
                counts[clusters[i]] += 1
    if not counts:
        return {"survives": None, "status": "NOT_COMPUTED: no usable rows"}
    dominant, dominant_rows = counts.most_common(1)[0]

    reduced = [([X[i][feature_index] for i in window["test_idx"]
                 if clusters[i] != dominant],
                [Y[i] for i in window["test_idx"]
                 if clusters[i] != dominant])
               for window in windows]
    per_window = [fi.spearman(f, o) for f, o in reduced]
    stability = fi.sign_consistency(per_window)
    null = fi.block_permutation_pvalue(reduced, permutations=PERMUTATIONS,
                                       seed=PERMUTATION_SEED)
    survives = bool(stability.get("all_same_sign")
                    and null.get("p_value") is not None
                    and null["p_value"] <= FDR_Q)
    return {
        "dominant_instrument": dominant,
        "dominant_rows": dominant_rows,
        "share_of_rows": round(dominant_rows / sum(counts.values()), 4),
        "mean_rho_without": stability.get("mean"),
        "all_same_sign_without": stability.get("all_same_sign"),
        "p_value_without": null.get("p_value"),
        "survives": survives,
        "status": "COMPUTED",
    }


# ======================================================================
# Probes
# ======================================================================

def probe(columns: Sequence[int], names, X, Y,
          windows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """
    A fixed low-capacity ridge probe, scored by rank agreement.

    Not a model candidate: never promoted, never gated on, and a good
    score authorises only a future hypothesis. Normalization is fitted
    inside the canonical fitter on the training rows alone, and rows
    with any missing feature are dropped rather than imputed.
    """
    per_window: List[Optional[float]] = []
    detail: List[Dict[str, Any]] = []
    paired: List[Tuple[List[Optional[float]], List[Optional[float]]]] = []

    for window in windows:
        train_idx, test_idx = window["train_idx"], window["test_idx"]
        X_train = [[X[i][c] for c in columns] for i in train_idx]
        Y_train = [Y[i] for i in train_idx]
        X_test = [[X[i][c] for c in columns] for i in test_idx]
        Y_test = [Y[i] for i in test_idx]
        complete_train = sum(1 for row in X_train
                             if all(v is not None for v in row))
        parameters = algorithms.fit(PROBE_FAMILY, X_train, Y_train,
                                    alpha=PROBE_ALPHA)
        predictions = algorithms.predict_batch(parameters, X_test)
        rho = fi.spearman(predictions, Y_test)
        per_window.append(rho)
        paired.append((predictions, Y_test))
        detail.append({
            "window": window["label"],
            "train_rows": len(train_idx),
            "complete_train_rows": complete_train,
            "test_rows": len(test_idx),
            "scored": sum(1 for p, y in zip(predictions, Y_test)
                          if p is not None and y is not None),
            "rho": rho,
        })

    stability = fi.sign_consistency(tuple(per_window))
    null = fi.block_permutation_pvalue(paired, permutations=PERMUTATIONS,
                                       seed=PERMUTATION_SEED)
    return {"features": len(columns), "windows": detail,
            "stability": stability, "null": null}


def interaction_column(X, left: int, right: int) -> List[Optional[float]]:
    return [None if row[left] is None or row[right] is None
            else row[left] * row[right] for row in X]


# ======================================================================
# Plan
# ======================================================================

def build_plan(db_path: str = TARGET_DB) -> Dict[str, Any]:
    context = preflight(db_path)
    holdout_start = datetime.fromisoformat(context["holdout_boundary"])
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        inventory = audit_inventory(conn, context)
        per_horizon = []
        for horizon in context["horizons"]:
            guard_no_protected_horizon(horizon)
            names, X, Y, cutoffs, clusters, _o, quality = load_dataset(
                conn, f"{horizon}.abnormal_return")
            windows = evaluation_windows(
                cutoffs, context["purge_days"][horizon], holdout_start)
            complete = sum(1 for row in X if all(v is not None for v in row))
            per_horizon.append({
                "horizon": horizon, "rows": len(X),
                "complete_rows": complete,
                "complete_pct": round(100.0 * complete / len(X), 1) if X else None,
                "clusters": len(set(clusters)),
                "windows": len(windows),
                "test_rows": [len(w["test_idx"]) for w in windows],
                "holdout_rows": sum(1 for c in cutoffs if c >= holdout_start),
                "rows_per_cluster_per_window": round(
                    sum(len(w["test_idx"]) for w in windows)
                    / max(1, sum(len({clusters[i] for i in w["test_idx"]})
                                 for w in windows)), 3),
            })
        redundancy_names, redX, _y, _c, _cl, _ob, _q = load_dataset(
            conn, f"{context['horizons'][0]}.abnormal_return")
        redundancy = fi.redundancy_map(
            {n: [row[i] for row in redX]
             for i, n in enumerate(redundancy_names)})
    finally:
        conn.close()

    groups = inventory["groups"]
    cells = {
        "univariate": len(context["numeric_features"]) * len(context["horizons"]),
        "group_probes": len(groups) * len(context["horizons"]),
        "ablation": (1 + len(groups)) * len(context["horizons"]),
        "interactions": len(INTERACTIONS) * len(context["horizons"]),
    }
    cells["total"] = sum(cells.values())

    return {
        "context": context,
        "inventory": inventory,
        "redundancy": redundancy,
        "per_horizon": per_horizon,
        "cells": cells,
        "permutation_budget": cells["univariate"] * PERMUTATIONS,
        "market_data_requests": 0,
        "paid_api_calls": 0,
    }


# ======================================================================
# Run
# ======================================================================

def run(db_path: str = TARGET_DB, *, verbose: bool = True) -> Dict[str, Any]:
    plan = build_plan(db_path)
    context = plan["context"]
    existing = context.get("existing_execution")
    if existing and existing["status"] == "complete":
        raise FrozenPlanViolation(
            f"already executed as {existing['execution_id']} "
            f"({existing['completed_at']}) -- use --show")

    execution_id = f"f6d-{uuid.uuid4().hex[:16]}"
    started_at = datetime.now(UTC)
    wall = time.time()
    holdout_start = datetime.fromisoformat(context["holdout_boundary"])
    cell_ids = [f"{f}|{h}" for h in context["horizons"]
                for f in context["numeric_features"]]

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
              started_at.isoformat(), "running", json.dumps(cell_ids), "[]",
              f"feature_inventory_hash={context['feature_inventory_hash']}; "
              f"family_hash={context['hypothesis_family_hash']}"))
        conn.commit()
    finally:
        conn.close()

    if verbose:
        print(f"PHASE {PHASE} EXECUTION {execution_id}")
        print(f"  snapshot  : {context['snapshot_id']} "
              f"({context['snapshot_hash'][:16]}...)")
        print(f"  prereg    : {context['preregistration_hash'][:16]}...")
        print(f"  inventory : {context['feature_inventory_hash'][:16]}... "
              f"({len(context['numeric_features'])} numeric of "
              f"{len(context['registry_features'])})")
        print(f"  family    : {context['hypothesis_family_size']} hypotheses, "
              f"BH q={FDR_Q}, {PERMUTATIONS} permutations, "
              f"seed {PERMUTATION_SEED}")
        print(f"  holdout   : {context['holdout_boundary']} "
              f"({context['holdout_origin']})")
        print()

    results: List[fi.InformationResult] = []
    groups_result: List[Dict[str, Any]] = []
    ablation_result: List[Dict[str, Any]] = []
    interaction_result: List[Dict[str, Any]] = []
    errors: List[str] = []
    datasets: Dict[str, Any] = {}

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for horizon in context["horizons"]:
            guard_no_protected_horizon(horizon)
            names, X, Y, cutoffs, clusters, _o, _q = load_dataset(
                conn, f"{horizon}.abnormal_return")
            datasets[horizon] = (names, X, Y, cutoffs, clusters)
            windows = evaluation_windows(
                cutoffs, context["purge_days"][horizon], holdout_start)
            index_of = {n: i for i, n in enumerate(names)}
            inventory_rows = {r["feature"]: r
                              for r in plan["inventory"]["features"]}

            for feature in context["numeric_features"]:
                try:
                    results.append(audit_feature(
                        feature, index_of[feature], names, X, Y, clusters,
                        windows, horizon=horizon,
                        inventory_row=inventory_rows[feature],
                        redundancy=plan["redundancy"]))
                except Exception as error:                   # noqa: BLE001
                    errors.append(f"{feature}|{horizon}: "
                                  f"{type(error).__name__}: {error}")
            if verbose:
                print(f"  {horizon}: {len(windows)} windows, "
                      f"{len(context['numeric_features'])} features audited",
                      flush=True)

            # Group probes, ablation, interactions.
            for group, members in plan["inventory"]["groups"].items():
                groups_result.append({
                    "group": group, "horizon": horizon,
                    "members": members,
                    **probe([index_of[m] for m in members], names, X, Y,
                            windows)})
            full_columns = [index_of[f] for f in context["numeric_features"]]
            ablation_result.append({
                "configuration": "FULL", "horizon": horizon,
                "removed_group": None,
                **probe(full_columns, names, X, Y, windows)})
            for group, members in plan["inventory"]["groups"].items():
                keep = [index_of[f] for f in context["numeric_features"]
                        if f not in members]
                ablation_result.append({
                    "configuration": f"FULL_MINUS_{group.upper()}",
                    "horizon": horizon, "removed_group": group,
                    **probe(keep, names, X, Y, windows)})
            for left, right in INTERACTIONS:
                extended = [row + [v] for row, v in zip(
                    X, interaction_column(X, index_of[left], index_of[right]))]
                interaction_result.append({
                    "interaction": f"{left} x {right}", "horizon": horizon,
                    **probe([index_of[left], index_of[right], len(names)],
                            names + [f"{left}*{right}"], extended, Y, windows)})
    finally:
        conn.close()

    # Benjamini-Hochberg over the declared family.
    p_values = {f"{r.feature}|{r.horizon}": r.null.get("p_value")
                for r in results}
    bh = fi.benjamini_hochberg(p_values, q=FDR_Q)

    # Concentration only for cells that could otherwise be candidates --
    # bounded work, and it cannot rescue anything that already failed.
    finalised: List[fi.InformationResult] = []
    candidate_by_feature: Dict[Tuple[str, str], bool] = {}
    partners = {}
    for left, right, _rho in (plan["redundancy"]["exact_or_near_duplicates"]
                              + plan["redundancy"]["high_redundancy_pairs"]):
        partners.setdefault(left, []).append(right)
        partners.setdefault(right, []).append(left)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for result in results:
            key = f"{result.feature}|{result.horizon}"
            rejected = key in bh["rejected"]
            concentration: Dict[str, Any] = {}
            if rejected and result.stability.get("all_same_sign"):
                names, X, Y, cutoffs, clusters = datasets[result.horizon]
                windows = evaluation_windows(
                    cutoffs, context["purge_days"][result.horizon],
                    holdout_start)
                concentration = concentration_check(
                    names.index(result.feature), X, Y, clusters, windows)
            from dataclasses import replace
            staged = replace(result, fdr_rejected=rejected,
                             adjusted_p_value=bh["adjusted"].get(key),
                             concentration=concentration)
            partner_is_candidate = any(
                candidate_by_feature.get((p, result.horizon))
                for p in partners.get(result.feature, []))
            verdict, notes = fi.classify(
                staged, min_windows=context["min_windows"],
                min_clusters=context["min_clusters"],
                redundant_partner_is_candidate=partner_is_candidate)
            staged = replace(staged, verdict=verdict, notes=tuple(notes))
            candidate_by_feature[(result.feature, result.horizon)] = (
                verdict == fi.FeatureVerdict.STABLE_INFORMATION_CANDIDATE.value)
            finalised.append(staged)
    finally:
        conn.close()

    # Group verdicts.
    for record in groups_result:
        stability = record["stability"]
        p = record["null"].get("p_value")
        if stability.get("scored", 0) < context["min_windows"]:
            verdict = fi.GroupVerdict.UNKNOWN.value
        elif p is not None and p <= FDR_Q and stability.get("all_same_sign"):
            verdict = fi.GroupVerdict.PROMISING_INFORMATION_GROUP.value
        elif p is not None and p <= FDR_Q:
            verdict = fi.GroupVerdict.UNSTABLE.value
        elif stability.get("all_same_sign"):
            verdict = fi.GroupVerdict.WEAK.value
        else:
            verdict = fi.GroupVerdict.NO_EVIDENCE.value
        record["verdict"] = verdict

    survivors = sorted(
        f"{r.feature}|{r.horizon}" for r in finalised
        if r.verdict == fi.FeatureVerdict.STABLE_INFORMATION_CANDIDATE.value)
    promising_groups = sorted(
        f"{g['group']}|{g['horizon']}" for g in groups_result
        if g["verdict"] == fi.GroupVerdict.PROMISING_INFORMATION_GROUP.value)
    survivor_hash = hashlib.sha256(
        json.dumps(survivors + promising_groups,
                   separators=(",", ":")).encode()).hexdigest()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE research_executions SET note = note || ? "
                     "WHERE execution_id = ?",
                     (f"; survivors={len(survivors)}; "
                      f"survivor_hash={survivor_hash}", execution_id))
        conn.commit()
    finally:
        conn.close()

    holdout_opened = bool(survivors or promising_groups)
    holdout: Dict[str, Any] = {
        "opened": holdout_opened,
        "reason": ("survivors existed, so the frozen list was confirmed on "
                   "unseen time" if holdout_opened else
                   "zero candidates survived the research-region gates, so "
                   "the holdout was not read"),
        "survivor_list_hash": survivor_hash,
        "results": [],
    }

    if holdout_opened:
        for key in survivors:
            feature, horizon = key.split("|")
            names, X, Y, cutoffs, clusters = datasets[horizon]
            idx = [i for i, c in enumerate(cutoffs) if c >= holdout_start]
            column = [X[i][names.index(feature)] for i in idx]
            outcome = [Y[i] for i in idx]
            rho = fi.spearman(column, outcome)
            research_mean = next(
                r.mean_rho for r in finalised
                if r.feature == feature and r.horizon == horizon)
            confirmed = bool(
                rho is not None and research_mean is not None
                and (rho > 0) == (research_mean > 0))
            holdout["results"].append({
                "kind": "feature", "candidate": key,
                "holdout_rows": len(idx),
                "holdout_rho": rho, "research_mean_rho": research_mean,
                "same_direction": confirmed,
                "verdict": ("FEATURE_INFORMATION_CONFIRMED" if confirmed
                            else "REJECTED_AT_HOLDOUT"),
            })

        # Promising GROUPS earn a holdout look too. Pre-registration
        # section 14 opens the holdout for a feature OR a group, and
        # section 39 reports confirmation for a group just as for a
        # feature. An earlier build opened the holdout on the strength of
        # two promising groups and then evaluated only the (empty)
        # feature list -- it claimed to have opened the holdout and
        # confirmed nothing. That run was invalidated rather than
        # patched over.
        for key in promising_groups:
            group, horizon = key.split("|")
            names, X, Y, cutoffs, clusters = datasets[horizon]
            index_of = {n: i for i, n in enumerate(names)}
            members = plan["inventory"]["groups"][group]
            columns = [index_of[m] for m in members]
            research_idx = [i for i, c in enumerate(cutoffs)
                            if c < holdout_start]
            holdout_idx = [i for i, c in enumerate(cutoffs)
                           if c >= holdout_start]
            parameters = algorithms.fit(
                PROBE_FAMILY,
                [[X[i][c] for c in columns] for i in research_idx],
                [Y[i] for i in research_idx], alpha=PROBE_ALPHA)
            predictions = algorithms.predict_batch(
                parameters, [[X[i][c] for c in columns] for i in holdout_idx])
            outcome = [Y[i] for i in holdout_idx]
            rho = fi.spearman(predictions, outcome)
            research_mean = next(
                g["stability"].get("mean") for g in groups_result
                if g["group"] == group and g["horizon"] == horizon)
            confirmed = bool(
                rho is not None and research_mean is not None
                and (rho > 0) == (research_mean > 0))
            holdout["results"].append({
                "kind": "group", "candidate": key,
                "members": members,
                "holdout_rows": len(holdout_idx),
                "probe_train_rows": len(research_idx),
                "holdout_rho": rho, "research_mean_rho": research_mean,
                "same_direction": confirmed,
                "verdict": ("GROUP_INFORMATION_CONFIRMED" if confirmed
                            else "REJECTED_AT_HOLDOUT"),
            })

        evaluated = len(holdout["results"])
        expected = len(survivors) + len(promising_groups)
        if evaluated != expected:
            errors.append(
                f"the holdout was opened for {expected} candidate(s) but "
                f"evaluated {evaluated}")

    completed_at = datetime.now(UTC)
    status = "failed" if errors else "complete"
    funnel = fi.information_funnel(finalised)

    result = {
        "execution_id": execution_id, "phase": PHASE, "status": status,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "runtime_seconds": round(time.time() - wall, 2),
        "frozen_plan": {k: context[k] for k in (
            "preregistration_path", "preregistration_hash", "snapshot_id",
            "snapshot_hash", "corpus_version_id", "dataset_version",
            "as_of_cutoff", "holdout_boundary", "holdout_origin", "horizons",
            "deferred", "feature_inventory_hash", "hypothesis_family_hash",
            "hypothesis_family_size", "purge_days", "embargo_days",
            "permutations", "permutation_seed", "fdr_q", "interactions",
            "min_windows", "min_clusters", "code_commit")},
        "inventory": plan["inventory"],
        "redundancy": plan["redundancy"],
        "per_horizon": plan["per_horizon"],
        "cells": plan["cells"],
        "univariate": [r.describe() for r in finalised],
        "multiple_testing": bh,
        "groups": groups_result,
        "ablation": ablation_result,
        "interactions": interaction_result,
        "information_funnel": funnel,
        "pre_holdout_survivors": survivors,
        "promising_groups": promising_groups,
        "holdout": holdout,
        "models_qualified": 0,
        "models_promoted": 0,
        "thresholds_changed": 0,
        "expected_return_capable": False,
        "target_capable": False,
        "broker_writes": 0,
        "market_data_requests": 0,
        "predecessors_preserved": context["predecessors_preserved"],
        "errors": errors,
    }

    conn = sqlite3.connect(db_path)
    try:
        for record in finalised:
            conn.execute("""
                INSERT OR REPLACE INTO research_candidate_results
                    (execution_id, cell_id, horizon_id, candidate, snapshot_id,
                     snapshot_hash, preregistration_hash, code_commit,
                     model_family, task, feature_config, survival_status,
                     holdout_verdict, qualified, failed_gates_json,
                     metrics_json, folds_json, notes_json, evaluated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (execution_id, f"{record.feature}|{record.horizon}",
                  record.horizon, record.feature, context["snapshot_id"],
                  context["snapshot_hash"], context["preregistration_hash"],
                  context["code_commit"], "univariate_audit", "information",
                  record.group, record.verdict, None, 0,
                  json.dumps(list(record.notes)),
                  json.dumps({"stability": record.stability,
                              "null": record.null,
                              "adjusted_p": record.adjusted_p_value,
                              "concentration": record.concentration},
                             default=str),
                  json.dumps(list(record.per_window_rho)),
                  json.dumps(list(record.notes)), completed_at.isoformat()))
        conn.execute("""
            UPDATE research_executions SET completed_at = ?, status = ?,
                   completed_cells_json = ?, qualified_count = ?
            WHERE execution_id = ?
        """, (completed_at.isoformat(), status,
              json.dumps([f"{r.feature}|{r.horizon}" for r in finalised]),
              len(survivors), execution_id))
        conn.commit()
    finally:
        conn.close()

    with open(RESULT_PATH, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, sort_keys=True, default=str)
    result["result_path"] = os.path.relpath(RESULT_PATH, ROOT)
    return result


def invalidate(db_path: str, *, reason: str) -> Dict[str, Any]:
    """
    Mark the recorded execution INVALIDATED, with the reason, so a
    defect fix can be rerun.

    Same semantics as the F.6C harness: the marker and its rows are kept
    and annotated, never deleted, so the register shows the invalidated
    attempt beside the valid one rather than a tidied history.
    """
    if not reason.strip():
        raise FrozenPlanViolation("an invalidation must state its reason")
    conn = sqlite3.connect(db_path)
    try:
        initialize_execution_schema(conn)
        rows = conn.execute(
            "SELECT execution_id FROM research_executions WHERE phase = ? "
            "AND status != 'invalidated' ORDER BY started_at DESC",
            (PHASE,)).fetchall()
        for (execution_id,) in rows:
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
        existing = find_execution(
            conn, preregistration_hash=file_digest(PREREGISTRATION),
            snapshot_hash=json.load(open(F6B_HANDOFF, encoding="utf-8"))
            ["snapshot"]["content_hash"])
    finally:
        conn.close()
    return existing or {"found": False, "reason": "no execution recorded"}


# ======================================================================
# CLI
# ======================================================================

def _print_plan(plan: Dict[str, Any]) -> None:
    context, inv = plan["context"], plan["inventory"]
    print(f"PHASE {PHASE} FROZEN PLAN (nothing measured against an outcome)")
    print(f"  snapshot        : {context['snapshot_id']}")
    print(f"  prereg hash     : {context['preregistration_hash']}")
    print(f"  pinned hash     : {PREREGISTRATION_SHA256}")
    print(f"  inventory hash  : {context['feature_inventory_hash']}")
    print(f"  family hash     : {context['hypothesis_family_hash']} "
          f"({context['hypothesis_family_size']} hypotheses)")
    print(f"  holdout         : {context['holdout_boundary']} "
          f"-- {context['holdout_origin']}")
    print(f"  horizons        : {context['horizons']}")
    print(f"  deferred        : {context['deferred']}")
    print(f"  predecessors    : {context['predecessors_preserved']}")
    print()
    print(f"  INVENTORY: {inv['registry_count']} registered | "
          f"{inv['numeric_count']} numeric | {inv['categorical_count']} "
          f"categorical | {inv['no_usable_value_count']} no usable value")
    print(f"  timing failures : {inv['timing_failures'] or 'none'}")
    print(f"  groups ({inv['group_count']}): "
          f"{ {g: len(f) for g, f in inv['groups'].items()} }")
    print(f"  namespaces absent from this snapshot: "
          f"{inv['namespaces_absent_from_snapshot']}")
    print()
    print(f"  {'feature':38s}{'kind':16s}{'avail%':>8}{'contemp':>9}{'late':>6}")
    for row in inv["features"]:
        print(f"  {row['feature']:38s}{row['kind']:16s}"
              f"{str(row['availability_pct']):>8}"
              f"{row['contemporaneous_rows']:>9}{row['as_of_after_cutoff']:>6}")
    print()
    red = plan["redundancy"]
    print(f"  REDUNDANCY: {red['features']} features -> "
          f"{red['effective_unique_count']} effective unique")
    print(f"    identical fingerprints : {red['identical_fingerprints'] or 'none'}")
    print(f"    near-duplicates        : {red['exact_or_near_duplicates'] or 'none'}")
    for left, right, rho in red["high_redundancy_pairs"]:
        print(f"    high redundancy        : {left} <-> {right}  rho={rho}")
    print()
    print("  HORIZON GEOMETRY")
    for item in plan["per_horizon"]:
        print(f"    {item['horizon']}: rows={item['rows']} "
              f"complete={item['complete_rows']} ({item['complete_pct']}%) "
              f"clusters={item['clusters']} windows={item['windows']} "
              f"test={item['test_rows']} holdout={item['holdout_rows']} "
              f"rows/cluster/window={item['rows_per_cluster_per_window']}")
    print()
    cells = plan["cells"]
    print(f"  BUDGET: univariate={cells['univariate']} "
          f"group={cells['group_probes']} ablation={cells['ablation']} "
          f"interactions={cells['interactions']} TOTAL={cells['total']}")
    print(f"  permutations: {plan['permutation_budget']:,} "
          f"(seed {PERMUTATION_SEED})")
    print(f"  market-data requests={plan['market_data_requests']} "
          f"paid API calls={plan['paid_api_calls']}")


def _print_result(result: Dict[str, Any]) -> None:
    print()
    print("RESULT")
    print(f"  execution : {result['execution_id']} "
          f"status={result['status'].upper()} "
          f"runtime={result['runtime_seconds']}s")
    for error in result["errors"]:
        print(f"  ERROR     : {error}")
    print()
    print("  UNIVARIATE (feature x horizon)")
    print(f"    {'feature':34s}{'hz':4s}{'mean rho':>10}{'trim':>9}"
          f"{'sign':>7}{'p':>8}{'adj p':>8}  verdict")
    for row in result["univariate"]:
        st = row["stability"]
        print(f"    {row['feature']:34s}{row['horizon']:4s}"
              f"{str(row['mean_rho']):>10}{str(row['trimmed_mean_rho']):>9}"
              f"{str(st.get('positive'))+'/'+str(st.get('negative')):>7}"
              f"{str(row['null'].get('p_value')):>8}"
              f"{str(row['adjusted_p_value']):>8}  {row['verdict']}")
    bh = result["multiple_testing"]
    print()
    print(f"  MULTIPLE TESTING: family={bh['family_size']} tested={bh['tested']} "
          f"nominal p<={bh['q']}: {bh['nominal_below_q']} "
          f"-> FDR-rejected: {bh['rejected_count']} {bh['rejected']}")
    print()
    print("  GROUP PROBES")
    print(f"    {'group':14s}{'hz':4s}{'n':>4}{'mean rho':>10}{'sign':>7}"
          f"{'p':>8}  verdict")
    for g in result["groups"]:
        st = g["stability"]
        print(f"    {g['group']:14s}{g['horizon']:4s}{g['features']:>4}"
              f"{str(st.get('mean')):>10}"
              f"{str(st.get('positive'))+'/'+str(st.get('negative')):>7}"
              f"{str(g['null'].get('p_value')):>8}  {g['verdict']}")
    print()
    print("  ABLATION (ridge probe, rank score)")
    for a in result["ablation"]:
        st = a["stability"]
        print(f"    {a['configuration']:26s}{a['horizon']:4s}"
              f"n={a['features']:>3} mean rho={str(st.get('mean')):>10} "
              f"p={str(a['null'].get('p_value'))}")
    print()
    print("  INTERACTIONS")
    for i in result["interactions"]:
        st = i["stability"]
        print(f"    {i['interaction']:58s}{i['horizon']:4s}"
              f"mean rho={str(st.get('mean')):>10} "
              f"p={str(i['null'].get('p_value'))}")
    print()
    print("  INFORMATION FUNNEL")
    for stage, count in result["information_funnel"].items():
        print(f"    {stage:26s} {count}")
    print()
    print(f"  pre-holdout survivors = {len(result['pre_holdout_survivors'])} "
          f"{result['pre_holdout_survivors']}")
    print(f"  promising groups      = {result['promising_groups']}")
    print(f"  survivor list hash    = "
          f"{result['holdout']['survivor_list_hash'][:16]}...")
    print(f"  HOLDOUT OPENED        = {result['holdout']['opened']}")
    print(f"    {result['holdout']['reason']}")
    for h in result["holdout"]["results"]:
        print(f"    {h['candidate']}: rows={h['holdout_rows']} "
              f"rho={h['holdout_rho']} research={h['research_mean_rho']} "
              f"-> {h['verdict']}")
    print()
    print(f"  models qualified={result['models_qualified']} "
          f"promoted={result['models_promoted']} "
          f"thresholds changed={result['thresholds_changed']}")
    print(f"  expected_return_capable={result['expected_return_capable']} "
          f"target_capable={result['target_capable']}")
    if result.get("result_path"):
        print(f"  written to {result['result_path']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=TARGET_DB)
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--invalidate", metavar="REASON", default=None,
                        help="Mark the recorded execution INVALIDATED with "
                             "this reason. The marker and its rows are kept.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        if args.invalidate:
            print(json.dumps(invalidate(args.db, reason=args.invalidate),
                             indent=2))
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
