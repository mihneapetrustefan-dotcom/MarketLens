"""
src/modeling/selection.py
-------------------------------
Which model is allowed to produce production-facing predictions.

THE PROBLEM THIS EXISTS TO FIX (Phase 17.5, NEW-01)
-------------------------------------------------------
`inference.load_model()` selected with `ORDER BY trained_at DESC LIMIT
1`. Newest wins, and nothing else was consulted.

Measured against the production database on 2026-09-05, all four
trained models carried `beats_all_baselines = 0` and a negative
r-squared — worse than predicting the mean — and the newest of them
had a directional accuracy of 0.413, worse than a coin flip. That
model produced 422 of 549 predictions, which became signals, ten of
which were active on a public page.

Nothing was bypassed, because nothing was there to bypass.

THE GATE ALREADY EXISTED
----------------------------
`ModelEvaluation.is_deployable` was written in Phase 9 and reads:

    Requires: beats every baseline AND has a large enough effective
    sample. Returns None when it cannot be judged -- which is
    different from False, and both are different from True.

It is defined, documented, unit-tested, and **consulted by no
production code path in the repository**. So this module invents no
threshold. Phase 18's instruction is explicit -- *where the existing
evaluator already defines a quality threshold, reuse it* -- and the
right fix for a gate that nobody calls is to call it, not to write a
second one that can disagree with the first.

`ModelStatus` was likewise already complete (DRAFT, TRAINED,
EVALUATED, ACTIVE, DEGRADED, RETIRED) and `ACTIVE` appeared nowhere
outside a test that lists the enum members. Every model in production
sits at `evaluated`. So this module adds no state either. It gives the
existing states their meaning:

    EVALUATED  — measured. Usable for research, backtesting and
                 experiments. NOT production-facing.
    ACTIVE     — measured, passed the gate, and a human promoted it.
                 The only status inference will select by default.
    DEGRADED   — was ACTIVE; later evidence withdrew that.
    RETIRED    — superseded. Kept forever, because a prediction whose
                 model has vanished cannot be audited.

WHAT THIS MODULE WILL NOT DO
--------------------------------
It will not promote anything. `eligibility()` reports whether a model
COULD be promoted; turning that into ACTIVE requires a named human and
a stated reason, and lives in `scripts/promote_model.py`. Automatic
promotion is precisely the failure mode this file exists to prevent,
so the code that decides eligibility deliberately has no ability to
act on it.

There is no threshold parameter anywhere in this file. A caller cannot
lower the bar by passing an argument, and the only way to score with a
model that fails is to ask for `SelectionPolicy.EXPERIMENTAL`, which
is loud, explicit, and marks everything it produces.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.domain.model_models import (
    BaselineComparison, ModelEvaluation, ModelStatus,
)


class SelectionPolicy(str, Enum):
    """
    How much a caller is allowed to settle for.

    ACTIVE_ONLY is the default everywhere. EXPERIMENTAL exists because
    research must keep working on models that have not earned
    promotion -- Phase 18 §8 -- but it is never the default and never
    silent: everything it produces is marked.
    """
    #: Only a model a human promoted. Production-facing.
    ACTIVE_ONLY = "active_only"
    #: Any evaluated model, newest first. Research and experiments.
    EXPERIMENTAL = "experimental"


#: The state a caller gets instead of a bad model. Named as a constant
#: so the string in a log, a test and a report cannot drift apart.
NO_VALIDATED_MODEL_AVAILABLE = "NO_VALIDATED_MODEL_AVAILABLE"


class NoUsableModel(Exception):
    """
    Raised when no trained model can be applied.

    An exception rather than an empty result: a caller that silently
    scored nothing would look identical to one that scored everything
    and found no signal.

    Defined here rather than in `inference` so that `NoValidatedModel`
    can inherit from it without a circular import. `inference`
    re-exports it, so every existing `from src.modeling.inference
    import NoUsableModel` keeps working and keeps meaning the same
    thing.
    """


class NoValidatedModel(NoUsableModel):
    """
    Raised when nothing is promoted and the caller asked for production.

    A subclass, deliberately: "no model passed the gate" IS a case of
    "no model can be applied". Code that already handled the general
    failure keeps working, and code that wants to tell the two apart —
    "train one" versus "promote one" — still can.

    Carries the reason each candidate was rejected. A gate that says
    only "no" teaches nobody anything; the useful part is *which
    models exist and what each one failed*, so the operator can see
    whether the answer is "train more" or "promote the one that
    passed".
    """

    def __init__(self, message: str,
                 candidates: Optional[Sequence["ModelEligibility"]] = None):
        super().__init__(message)
        self.code = NO_VALIDATED_MODEL_AVAILABLE
        self.candidates: List[ModelEligibility] = list(candidates or ())

    def report(self) -> str:
        lines = [str(self), ""]
        if not self.candidates:
            lines.append("  No trained model matches the label at all.")
            return "\n".join(lines)
        lines.append("  Candidates considered, newest first:")
        for candidate in self.candidates:
            lines.append(f"    {candidate.trained_model_id}  "
                         f"[{candidate.status.value}]  {candidate.verdict}")
            for reason in candidate.reasons:
                lines.append(f"        - {reason}")
        return "\n".join(lines)


@dataclass
class ModelEligibility:
    """
    One model, judged. Everything a promotion decision needs, in the
    order a person would ask for it.

    `deployable` is `Optional[bool]` on purpose and mirrors
    `ModelEvaluation.is_deployable`: None means "cannot be judged"
    (no evaluation, or no baseline was run), which is not the same as
    False, and neither is the same as True. Collapsing the three into
    a boolean is how an unjudged model becomes a passing one.
    """
    trained_model_id: str
    model_qualified_id: str = ""
    label_name: str = ""
    status: ModelStatus = ModelStatus.TRAINED
    trained_at: str = ""

    evaluation_id: Optional[str] = None
    evaluated_at: Optional[str] = None
    sample_size: int = 0
    cluster_count: Optional[int] = None
    effective_sample_size: int = 0
    small_sample: bool = True
    beats_all_baselines: Optional[bool] = None
    metrics: Dict[str, float] = field(default_factory=dict)
    baseline_count: int = 0

    #: The existing gate's verdict, unmodified.
    deployable: Optional[bool] = None
    reasons: List[str] = field(default_factory=list)

    dataset_version: str = ""
    feature_set_version: str = ""
    label_version: str = ""

    @property
    def is_active(self) -> bool:
        return self.status == ModelStatus.ACTIVE

    @property
    def may_be_promoted(self) -> bool:
        """
        Whether a human is permitted to promote this model.

        Strictly `deployable is True`. An unjudgeable model is not
        promotable: "we could not measure it" is not evidence in
        favour.
        """
        return self.deployable is True

    @property
    def verdict(self) -> str:
        if self.status == ModelStatus.ACTIVE:
            return "ACTIVE"
        if self.status == ModelStatus.RETIRED:
            return "RETIRED"
        if self.status == ModelStatus.DEGRADED:
            return "DEGRADED"
        if self.deployable is True:
            return "ELIGIBLE (not promoted)"
        if self.deployable is False:
            return "FAILED"
        return "UNJUDGED"

    def summary(self) -> str:
        r2 = self.metrics.get("r_squared")
        acc = self.metrics.get("directional_accuracy")
        bits = [f"{self.trained_model_id}", f"[{self.verdict}]"]
        if r2 is not None:
            bits.append(f"r2={r2:+.3f}")
        if acc is not None:
            bits.append(f"dir={acc:.3f}")
        bits.append(f"eff_n={self.effective_sample_size}")
        return "  ".join(bits)


def _latest_evaluation(conn: sqlite3.Connection,
                       trained_model_id: str) -> Optional[Tuple[Any, ...]]:
    return conn.execute("""
        SELECT evaluation_id, model_qualified_id, window_label, sample_size,
               cluster_count, metrics_json, abstention_rate, evaluated_at
        FROM model_evaluations
        WHERE trained_model_id = ?
        ORDER BY evaluated_at DESC LIMIT 1
    """, (trained_model_id,)).fetchone()


def _baselines(conn: sqlite3.Connection,
               evaluation_id: str) -> List[BaselineComparison]:
    return [
        BaselineComparison(baseline_name=row[0], metric_name=row[1],
                           baseline_score=row[2], model_score=row[3])
        for row in conn.execute("""
            SELECT baseline_name, metric_name, baseline_score, model_score
            FROM model_baseline_comparisons WHERE evaluation_id = ?
        """, (evaluation_id,))
    ]


def eligibility(conn: sqlite3.Connection,
                trained_model_id: str) -> ModelEligibility:
    """
    Judge one model against the criteria the evaluator already defines.

    Reconstructs a real `ModelEvaluation` from the stored rows and asks
    it. The properties -- `effective_sample_size`, `small_sample`,
    `beats_all_baselines`, `is_deployable` -- are not reimplemented
    here, because a second implementation is a second answer waiting to
    disagree with the first.
    """
    row = conn.execute("""
        SELECT trained_model_id, model_qualified_id, label_name, label_version,
               status, trained_at, dataset_version, feature_set_version
        FROM trained_models WHERE trained_model_id = ?
    """, (trained_model_id,)).fetchone()
    if row is None:
        result = ModelEligibility(trained_model_id=trained_model_id)
        result.reasons.append("no such trained model")
        return result

    try:
        status = ModelStatus(row[4]) if row[4] else ModelStatus.TRAINED
    except ValueError:
        status = ModelStatus.TRAINED

    result = ModelEligibility(
        trained_model_id=row[0], model_qualified_id=row[1] or "",
        label_name=row[2] or "", label_version=row[3] or "",
        status=status, trained_at=row[5] or "",
        dataset_version=row[6] or "", feature_set_version=row[7] or "")

    evaluation_row = _latest_evaluation(conn, trained_model_id)
    if evaluation_row is None:
        result.reasons.append(
            "never evaluated — a model that has not been measured "
            "cannot be judged, and unjudged is not the same as passing")
        return result

    comparisons = _baselines(conn, evaluation_row[0])
    try:
        metrics = json.loads(evaluation_row[5] or "{}")
    except (TypeError, ValueError):
        metrics = {}

    evaluation = ModelEvaluation(
        evaluation_id=evaluation_row[0],
        trained_model_id=trained_model_id,
        model_qualified_id=evaluation_row[1] or "",
        window_label=evaluation_row[2] or "",
        sample_size=int(evaluation_row[3] or 0),
        cluster_count=evaluation_row[4],
        metrics={k: v for k, v in metrics.items() if isinstance(v, (int, float))},
        baseline_comparisons=comparisons,
        abstention_rate=evaluation_row[6])

    result.evaluation_id = evaluation.evaluation_id
    result.evaluated_at = evaluation_row[7] or ""
    result.sample_size = evaluation.sample_size
    result.cluster_count = evaluation.cluster_count
    result.effective_sample_size = evaluation.effective_sample_size
    result.small_sample = evaluation.small_sample
    result.beats_all_baselines = evaluation.beats_all_baselines
    result.metrics = dict(evaluation.metrics)
    result.baseline_count = len(comparisons)
    result.deployable = evaluation.is_deployable

    # The reasons are written from the same properties that produced
    # the verdict, so an explanation cannot describe a different
    # decision from the one that was taken.
    if not comparisons:
        result.reasons.append(
            "no baseline comparison was recorded — a metric without its "
            "baseline is close to meaningless")
    elif evaluation.beats_all_baselines is False:
        beaten = [c.baseline_name for c in comparisons if c.beats_baseline is False]
        result.reasons.append(
            f"does not beat {len(beaten)} of {len(comparisons)} baseline(s): "
            + ", ".join(sorted(beaten)))
    if evaluation.small_sample:
        result.reasons.append(
            f"effective sample {evaluation.effective_sample_size} is below "
            f"{ModelEvaluation.MIN_EFFECTIVE_SAMPLE} — descriptive only")
    if evaluation.is_deployable is True and status != ModelStatus.ACTIVE:
        result.reasons.append(
            "passes the gate but has not been promoted — promotion is a "
            "human decision (scripts/promote_model.py)")

    return result


def candidates(conn: sqlite3.Connection,
               label_name: Optional[str] = None) -> List[ModelEligibility]:
    """Every trained model for a label, newest first, each judged."""
    sql = ("SELECT trained_model_id FROM trained_models"
           + (" WHERE label_name = ?" if label_name else "")
           + " ORDER BY trained_at DESC")
    params = (label_name,) if label_name else ()
    return [eligibility(conn, row[0]) for row in conn.execute(sql, params)]


def select(conn: sqlite3.Connection,
           label_name: Optional[str] = None,
           policy: SelectionPolicy = SelectionPolicy.ACTIVE_ONLY,
           trained_model_id: Optional[str] = None) -> ModelEligibility:
    """
    Choose the model that may score observations.

    Deterministic in every branch: a pinned id, else the newest ACTIVE
    model, else -- only under EXPERIMENTAL -- the newest evaluated one.
    Ties are impossible because `trained_at` carries microseconds, and
    the ordering is total.

    Raises `NoValidatedModel` rather than falling back. Falling back is
    what the old code did, and the fallback was invisible.
    """
    if trained_model_id:
        pinned = eligibility(conn, trained_model_id)
        if not pinned.model_qualified_id and pinned.reasons:
            raise NoValidatedModel(
                f"No trained model {trained_model_id!r} exists.", [pinned])
        # A pinned id is an operator naming one model on purpose. It is
        # honoured under either policy and reported honestly by the
        # caller; refusing it would only push people to edit the
        # database by hand, which is worse.
        return pinned

    pool = candidates(conn, label_name)
    if not pool:
        raise NoValidatedModel(
            "No trained model exists"
            + (f" for label {label_name!r}. " if label_name else ". ")
            + "Run scripts/train_models.py first; inference applies a "
              "model, it does not fit one.", [])

    active = [c for c in pool if c.status == ModelStatus.ACTIVE]
    if active:
        return active[0]

    if policy == SelectionPolicy.ACTIVE_ONLY:
        promotable = [c for c in pool if c.may_be_promoted]
        hint = (
            f"\n  {len(promotable)} model(s) PASS the gate and are waiting "
            f"for promotion: " + ", ".join(c.trained_model_id for c in promotable)
            + "\n  Promote one with scripts/promote_model.py --approved-by ..."
            if promotable else
            "\n  No candidate passes the gate. This is not a configuration "
            "problem to be worked around — it is the evaluator reporting "
            "that no model has shown an edge.")
        raise NoValidatedModel(
            f"{NO_VALIDATED_MODEL_AVAILABLE}: no model is ACTIVE"
            + (f" for label {label_name!r}." if label_name else ".") + hint,
            pool)

    # EXPERIMENTAL: newest evaluated model, and the caller is
    # responsible for saying so out loud.
    return pool[0]
