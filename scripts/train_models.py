"""
Trains and evaluates Phase 9 models on the research dataset assembled
by Phases 7 and 8, and persists the results with their mandatory
baseline comparisons.

READ THIS BEFORE INTERPRETING ANY OUTPUT
--------------------------------------------
On the current dataset this script produces a RESULT, not a FINDING.
The observations span roughly four weeks and come from a few hundred
instruments. WalkForwardSplitter's defaults need about 42 months of
history to build even one train/test cycle; there is nowhere near
that, so this script uses a SINGLE chronological split instead.

That is a deliberate, stated downgrade, not a silent one:

  - A single split cannot show whether performance is stable across
    market regimes. Walk-forward exists precisely to answer that, and
    it is not being answered here.
  - ModelEvaluation.MIN_EFFECTIVE_SAMPLE will very likely flag these
    evaluations as descriptive-only. That flag is persisted, not
    discarded.
  - Observations cluster by instrument, so the effective sample is far
    smaller than the row count. cluster_count is passed through so the
    engine's own warning about this fires.

The purpose of running it now is to prove the pipeline is wired
correctly end to end — data flows from features to labels to a fitted
model to an evaluated comparison. Whether the market is predictable is
a question this dataset cannot answer, and no number printed below
should be read as answering it.

THE SPLIT IS CHRONOLOGICAL, WITH AN EMBARGO
-----------------------------------------------
Train and test are separated by information_cutoff, never randomly:
a random split would let a model learn from observations dated after
the ones it is tested on, which is look-ahead by another name. An
embargo gap is applied between the two so that an observation whose
label resolves after the split boundary cannot appear in training —
the same purging logic walk-forward would apply, reduced to one
boundary.

FEATURES COME FROM BOTH PHASES, LABELS ONLY FROM PHASE 7
------------------------------------------------------------
research_features holds rows from Phase 7 (carried over from the
impact study) and Phase 8 (computed by the feature engine). Both are
used. Labels come from research_labels, which only Phase 7 writes.

Only observations with quality_level != 'invalid' are trained on.
Invalid rows are still counted and reported — they are excluded from
fitting, not hidden.

SAFETY
------
- Schema creation is CREATE TABLE IF NOT EXISTS.
- All research tables are read-only inputs.
- --dry-run trains and evaluates in memory, reporting everything,
  without writing.
- Re-running replaces this script's own rows rather than accumulating.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
SRC = os.path.join(REPO_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from src.data_access.model_schema import initialize_model_schema
from src.domain.model_models import (
    ModelSpecification, ModelFamily, PredictionTask, TrainingWindow,
)
from src.modeling.engine import ModelingEngine, primary_metric_name

DEFAULT_DB = os.path.join(REPO_ROOT, "data", "marketlens.db")

#: Default label to model. d5 balances having enough post-event data
#: to resolve against not reaching so far forward that unrelated news
#: dominates the outcome.
DEFAULT_LABEL = "d5.abnormal_return"

#: Fraction of observations (chronologically earliest) used for
#: training. The rest, minus the embargo, become the test set.
TRAIN_FRACTION = 0.7

#: Gap between the last training observation and the first test one.
#: Must be at least as long as the label horizon, or a training label
#: resolves inside the test period.
EMBARGO_DAYS = 6.0


def load_dataset(conn: sqlite3.Connection, label_name: str
                  ) -> Tuple[List[str], List[List[Optional[float]]], List[Optional[float]],
                             List[datetime], List[str], Counter]:
    """
    Build the design matrix.

    Returns (feature_names, X, Y, cutoffs, cluster_ids, quality_counts).
    Rows are ordered by information_cutoff, oldest first — the
    chronological split depends on that ordering.
    """
    observations = conn.execute("""
        SELECT observation_id, information_cutoff, event_cluster_id, quality_level
        FROM research_observations
        WHERE information_cutoff IS NOT NULL
        ORDER BY information_cutoff ASC
    """).fetchall()

    quality_counts = Counter(row[3] for row in observations)
    usable = [row for row in observations if row[3] != "invalid"]
    if not usable:
        return [], [], [], [], [], quality_counts

    observation_ids = [row[0] for row in usable]
    placeholders = ",".join("?" * len(observation_ids))

    # Only numeric features can enter a design matrix. Categorical
    # ones (event_type, ticker, exchange) are excluded rather than
    # silently encoded — an arbitrary integer encoding would imply an
    # ordering that does not exist.
    feature_rows = conn.execute(f"""
        SELECT observation_id, qualified_name, value_json
        FROM research_features WHERE observation_id IN ({placeholders})
    """, observation_ids).fetchall()

    by_observation: Dict[str, Dict[str, Optional[float]]] = defaultdict(dict)
    numeric_names = set()
    for observation_id, name, value_json in feature_rows:
        try:
            value = json.loads(value_json) if value_json else None
        except (ValueError, TypeError):
            value = None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue  # categorical or null — see comment above
        by_observation[observation_id][name] = float(value)
        numeric_names.add(name)

    label_rows = conn.execute(f"""
        SELECT observation_id, value_json FROM research_labels
        WHERE observation_id IN ({placeholders}) AND name = ?
    """, observation_ids + [label_name]).fetchall()
    labels: Dict[str, Optional[float]] = {}
    for observation_id, value_json in label_rows:
        try:
            value = json.loads(value_json) if value_json else None
        except (ValueError, TypeError):
            value = None
        labels[observation_id] = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    feature_names = sorted(numeric_names)
    X, Y, cutoffs, cluster_ids = [], [], [], []
    for observation_id, cutoff_str, cluster_id, _quality in usable:
        if observation_id not in labels or labels[observation_id] is None:
            continue  # no resolved label — cannot train or score on it
        row = [by_observation[observation_id].get(name) for name in feature_names]
        X.append(row)
        Y.append(labels[observation_id])
        cutoffs.append(datetime.fromisoformat(cutoff_str))
        cluster_ids.append(cluster_id or observation_id)

    return feature_names, X, Y, cutoffs, cluster_ids, quality_counts


def chronological_split(cutoffs: Sequence[datetime], train_fraction: float,
                        embargo_days: float) -> Tuple[List[int], List[int], int]:
    """
    Split by time, not at random, with an embargo gap.

    Returns (train_indices, test_indices, embargoed_count). Rows whose
    cutoff falls inside the embargo window are dropped from BOTH sets —
    that is what an embargo is for.
    """
    if not cutoffs:
        return [], [], 0
    boundary_index = int(len(cutoffs) * train_fraction)
    if boundary_index <= 0 or boundary_index >= len(cutoffs):
        return list(range(len(cutoffs))), [], 0

    boundary = cutoffs[boundary_index]
    embargo_end = boundary + timedelta(days=embargo_days)

    train, test, embargoed = [], [], 0
    for index, cutoff in enumerate(cutoffs):
        if cutoff < boundary:
            train.append(index)
        elif embargo_days > 0 and cutoff <= embargo_end:
            # Strictly inside the embargo window. Guarded on
            # embargo_days > 0 so that a zero embargo drops nothing:
            # without the guard, `cutoff <= boundary` would still
            # exclude the row sitting exactly on the boundary.
            embargoed += 1
        else:
            test.append(index)
    return train, test, embargoed


def persist(conn: sqlite3.Connection, model, evaluation) -> None:
    spec = model.specification
    conn.execute("""
        INSERT OR REPLACE INTO trained_models (
            trained_model_id, model_id, model_qualified_id, name, task, family,
            model_version, label_name, label_version, feature_set_id, feature_set_version,
            dataset_version, hyperparameters_json, parameters_json, feature_names_json,
            train_start, train_end, train_sample_size, train_cluster_count,
            status, training_notes_json, trained_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        model.trained_model_id, spec.model_id, spec.qualified_id, spec.name,
        spec.task.value, spec.family.value, spec.version, spec.label_name,
        spec.label_version, spec.feature_set_id, spec.feature_set_version,
        spec.dataset_version, json.dumps(spec.hyperparameters),
        json.dumps(model.parameters, default=str), json.dumps(model.feature_names),
        _iso(model.train_start), _iso(model.train_end), model.train_sample_size,
        model.train_cluster_count, model.status.value,
        json.dumps(model.training_notes), _iso(model.trained_at),
    ))

    conn.execute("""
        INSERT OR REPLACE INTO model_evaluations (
            evaluation_id, trained_model_id, model_qualified_id, window_label,
            sample_size, cluster_count, effective_sample_size, small_sample,
            beats_all_baselines, abstention_rate, metrics_json, notes_json, evaluated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        evaluation.evaluation_id, evaluation.trained_model_id, evaluation.model_qualified_id,
        evaluation.window_label, evaluation.sample_size, evaluation.cluster_count,
        evaluation.effective_sample_size, int(evaluation.small_sample),
        int(evaluation.beats_all_baselines), evaluation.abstention_rate,
        json.dumps(evaluation.metrics), json.dumps(evaluation.notes), _iso(evaluation.evaluated_at),
    ))

    conn.execute("DELETE FROM model_baseline_comparisons WHERE evaluation_id = ?",
                 (evaluation.evaluation_id,))
    for comparison in evaluation.baseline_comparisons:
        conn.execute("""
            INSERT OR REPLACE INTO model_baseline_comparisons
            (evaluation_id, baseline_name, metric_name, baseline_score, model_score)
            VALUES (?,?,?,?,?)
        """, (evaluation.evaluation_id, comparison.baseline_name, comparison.metric_name,
              comparison.baseline_score, comparison.model_score))


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--label", default=DEFAULT_LABEL,
                        help=f"Label to model. Default {DEFAULT_LABEL}.")
    parser.add_argument("--train-fraction", type=float, default=TRAIN_FRACTION)
    parser.add_argument("--embargo-days", type=float, default=EMBARGO_DAYS)
    parser.add_argument("--apply", action="store_true",
                        help="Actually write. Without this the script is a dry run.")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"EROARE: baza nu exista: {args.db}")
        return 1

    conn = sqlite3.connect(args.db)
    initialize_model_schema(conn)

    feature_names, X, Y, cutoffs, cluster_ids, quality_counts = load_dataset(conn, args.label)

    print(f"Eticheta modelata      : {args.label}")
    print("Observatii pe calitate :")
    for level, count in quality_counts.most_common():
        print(f"  {level:12s} {count:>6,}")
    print(f"Randuri utilizabile (cu eticheta rezolvata): {len(X):,}")
    print(f"Caracteristici numerice: {len(feature_names)}")

    if len(X) < 10:
        print("\nPREA PUTINE DATE: sub 10 randuri utilizabile. Nu antrenez nimic.")
        conn.close()
        return 2

    train_idx, test_idx, embargoed = chronological_split(cutoffs, args.train_fraction, args.embargo_days)
    print()
    print(f"Impartire cronologica  : {len(train_idx):,} antrenare / {len(test_idx):,} testare")
    print(f"Sarite prin embargo    : {embargoed:,} (fereastra {args.embargo_days} zile)")
    print(f"Interval date          : {cutoffs[0].date()} -> {cutoffs[-1].date()} "
          f"({(cutoffs[-1] - cutoffs[0]).days} zile)")

    if not test_idx:
        print("\nSET DE TESTARE GOL dupa embargo. Nu pot evalua nimic onest.")
        conn.close()
        return 2

    X_train = [X[i] for i in train_idx]
    Y_train = [Y[i] for i in train_idx]
    X_test = [X[i] for i in test_idx]
    Y_test = [Y[i] for i in test_idx]
    train_clusters = len({cluster_ids[i] for i in train_idx})
    test_clusters = len({cluster_ids[i] for i in test_idx})
    print(f"Clustere (instrumente) : {train_clusters} antrenare / {test_clusters} testare")

    window = TrainingWindow(
        label="single_chronological_split",
        train_start=cutoffs[train_idx[0]], train_end=cutoffs[train_idx[-1]],
        test_start=cutoffs[test_idx[0]], test_end=cutoffs[test_idx[-1]],
        train_size=len(train_idx), test_size=len(test_idx),
        embargoed_count=embargoed,
    )

    specification = ModelSpecification(
        model_id="ridge_abnormal_return",
        name="Ridge regression on abnormal return",
        task=PredictionTask.ABNORMAL_RETURN,
        family=ModelFamily.RIDGE_REGRESSION,
        version="v1",
        label_name=args.label,
        feature_set_id="all_numeric_v1",
        feature_set_version="v1",
        dataset_version="v1",
        hyperparameters={"alpha": 1.0},
    )

    engine = ModelingEngine()
    model, evaluation = engine.train_and_evaluate(
        specification, X_train, Y_train, X_test, Y_test,
        feature_names=feature_names, window=window, cluster_count=test_clusters,
    )

    metric_name = primary_metric_name(specification.task)
    print()
    print("=== REZULTAT ===")
    print(f"Metrica principala ({metric_name}): {evaluation.metrics.get(metric_name)}")
    print(f"Toate metricile: {evaluation.metrics}")
    print(f"Rata de abtinere: {evaluation.abstention_rate}")
    print()
    print("Comparatii cu baseline-urile obligatorii:")
    for comparison in evaluation.baseline_comparisons:
        print(f"  {comparison.baseline_name:28s} baseline={comparison.baseline_score} "
              f"model={comparison.model_score}")
    print(f"Bate TOATE baseline-urile: {evaluation.beats_all_baselines}")
    print()
    print(f"Marime efectiva esantion: {evaluation.effective_sample_size} "
          f"(esantion mic: {evaluation.small_sample})")
    for note in model.training_notes:
        print(f"  NOTA MODEL: {note}")
    for note in evaluation.notes:
        print(f"  NOTA EVALUARE: {note}")

    print()
    print("INTERPRETARE: o singura impartire cronologica pe un interval de "
          f"{(cutoffs[-1] - cutoffs[0]).days} zile nu poate arata daca performanta "
          "e stabila intre regimuri de piata. Acest rezultat confirma ca lantul "
          "de date functioneaza cap-coada, nu ca piata e predictibila.")

    if not args.apply:
        print("\nDRY RUN — nimic nu a fost scris. Adaugati --apply pentru a scrie.")
        conn.close()
        return 0

    persist(conn, model, evaluation)
    conn.commit()
    conn.close()
    print(f"\nSCRIS: model {model.trained_model_id}, evaluare {evaluation.evaluation_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
