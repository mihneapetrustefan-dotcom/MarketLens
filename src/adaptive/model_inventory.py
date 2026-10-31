"""
src/adaptive/model_inventory.py
---------------------------------------------
Which models exist, and why they are not qualified (Phase 25.9I-F.5).

THE QUESTION THIS ANSWERS
-----------------------------
    WHY IS THERE NO MODEL FOR THIS HORIZON?

Read-only. It trains nothing, evaluates nothing, promotes nothing and
weakens no gate.

THE GATE IS REUSED, NOT REIMPLEMENTED
-----------------------------------------
Qualification is `ModelEvaluation.is_deployable` (Phase 10), which
requires a model to beat EVERY baseline and to rest on an effective
sample of at least `MIN_EFFECTIVE_SAMPLE` (30 clusters). This module
reconstructs the evaluation object from its stored row and asks that
property. It does not re-derive the rule, so the rule cannot drift into
a second, kinder copy living in the adaptive layer.

`is_deployable` returns THREE values and the distinction matters:

    True   earned further consideration
    False  failed a gate
    None   cannot be judged -- no baseline comparison exists

None is not False. A model nobody compared is not a model that lost.

WHAT THE CORPUS ACTUALLY CONTAINS
-------------------------------------
Measured 2026-09-30:

    trained_models       2 rows, but ONE specification: the same
                         `ridge_abnormal_return:v1` refitted twice
    status               `evaluated` for both -- never ACTIVE
    horizon              `d5.abnormal_return`, and only that
    evaluations          2, both on 5 test rows / 4 clusters
    r_squared            -0.197  (worse than predicting the mean)
    beats_all_baselines  FALSE -- it loses to the historical-mean and
                         majority-class baselines on MAE
    effective sample     4, against a floor of 30

So the single model fails TWO independent gates and is correctly
unpromoted. That is the honest reason the dashboard is empty, and this
module exists to say it in numbers rather than leave a blank cell.

PROMOTION IS NOT AVAILABLE HERE
-----------------------------------
There is no promote path in this module, by construction. The canonical
one is `src/modeling/promotion.py` and it requires the governance route.
The most this layer will ever say is CANDIDATE.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

from src.domain.model_models import (
    BaselineComparison, ModelEvaluation, ModelStatus,
)


class QualificationVerdict(str, Enum):
    """
    Three states, because `is_deployable` has three answers.

    UNJUDGED is not a polite word for FAILED: a model with no baseline
    comparison has not lost, it has not been measured, and the repair
    for each is different.
    """
    QUALIFIED = "qualified"
    FAILED = "failed"
    UNJUDGED = "unjudged"
    NO_EVALUATION = "no_evaluation"

    @property
    def is_qualified(self) -> bool:
        return self is QualificationVerdict.QUALIFIED


#: Gate failure codes, named for what a reader would have to fix.
GATE_BEATS_BASELINES = "BASELINE_NOT_BEATEN"
GATE_SAMPLE = "SAMPLE_TOO_SMALL"
GATE_NO_BASELINE = "NO_BASELINE_COMPARISON"
GATE_NOT_ACTIVE = "MODEL_NOT_ACTIVE"
GATE_NO_EVALUATION = "NEVER_EVALUATED"


@dataclass(frozen=True)
class ModelRecord:
    """One trained model, its evaluation and its gate verdict."""
    trained_model_id: str
    model_qualified_id: str
    name: str = ""
    family: str = ""
    task: str = ""
    label_name: str = ""
    horizon_id: str = ""
    status: str = ""
    feature_count: int = 0
    train_sample_size: int = 0
    train_cluster_count: Optional[int] = None
    trained_at: Optional[str] = None
    # evaluation
    evaluation_id: Optional[str] = None
    window_label: str = ""
    test_sample_size: int = 0
    cluster_count: Optional[int] = None
    effective_sample_size: Optional[int] = None
    small_sample: Optional[bool] = None
    beats_all_baselines: Optional[bool] = None
    metrics: Dict[str, float] = field(default_factory=dict)
    baselines_lost_to: Tuple[str, ...] = ()
    # verdict
    verdict: QualificationVerdict = QualificationVerdict.NO_EVALUATION
    gate_failures: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()

    @property
    def is_active(self) -> bool:
        return str(self.status).lower() == ModelStatus.ACTIVE.value

    @property
    def is_qualified(self) -> bool:
        """
        Qualified AND active. A model that passed its gates but was
        never promoted is not usable for a production decision, and
        saying otherwise would quietly bypass governance.
        """
        return self.verdict.is_qualified and self.is_active

    def describe(self) -> Dict[str, object]:
        return {
            "trained_model_id": self.trained_model_id,
            "model_qualified_id": self.model_qualified_id,
            "family": self.family,
            "task": self.task,
            "horizon_id": self.horizon_id,
            "status": self.status,
            "features": self.feature_count,
            "train_sample_size": self.train_sample_size,
            "train_cluster_count": self.train_cluster_count,
            "test_sample_size": self.test_sample_size,
            "effective_sample_size": self.effective_sample_size,
            "small_sample": self.small_sample,
            "beats_all_baselines": self.beats_all_baselines,
            "metrics": dict(self.metrics),
            "baselines_lost_to": list(self.baselines_lost_to),
            "verdict": self.verdict.value,
            "is_active": self.is_active,
            "is_qualified": self.is_qualified,
            "gate_failures": list(self.gate_failures),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class ModelInventory:
    """Every trained model, deterministically ordered."""
    models: Tuple[ModelRecord, ...] = ()
    built_at: Optional[datetime] = None

    def __iter__(self):
        return iter(self.models)

    def __len__(self) -> int:
        return len(self.models)

    @property
    def qualified(self) -> Tuple[ModelRecord, ...]:
        return tuple(m for m in self.models if m.is_qualified)

    @property
    def qualified_ids(self) -> Tuple[str, ...]:
        return tuple(sorted({m.model_qualified_id for m in self.qualified}))

    @property
    def specifications(self) -> Tuple[str, ...]:
        """
        Distinct model SPECIFICATIONS, not rows.

        Walk-forward training writes a new `trained_model_id` per run,
        so counting rows overstates how many independent models exist --
        the same mistake that once inflated signal confidence from 0.30
        to 0.50 by counting refits as corroboration.
        """
        return tuple(sorted({m.model_qualified_id for m in self.models}))

    def for_horizon(self, horizon_id: str) -> Tuple[ModelRecord, ...]:
        wanted = str(horizon_id).strip().lower()
        return tuple(m for m in self.models
                     if m.horizon_id.lower() == wanted)

    def gate_failure_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for model in self.models:
            for failure in model.gate_failures:
                counts[failure] = counts.get(failure, 0) + 1
        return dict(sorted(counts.items()))

    def describe(self) -> Dict[str, object]:
        by_horizon: Dict[str, int] = {}
        for model in self.models:
            by_horizon[model.horizon_id] = by_horizon.get(model.horizon_id, 0) + 1
        return {
            "built_at": self.built_at.isoformat() if self.built_at else None,
            "trained_rows": len(self.models),
            "distinct_specifications": len(self.specifications),
            "specifications": list(self.specifications),
            "evaluated": sum(1 for m in self.models if m.evaluation_id),
            "qualified": len(self.qualified),
            "promoted_active": sum(1 for m in self.models if m.is_active),
            "by_horizon": by_horizon,
            "gate_failures": self.gate_failure_counts(),
            "models": [m.describe() for m in self.models],
        }


def _loads(raw, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def build_model_inventory(conn: sqlite3.Connection, *,
                          as_of: Optional[datetime] = None) -> ModelInventory:
    """
    Read every trained model and ask the canonical gate about it.

    Deterministic order: horizon, then qualified id, then trained id, so
    two runs over the same corpus produce the same sequence.
    """
    try:
        rows = conn.execute(
            "SELECT trained_model_id, model_qualified_id, name, family, task, "
            "label_name, status, feature_names_json, train_sample_size, "
            "train_cluster_count, trained_at, training_notes_json "
            "FROM trained_models").fetchall()
    except sqlite3.DatabaseError:
        return ModelInventory(built_at=as_of)

    evaluations: Dict[str, Tuple] = {}
    try:
        for row in conn.execute(
                "SELECT trained_model_id, evaluation_id, model_qualified_id, "
                "window_label, sample_size, cluster_count, "
                "effective_sample_size, small_sample, beats_all_baselines, "
                "metrics_json, notes_json, abstention_rate "
                "FROM model_evaluations ORDER BY evaluated_at").fetchall():
            evaluations[str(row[0])] = row
    except sqlite3.DatabaseError:
        evaluations = {}

    comparisons: Dict[str, List[Tuple[str, str, float, float]]] = {}
    try:
        for evaluation_id, baseline_name, metric_name, baseline_score, \
                model_score in conn.execute(
                    "SELECT evaluation_id, baseline_name, metric_name, "
                    "baseline_score, model_score "
                    "FROM model_baseline_comparisons").fetchall():
            comparisons.setdefault(str(evaluation_id), []).append(
                (str(baseline_name), str(metric_name), baseline_score,
                 model_score))
    except sqlite3.DatabaseError:
        comparisons = {}

    records: List[ModelRecord] = []
    for row in rows:
        (trained_model_id, model_qualified_id, name, family, task, label_name,
         status, feature_names_json, train_sample_size, train_cluster_count,
         trained_at, training_notes_json) = row

        horizon = str(label_name or "").split(".")[0]
        features = _loads(feature_names_json, [])
        notes = list(_loads(training_notes_json, []))

        evaluation_row = evaluations.get(str(trained_model_id))
        gate_failures: List[str] = []
        verdict = QualificationVerdict.NO_EVALUATION
        metrics: Dict[str, float] = {}
        lost_to: List[str] = []
        evaluation_id = window_label = None
        test_n = 0
        clusters = effective = None
        small = beats = None

        if evaluation_row is None:
            gate_failures.append(GATE_NO_EVALUATION)
        else:
            (_, evaluation_id, eval_qualified_id, window_label, test_n,
             clusters, effective, small, beats, metrics_json, notes_json,
             abstention_rate) = evaluation_row
            metrics = dict(_loads(metrics_json, {}))
            notes.extend(_loads(notes_json, []))

            # Reconstruct the canonical evaluation and ASK IT. The rule
            # lives in `ModelEvaluation.is_deployable` and is not copied.
            baseline_objects = [
                BaselineComparison(baseline_name=name, metric_name=metric,
                                   baseline_score=baseline_score,
                                   model_score=model_score)
                for name, metric, baseline_score, model_score
                in comparisons.get(str(evaluation_id), [])]
            evaluation = ModelEvaluation(
                evaluation_id=str(evaluation_id),
                trained_model_id=str(trained_model_id),
                model_qualified_id=str(eval_qualified_id or model_qualified_id),
                window_label=str(window_label or ""),
                sample_size=int(test_n or 0),
                cluster_count=clusters,
                metrics=metrics,
                baseline_comparisons=baseline_objects,
                abstention_rate=abstention_rate)
            deployable = evaluation.is_deployable

            if deployable is None:
                verdict = QualificationVerdict.UNJUDGED
                gate_failures.append(GATE_NO_BASELINE)
            elif deployable:
                verdict = QualificationVerdict.QUALIFIED
            else:
                verdict = QualificationVerdict.FAILED
                if evaluation.small_sample:
                    gate_failures.append(GATE_SAMPLE)
                if evaluation.beats_all_baselines is False:
                    gate_failures.append(GATE_BEATS_BASELINES)

            for baseline in baseline_objects:
                if baseline.beats_baseline is False:
                    lost_to.append(baseline.baseline_name)

        if str(status).lower() != ModelStatus.ACTIVE.value:
            gate_failures.append(GATE_NOT_ACTIVE)

        records.append(ModelRecord(
            trained_model_id=str(trained_model_id),
            model_qualified_id=str(model_qualified_id or ""),
            name=str(name or ""), family=str(family or ""), task=str(task or ""),
            label_name=str(label_name or ""), horizon_id=horizon,
            status=str(status or ""), feature_count=len(features),
            train_sample_size=int(train_sample_size or 0),
            train_cluster_count=train_cluster_count,
            trained_at=str(trained_at) if trained_at else None,
            evaluation_id=str(evaluation_id) if evaluation_id else None,
            window_label=str(window_label or ""),
            test_sample_size=int(test_n or 0), cluster_count=clusters,
            effective_sample_size=effective,
            small_sample=bool(small) if small is not None else None,
            beats_all_baselines=(bool(beats) if beats is not None else None),
            metrics=metrics, baselines_lost_to=tuple(sorted(set(lost_to))),
            verdict=verdict, gate_failures=tuple(dict.fromkeys(gate_failures)),
            notes=tuple(notes)))

    records.sort(key=lambda m: (m.horizon_id, m.model_qualified_id,
                                m.trained_model_id))
    return ModelInventory(models=tuple(records), built_at=as_of)
