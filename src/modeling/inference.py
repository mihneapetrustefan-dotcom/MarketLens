"""
src/modeling/inference.py
-------------------------------
Applying a trained model to observations that have no answer yet.

WHY THIS EXISTS
-------------------
Until now the only thing that ever wrote a `predictions` row was
`scripts/train_models.py`, and it predicts on the held-out TEST SLICE
of a chronological split. That is exactly right for measuring a model
and useless for using one: the test slice ends in the past by
construction, so every prediction in the database was, by design,
about something that had already happened.

Measured on 2026-09-04: the newest research observation had an
information cutoff of 2026-09-03, the newest PREDICTED one was
2026-08-26, and all 190 signals were suppressed as `stale_prediction`.
Thirteen observations from the previous week carried no prediction at
all. The pipeline could train and backtest; it could not score today.

THE ONE THING THIS GETS RIGHT OR ELSE
-----------------------------------------
Feature ordering. `train_models.load_dataset` builds its design matrix
from `sorted(numeric_names)` — whatever numeric features happened to
exist in the database at training time. A model's coefficients are
positional. Score it against a matrix built by re-deriving that sort
later, after a new feature has appeared, and every coefficient lands on
the wrong column. The model still returns numbers. They are noise, and
nothing downstream can tell.

So inference NEVER re-derives the feature list. It reads
`feature_names_json` off the trained model — the exact ordering fitted
against — and builds the matrix to that contract. A feature the model
was trained on but the observation lacks becomes None, which the
algorithm turns into an abstention rather than a zero.

WHAT IT REFUSES
-------------------
  - to score without a stored feature contract (a model persisted
    before `feature_names_json` existed cannot be applied safely)
  - to score when no observation can satisfy enough of that contract
  - to use a feature computed AFTER the observation's information
    cutoff, which would be look-ahead leakage in the one place the
    Phase 0 barrier does not reach, because these rows are read
    straight from the feature table

Labels are deliberately NOT required. `train_models.load_dataset`
skips observations whose label is unresolved, with a comment saying
they "cannot train or score" — true of training, false of scoring. The
absent label is the thing being predicted.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.domain.model_models import (
    ModelFamily, ModelSpecification, ModelStatus, PredictionTask,
    TrainedModel,
)
from src.modeling.engine import ModelingEngine


class NoUsableModel(Exception):
    """
    Raised when no trained model can be applied.

    An exception rather than an empty result: a caller that silently
    scored nothing would look identical to one that scored everything
    and found no signal.
    """


class FeatureContractBroken(Exception):
    """
    Raised when the stored feature ordering cannot be honoured.

    Never downgraded to a warning. A positional mismatch produces
    confident numbers from misaligned coefficients, and nothing
    downstream can detect it.
    """


@dataclass
class ScoringReport:
    """What one inference pass did, and what it declined to do."""
    model_qualified_id: str = ""
    trained_model_id: str = ""
    candidates: int = 0
    scored: int = 0
    abstained: int = 0
    skipped_already_scored: int = 0
    skipped_no_features: int = 0
    skipped_leaky_features: int = 0
    skipped_too_old: int = 0
    feature_coverage: Dict[str, int] = field(default_factory=dict)
    newest_cutoff: Optional[datetime] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model_qualified_id,
            "trained_model_id": self.trained_model_id,
            "candidates": self.candidates,
            "scored": self.scored,
            "abstained": self.abstained,
            "skipped": {
                "already_scored": self.skipped_already_scored,
                "no_features": self.skipped_no_features,
                "leaky_features": self.skipped_leaky_features,
                "too_old": self.skipped_too_old,
            },
            "newest_cutoff": (self.newest_cutoff.isoformat()
                              if self.newest_cutoff else None),
        }


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load_model(conn: sqlite3.Connection,
               trained_model_id: Optional[str] = None,
               label_name: Optional[str] = None) -> TrainedModel:
    """
    Rebuild a `TrainedModel` from the database.

    Without `trained_model_id`, the most recently trained model is
    used — and for a given label if one is named, so scoring for
    `d5.abnormal_return` cannot accidentally pick up a model fitted
    against a different target.
    """
    sql = """
        SELECT trained_model_id, model_id, model_qualified_id, name, task,
               family, model_version, label_name, label_version,
               feature_set_id, feature_set_version, dataset_version,
               hyperparameters_json, parameters_json, feature_names_json,
               train_start, train_end, train_sample_size, train_cluster_count,
               status, trained_at
        FROM trained_models
    """
    params: List[Any] = []
    where = []
    if trained_model_id:
        where.append("trained_model_id = ?")
        params.append(trained_model_id)
    if label_name:
        where.append("label_name = ?")
        params.append(label_name)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY trained_at DESC LIMIT 1"

    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise NoUsableModel(
            "No trained model matches. Run scripts/train_models.py first; "
            "inference applies a model, it does not fit one.")

    feature_names = json.loads(row[14] or "[]")
    if not feature_names:
        raise FeatureContractBroken(
            f"Trained model {row[0]} stores no feature_names. Its "
            f"coefficients are positional and there is no way to know "
            f"which column each belongs to, so it cannot be applied.")

    specification = ModelSpecification(
        model_id=row[1], name=row[3],
        task=PredictionTask(row[4]), family=ModelFamily(row[5]),
        version=row[6], label_name=row[7] or "", label_version=row[8] or "v1",
        feature_set_id=row[9], feature_set_version=row[10],
        dataset_version=row[11],
        hyperparameters=json.loads(row[12] or "{}"))

    return TrainedModel(
        trained_model_id=row[0],
        specification=specification,
        parameters=json.loads(row[13] or "{}"),
        feature_names=feature_names,
        train_start=_parse(row[15]), train_end=_parse(row[16]),
        train_sample_size=row[17] or 0, train_cluster_count=row[18],
        status=ModelStatus(row[19]) if row[19] else ModelStatus.TRAINED,
        trained_at=_parse(row[20]))


def candidates(conn: sqlite3.Connection, model: TrainedModel,
               max_age_days: Optional[float] = 30.0,
               now: Optional[datetime] = None,
               rescore: bool = False) -> List[Tuple[str, datetime]]:
    """
    Observations this model has not scored yet.

    `max_age_days` bounds how far back to reach. Scoring a two-month-old
    event produces a prediction the signal layer will immediately
    suppress as stale, so the default is to leave it alone rather than
    manufacture rows nothing can use. Pass None to score everything.
    """
    now = now or datetime.now(timezone.utc)
    sql = """
        SELECT o.observation_id, o.information_cutoff
        FROM research_observations o
        WHERE o.information_cutoff IS NOT NULL
          AND COALESCE(o.quality_level, '') != 'invalid'
    """
    params: List[Any] = []
    if not rescore:
        sql += """
          AND NOT EXISTS (SELECT 1 FROM predictions p
                          WHERE p.observation_id = o.observation_id
                            AND p.trained_model_id = ?)
        """
        params.append(model.trained_model_id)
    sql += " ORDER BY o.information_cutoff DESC"

    out: List[Tuple[str, datetime]] = []
    for observation_id, cutoff_str in conn.execute(sql, params):
        cutoff = _parse(cutoff_str)
        if cutoff is None:
            continue
        if max_age_days is not None:
            if (now - cutoff) > timedelta(days=max_age_days):
                continue
        out.append((observation_id, cutoff))
    return out


def build_matrix(conn: sqlite3.Connection, model: TrainedModel,
                 rows: Sequence[Tuple[str, datetime]],
                 report: ScoringReport,
                 min_feature_coverage: float = 0.5,
                 enforce_point_in_time: bool = True
                 ) -> Tuple[List[List[Optional[float]]], List[str],
                            List[datetime]]:
    """
    Build the design matrix TO THE MODEL'S OWN COLUMN ORDER.

    Not to the current sort of whatever is in `research_features`. That
    distinction is the whole safety property of this module — see the
    module docstring.
    """
    if not rows:
        return [], [], []

    observation_ids = [r[0] for r in rows]
    cutoffs = {r[0]: r[1] for r in rows}
    placeholders = ",".join("?" * len(observation_ids))

    by_observation: Dict[str, Dict[str, float]] = {}
    for observation_id, name, value_json, as_of in conn.execute(f"""
        SELECT observation_id, qualified_name, value_json, as_of
        FROM research_features WHERE observation_id IN ({placeholders})
    """, observation_ids):
        if name not in model.feature_names:
            continue          # not part of this model's contract
        try:
            value = json.loads(value_json) if value_json else None
        except (ValueError, TypeError):
            value = None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue          # categorical; excluded at training too

        if enforce_point_in_time:
            computed = _parse(as_of)
            cutoff = cutoffs.get(observation_id)
            if computed is not None and cutoff is not None and computed > cutoff:
                # A feature computed after the moment it claims to
                # describe. The Phase 0 barrier guards reads through
                # PointInTimeView; these rows are read straight from
                # the table, so the check is repeated here.
                by_observation.setdefault(observation_id, {})
                by_observation[observation_id]["__leak__"] = 1.0
                continue

        by_observation.setdefault(observation_id, {})[name] = float(value)

    required = len(model.feature_names)
    X: List[List[Optional[float]]] = []
    kept_ids: List[str] = []
    kept_cutoffs: List[datetime] = []

    for observation_id, cutoff in rows:
        values = by_observation.get(observation_id, {})
        if values.pop("__leak__", None) is not None:
            report.skipped_leaky_features += 1
            continue
        present = sum(1 for n in model.feature_names if n in values)
        if required and present / required < min_feature_coverage:
            report.skipped_no_features += 1
            continue
        for name in model.feature_names:
            if name in values:
                report.feature_coverage[name] = \
                    report.feature_coverage.get(name, 0) + 1
        X.append([values.get(name) for name in model.feature_names])
        kept_ids.append(observation_id)
        kept_cutoffs.append(cutoff)

    return X, kept_ids, kept_cutoffs


def score(conn: sqlite3.Connection,
          trained_model_id: Optional[str] = None,
          label_name: Optional[str] = None,
          max_age_days: Optional[float] = 30.0,
          min_feature_coverage: float = 0.5,
          now: Optional[datetime] = None,
          rescore: bool = False,
          engine: Optional[ModelingEngine] = None
          ) -> Tuple[List[Any], ScoringReport]:
    """
    Apply a trained model to everything it has not scored yet.

    Returns the predictions and a report of what was declined. Nothing
    is written — persistence is the caller's, so a dry run is the
    default shape rather than a flag threaded through.
    """
    model = load_model(conn, trained_model_id, label_name)
    report = ScoringReport(
        model_qualified_id=model.specification.qualified_id,
        trained_model_id=model.trained_model_id)

    rows = candidates(conn, model, max_age_days, now, rescore)
    report.candidates = len(rows)
    if rows:
        report.newest_cutoff = max(r[1] for r in rows)

    X, observation_ids, cutoffs = build_matrix(
        conn, model, rows, report, min_feature_coverage)

    if rows and not X:
        raise FeatureContractBroken(
            f"None of {len(rows)} candidate observation(s) carries at least "
            f"{min_feature_coverage:.0%} of the {len(model.feature_names)} "
            f"features {model.specification.qualified_id} was trained on. "
            f"Scoring anyway would apply positional coefficients to mostly "
            f"absent columns.")

    if not X:
        return [], report

    engine = engine or ModelingEngine()
    predictions = engine.predict(model, X, observation_ids, cutoffs)
    report.scored = len(predictions)
    report.abstained = sum(1 for p in predictions if p.is_abstention)
    return predictions, report


def save_predictions(conn: sqlite3.Connection,
                     predictions: Sequence[Any]) -> int:
    """
    Persist predictions so a Phase 10 signal can reference one by id.

    Lives here rather than in a script because there are now two
    producers -- `train_models.py` writing test-slice predictions and
    this module writing live ones -- and two copies of an INSERT is two
    definitions of what a prediction row is.

    Strictly this belongs in `data_access`, which has a schema module
    for models but no repository. Put here, beside its only callers,
    rather than in a new near-empty module; move it if a model
    repository ever grows.
    """
    for p in predictions:
        conn.execute("""
            INSERT OR REPLACE INTO predictions (
                prediction_id, trained_model_id, model_qualified_id, observation_id,
                predicted_value, predicted_class, class_probabilities_json, confidence,
                prediction_interval_low, prediction_interval_high, uncertainty_basis,
                information_cutoff, feature_set_version, predicted_at,
                is_abstention, abstention_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            p.prediction_id, p.trained_model_id, p.model_qualified_id,
            p.observation_id, p.predicted_value, p.predicted_class,
            json.dumps(p.class_probabilities) if p.class_probabilities else None,
            p.confidence, p.prediction_interval_low, p.prediction_interval_high,
            p.uncertainty_basis,
            p.information_cutoff.isoformat() if p.information_cutoff else None,
            p.feature_set_version,
            p.predicted_at.isoformat() if p.predicted_at else None,
            int(p.is_abstention), p.abstention_reason,
        ))
    conn.commit()
    return len(predictions)
