"""
src/experiments/engine.py
---------------------------------
Running an experiment: split, evaluate, test robustness, decide.

    definition (frozen) -> dataset snapshot -> chronological split
        -> baseline arm + candidate arm, in and out of sample
        -> bootstrap interval -> robustness -> sensitivity
        -> decision against the PREDEFINED criteria

WHAT THIS ENGINE REUSES RATHER THAN REBUILDS
------------------------------------------------
Phase 9's `WalkForwardSplitter` — with its purge and embargo — for
walk-forward protocols (§40). Phase 12's `BacktestEngine` is the
executor for strategy experiments (§36); there is no second backtester
here and `strategy_backtest` delegates to it. Phase 19's outcomes and
Phase 21's experiences are the data; nothing is recomputed.

THE THREE RULES THE ENGINE ENFORCES
---------------------------------------
1. **The definition is frozen once started** (§32, §73). Every run
   records the fingerprint it executed, and a run against a definition
   whose fingerprint has changed is refused. Since the acceptance
   criteria are inside the fingerprint, success cannot be redefined
   after the answer is visible.

2. **The split is chronological, never random** (§39). A random split
   of financial observations leaks: two rows from the same day land on
   opposite sides and the test set learns the training set's answer.
   The candidate is selected on the training half and judged on the
   held-out half it never touched.

3. **Nothing production is touched** (§79). The engine reads
   experiences and writes experiment tables. It cannot promote a model,
   change a threshold, alter a strategy or move capital, and
   `tests/experiments/test_engine_and_safety.py` proves that by
   parsing this package's source.

WHY THE DECISION IS A LIST OF REASONS
----------------------------------------
Every criterion is checked and every check is recorded, whether it
passed or failed. A verdict with no reasoning is an assertion, and a
research result that cannot be argued with is not a research result.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.data_access.experiment_schema import initialize_experiment_schema
from src.domain.experiment_models import (
    EXPERIMENT_METHOD_VERSION, ArmMetrics, ArmSpec, Decision, Experiment,
    ExperimentResult, ExperimentRun, ExperimentStatus, RunStatus,
    bootstrap_difference, economic_significance, multiple_testing_note,
)
from src.experiments import evaluators


class ExperimentError(Exception):
    """The experiment cannot proceed. Always with a reason."""


class DefinitionChanged(ExperimentError):
    """
    The definition was edited after the experiment started (§32, §73).

    The most important error in this file. An experiment whose criteria
    can move after the result is visible is not an experiment.
    """


class ResourceLimitExceeded(ExperimentError):
    """A limit in §57 was breached. Failed with a reason, not killed."""


class Cancelled(ExperimentError):
    """Cancelled deliberately (§59). Partial results are preserved."""


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def current_data_cutoff(conn: sqlite3.Connection) -> str:
    """
    How far the experience record currently extends.

    THE DEFECT THIS EXISTS TO CLOSE
    -----------------------------------
    An experiment's dataset identity was purely DEFINITIONAL: `as_of`,
    filters, universe, versions. With `as_of` unset -- which is the
    normal case for "test this on everything we have" -- a dataset that
    GREW between two runs produced an identical fingerprint.

    The run cache keys on that fingerprint, so the second run was a
    cache hit and returned the first run's effect. Reproduced during
    the Phase 23.5 audit: 300 experiences gave +0.3333, 150 more
    experiences arrived, and the re-run reported +0.3333 as current
    research on 450 rows.

    That is the worst possible failure for this project specifically:
    the whole stated limitation of the current record is that it is
    short and more data would change the answer, and the cache was
    hiding exactly that.

    Stamping the cutoff makes a larger record a DIFFERENT dataset, so
    it becomes a different experiment rather than a stale answer -- and
    that is also what makes re-testing a depleted hypothesis on new
    evidence possible at all (Phase 23 §66).
    """
    try:
        row = conn.execute(
            "SELECT MAX(available_at) FROM trading_experiences "
            "WHERE available_at IS NOT NULL").fetchone()
    except sqlite3.OperationalError:
        return ""
    return str(row[0]) if row and row[0] else ""


def code_version() -> str:
    """The commit an experiment ran against (§9, §74). Best effort."""
    import subprocess
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


# ======================================================================
# Dataset: the cohort an experiment is allowed to see
# ======================================================================

_EXPERIENCE_COLUMNS = (
    "experience_id", "subject_kind", "subject_id", "horizon", "quality",
    "experience_class", "expected_direction", "expected_return",
    "actual_return", "direction_result", "mfe", "mae", "primary_error",
    "trained_model_id", "model_status", "strategy_id", "instrument_id",
    "asset_class", "sector_id", "event_type", "market_regime",
    "signal_confidence", "signal_strength", "available_at",
    "information_cutoff",
)


def load_cohort(conn: sqlite3.Connection, experiment: Experiment
                ) -> List[Dict[str, Any]]:
    """
    The dataset snapshot, as rows (§10, §11, §12).

    `as_of` is applied as `available_at <= as_of` — Phase 21's
    point-in-time key, which is when an experience became KNOWABLE
    rather than when it was written. That single clause is what stops
    an experiment anchored in the past from consulting its own future
    (§11, §12, §72).

    Incomplete experiences are excluded: an experience with no moment
    of becoming knowable cannot be placed on either side of a
    chronological split.
    """
    from src.data_access.memory_schema import initialize_memory_schema
    initialize_memory_schema(conn)

    snapshot = experiment.dataset
    clauses = ["quality IN ('validated','experimental')",
               "available_at IS NOT NULL"]
    params: List[Any] = []
    if snapshot.as_of:
        clauses.append("available_at <= ?")
        params.append(snapshot.as_of)
    if snapshot.filters.get("horizon"):
        clauses.append("horizon = ?")
        params.append(snapshot.filters["horizon"])
    if snapshot.filters.get("asset_class"):
        clauses.append("asset_class = ?")
        params.append(snapshot.filters["asset_class"])
    if snapshot.filters.get("subject_kind"):
        clauses.append("subject_kind = ?")
        params.append(snapshot.filters["subject_kind"])
    if snapshot.filters.get("quality"):
        clauses.append("quality = ?")
        params.append(snapshot.filters["quality"])

    sql = (f"SELECT {', '.join(_EXPERIENCE_COLUMNS)} FROM trading_experiences "
           f"WHERE {' AND '.join(clauses)} ORDER BY available_at, experience_id "
           f"LIMIT ?")
    params.append(experiment.limits.max_rows + 1)

    rows = [dict(zip(_EXPERIENCE_COLUMNS, row))
            for row in conn.execute(sql, params)]
    if len(rows) > experiment.limits.max_rows:
        raise ResourceLimitExceeded(
            f"the cohort exceeds max_rows={experiment.limits.max_rows:,}. "
            f"Narrow the universe or raise the limit deliberately; an "
            f"unbounded query is how one experiment starves the rest (§81).")
    return rows


def chronological_split(rows: Sequence[Dict[str, Any]],
                        holdout_fraction: float
                        ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Split by time, never at random (§38, §39).

    A random split of financial observations leaks: two rows from the
    same day land on opposite sides, and the test set learns the
    training set's answer without anyone doing anything wrong.

    Rows are already ordered by `available_at`, so the cut is a single
    index — and the boundary timestamp is returned to the caller in the
    limitations so a reader can see where the two halves meet.
    """
    if not rows:
        return [], []
    cut = int(len(rows) * (1.0 - holdout_fraction))
    cut = max(1, min(cut, len(rows) - 1)) if len(rows) > 1 else len(rows)
    return list(rows[:cut]), list(rows[cut:])


def walk_forward_slices(rows: Sequence[Dict[str, Any]],
                        experiment: Experiment) -> List[Tuple[str, List, List]]:
    """
    Train/test slices from Phase 9's splitter (§37, §40).

    The purge and embargo come from `WalkForwardSplitter`, which
    already knows how to keep a label horizon from bleeding across a
    boundary. Reimplementing that here would create a second answer to
    a question Phase 9 settled.

    Falls back to a single chronological holdout when the record is too
    short for even one window — which, at 27 days of history, it
    usually is. The fallback is reported rather than hidden.
    """
    from src.modeling.splits import WalkForwardSplitter

    moments = [_parse(r.get("available_at")) for r in rows]
    moments = [m for m in moments if m]
    if not moments:
        return []

    splitter = WalkForwardSplitter(
        label_horizon_days=experiment.protocol.label_horizon_days,
        embargo_days=experiment.protocol.embargo_days,
        train_months=experiment.protocol.train_months,
        test_months=experiment.protocol.test_months,
        step_months=experiment.protocol.step_months,
        expanding=experiment.protocol.expanding)

    try:
        windows = splitter.generate_windows(min(moments), max(moments))
    except (ValueError, TypeError):
        windows = []

    slices: List[Tuple[str, List, List]] = []
    for index, window in enumerate(windows):
        train = [r for r in rows
                 if window["train_start"] <= (_parse(r["available_at"]) or window["train_start"])
                 < window["train_end"]]
        test = [r for r in rows
                if window["train_end"] <= (_parse(r["available_at"]) or window["train_end"])
                < window.get("test_end", window["train_end"])]
        if train and test:
            slices.append((f"window-{index + 1}", train, test))
    return slices


def time_slices(rows: Sequence[Dict[str, Any]], count: int = 3
                ) -> List[Tuple[str, List[Dict[str, Any]]]]:
    """
    Equal-length chronological slices, for robustness (§50).

    An effect that only exists in one sub-period is not robust, and the
    only way to see that is to look at the sub-periods separately.
    """
    if not rows or count < 2:
        return []
    size = max(1, len(rows) // count)
    slices = []
    for index in range(count):
        start = index * size
        end = len(rows) if index == count - 1 else (index + 1) * size
        chunk = rows[start:end]
        if chunk:
            label = (f"{(chunk[0].get('available_at') or '')[:10]}"
                     f"..{(chunk[-1].get('available_at') or '')[:10]}")
            slices.append((label, chunk))
    return slices


# ======================================================================
# Running
# ======================================================================

def _effect(baseline: ArmMetrics, candidate: ArmMetrics,
            metric: str) -> Optional[float]:
    left, right = baseline.metric(metric), candidate.metric(metric)
    if left is None or right is None:
        return None
    return right - left


def _metric_series(rows: Sequence[Dict[str, Any]], metric: str) -> List[float]:
    """
    The per-observation series a bootstrap resamples.

    Directional accuracy becomes 1/0 per decided observation; a return
    metric becomes the returns themselves. Neutrals are dropped from
    the accuracy series for the same reason they are dropped from the
    rate.
    """
    if metric == "directional_accuracy":
        return [1.0 if r.get("direction_result") == "hit" else 0.0
                for r in rows if r.get("direction_result") in ("hit", "miss")]
    key = {"mean_return": "actual_return", "median_return": "actual_return",
           "mean_mfe": "mfe", "mean_mae": "mae"}.get(metric, "actual_return")
    return [r[key] for r in rows if r.get(key) is not None]


def run(conn: sqlite3.Connection, experiment: Experiment, *,
        seed: Optional[int] = None,
        environment: str = "local",
        allow_cache: bool = True,
        cancel_check: Optional[Any] = None,
        now: Optional[datetime] = None
        ) -> Tuple[ExperimentRun, Optional[ExperimentResult]]:
    """
    Execute one run of a frozen definition.

    Returns `(run, result)`. `result` is None when the run failed or was
    cancelled — and the run still carries its reason, because a failed
    experiment that leaves no trace is an experiment that will be run
    again by accident (§59, §60).
    """
    initialize_experiment_schema(conn)
    now = now or datetime.now(timezone.utc)
    experiment.validate()

    fingerprint = experiment.fingerprint
    stored = conn.execute(
        "SELECT fingerprint, status FROM experiments WHERE experiment_id = ?",
        (experiment.experiment_id,)).fetchone()
    if stored and stored[0] != fingerprint:
        status = ExperimentStatus(stored[1])
        if status.is_started:
            raise DefinitionChanged(
                f"Experiment {experiment.experiment_id} is {status.value} and "
                f"its definition has changed since it started "
                f"({stored[0][:12]} -> {fingerprint[:12]}). A definition that "
                f"can move after the answer is visible is not an experiment. "
                f"Create a new one instead.")

    run_record = ExperimentRun(
        run_id=f"run-{uuid.uuid4().hex[:20]}",
        experiment_id=experiment.experiment_id,
        status=RunStatus.RUNNING,
        seed=seed if seed is not None else experiment.protocol.random_seed,
        environment=environment,
        dataset_snapshot_id=experiment.dataset.snapshot_id,
        code_version=experiment.code_version or code_version(),
        fingerprint=fingerprint,
        started_at=now)

    # ---- cache (§61) -------------------------------------------------
    if allow_cache:
        cached = conn.execute("""
            SELECT r.run_id FROM experiment_runs r
            JOIN experiment_results x ON x.run_id = r.run_id
            WHERE r.fingerprint = ? AND r.seed = ? AND r.status = 'completed'
              AND r.cache_hit = 0
            ORDER BY r.completed_at DESC LIMIT 1
        """, (fingerprint, run_record.seed)).fetchone()
        if cached:
            # Reuse is NEVER silent. The run is recorded as a cache hit
            # naming the run it copied, so a reader can tell a fresh
            # result from a reused one.
            run_record.cache_hit = True
            run_record.cached_from_run = cached[0]
            run_record.status = RunStatus.COMPLETED
            run_record.completed_at = now
            run_record.duration_seconds = 0.0
            result = load_result(conn, cached[0])
            if result is not None:
                result.run_id = run_record.run_id
                result.limitations.append(
                    f"reused from run {cached[0]}: identical fingerprint and "
                    f"seed. Nothing was recomputed.")
            return run_record, result

    started = time.time()

    def check_limits():
        if cancel_check is not None and cancel_check():
            raise Cancelled("cancelled by request")
        elapsed = time.time() - started
        if elapsed > experiment.limits.max_runtime_seconds:
            raise ResourceLimitExceeded(
                f"exceeded max_runtime_seconds="
                f"{experiment.limits.max_runtime_seconds:g} after "
                f"{elapsed:.1f}s")

    try:
        rows = load_cohort(conn, experiment)
        run_record.rows_examined = len(rows)
        check_limits()

        if not rows:
            raise ExperimentError(
                "the dataset snapshot is empty. Either the as-of cut precedes "
                "every experience, or the filters exclude everything.")

        metric = experiment.hypothesis.metric
        train, test = chronological_split(rows,
                                          experiment.protocol.holdout_fraction)

        baseline_is, _ = evaluators.evaluate(experiment.baseline, train)
        candidate_is, _ = evaluators.evaluate(experiment.candidate, train)
        check_limits()
        baseline_oos, baseline_oos_rows = evaluators.evaluate(experiment.baseline, test)
        candidate_oos, candidate_oos_rows = evaluators.evaluate(experiment.candidate, test)
        check_limits()

        result = ExperimentResult(
            experiment_id=experiment.experiment_id, run_id=run_record.run_id,
            metric=metric,
            baseline_in_sample=baseline_is, candidate_in_sample=candidate_is,
            baseline_out_of_sample=baseline_oos,
            candidate_out_of_sample=candidate_oos)

        result.effect = _effect(baseline_oos, candidate_oos, metric)
        result.effect_in_sample = _effect(baseline_is, candidate_is, metric)

        iterations = min(experiment.protocol.bootstrap_iterations,
                         experiment.limits.max_bootstrap_iterations)
        low, high, method = bootstrap_difference(
            _metric_series(baseline_oos_rows, metric),
            _metric_series(candidate_oos_rows, metric),
            iterations=iterations, seed=run_record.seed)
        result.effect_low, result.effect_high = low, high
        result.interval_method = method
        check_limits()

        # ---- robustness across time slices (§50) --------------------
        slices = time_slices(rows, count=3)
        passing = 0
        details = {}
        for label, chunk in slices:
            b, _ = evaluators.evaluate(experiment.baseline, chunk)
            c, _ = evaluators.evaluate(experiment.candidate, chunk)
            slice_effect = _effect(b, c, metric)
            details[label] = {
                "baseline": b.metric(metric), "candidate": c.metric(metric),
                "effect": slice_effect, "sample": c.sample_size}
            if slice_effect is not None and slice_effect >= experiment.criteria.min_effect:
                passing += 1
        result.robust_slices = len(slices)
        result.robust_slices_passing = passing
        result.robustness = details
        check_limits()

        # ---- complexity (§49) ---------------------------------------
        base_complexity = max(experiment.baseline.complexity, 1)
        result.complexity_ratio = experiment.candidate.complexity / base_complexity

        significant, economic_note = economic_significance(result.effect, metric)
        result.economically_significant = significant

        # ---- selection bias (§41, §42) ------------------------------
        family_counts = family_statistics(conn, experiment)
        result.family_experiment_count = family_counts["experiments"]
        result.family_comparison_count = family_counts["comparisons"]

        decide(experiment, result, economic_note=economic_note,
               boundary=(test[0].get("available_at") if test else None))

        run_record.status = RunStatus.COMPLETED
        run_record.completed_at = datetime.now(timezone.utc)
        run_record.duration_seconds = time.time() - started
        return run_record, result

    except Cancelled as cancelled:
        run_record.status = RunStatus.CANCELLED
        run_record.cancelled_reason = str(cancelled)
        run_record.completed_at = datetime.now(timezone.utc)
        run_record.duration_seconds = time.time() - started
        return run_record, None
    except (ExperimentError, evaluators.EvaluatorError) as error:
        run_record.status = RunStatus.FAILED
        run_record.error = str(error)
        run_record.completed_at = datetime.now(timezone.utc)
        run_record.duration_seconds = time.time() - started
        return run_record, None


def decide(experiment: Experiment, result: ExperimentResult, *,
           economic_note: str = "", boundary: Optional[str] = None) -> None:
    """
    Apply the PREDEFINED criteria and record every check (§46, §47).

    PASS means the criteria were met. It does not mean profitable, and
    it does not mean deploy — §47 is explicit, and the reasons list
    says so in words so a reader skimming a verdict cannot miss it.

    INCONCLUSIVE is reached before FAIL whenever the evidence could not
    decide: too small a sample, or no measurable effect. Calling an
    unmeasurable experiment a failure would make "we could not tell"
    indistinguishable from "it did not work".
    """
    criteria = experiment.criteria
    reasons: List[str] = []
    limitations: List[str] = []

    sample = result.candidate_out_of_sample.sample_size
    decided = result.candidate_out_of_sample.decided

    if boundary:
        limitations.append(
            f"the out-of-sample half begins at {boundary[:19]}; the candidate "
            f"was never fitted or selected on it")

    if sample < criteria.min_sample:
        result.decision = Decision.INCONCLUSIVE
        reasons.append(
            f"INCONCLUSIVE: {sample} out-of-sample observation(s), under the "
            f"{criteria.min_sample} required. Too small to decide either way "
            f"— this is not a failure, it is an absence of evidence.")
        result.reasons = reasons
        result.limitations = limitations + [
            multiple_testing_note(result.family_experiment_count,
                                  result.family_comparison_count)]
        return

    if result.effect is None:
        result.decision = Decision.INCONCLUSIVE
        reasons.append(
            f"INCONCLUSIVE: the metric {result.metric!r} could not be "
            f"computed on one or both arms out of sample.")
        result.reasons = reasons
        result.limitations = limitations
        return

    checks: List[Tuple[bool, str]] = []

    met_effect = result.effect >= criteria.min_effect
    checks.append((met_effect,
                   f"out-of-sample effect {result.effect:+.4f} on "
                   f"{result.metric} against a required "
                   f"{criteria.min_effect:+.4f}"))

    if criteria.require_interval_excludes_zero:
        if result.effect_low is None:
            checks.append((False,
                           "no bootstrap interval could be computed (one arm "
                           "held fewer than 30 out-of-sample observations)"))
        else:
            excludes = result.effect_low > 0 or result.effect_high < 0
            checks.append((excludes,
                           f"95% bootstrap interval "
                           f"[{result.effect_low:+.4f}, "
                           f"{result.effect_high:+.4f}] "
                           + ("excludes zero" if excludes else "includes zero")))

    if result.robust_slices:
        fraction = result.robust_slices_passing / result.robust_slices
        checks.append((fraction >= criteria.min_robust_fraction,
                       f"the effect held in {result.robust_slices_passing} of "
                       f"{result.robust_slices} time slices "
                       f"({fraction:.0%}), against a required "
                       f"{criteria.min_robust_fraction:.0%}"))
    else:
        limitations.append(
            "too few observations to cut into time slices, so robustness "
            "across periods was not assessed")

    if result.complexity_ratio is not None:
        within = result.complexity_ratio <= criteria.max_complexity_ratio
        checks.append((within,
                       f"the candidate is {result.complexity_ratio:.1f}x as "
                       f"complex as the baseline, against a ceiling of "
                       f"{criteria.max_complexity_ratio:.1f}x"))

    if result.economically_significant is False:
        limitations.append(f"economic significance: {economic_note}")
    elif economic_note:
        limitations.append(economic_note)

    failed = [text for ok, text in checks if not ok]
    passed = [text for ok, text in checks if ok]

    if failed:
        result.decision = Decision.FAIL
        reasons.append("FAIL: " + "; ".join(failed))
        if passed:
            reasons.append("Met: " + "; ".join(passed))
    else:
        result.decision = Decision.PASS
        reasons.append("PASS: " + "; ".join(passed))
        reasons.append(
            "PASS means the criteria fixed before this ran were met. It does "
            "not mean profitable and it does not mean deploy — promotion is a "
            "separate, human decision.")

    gap = result.overfitting_gap
    if gap is not None and gap > 0.05:
        limitations.append(
            f"the in-sample effect exceeds the out-of-sample effect by "
            f"{gap:+.4f}, which is the signature of a candidate that fitted "
            f"its training half")

    limitations.append(multiple_testing_note(result.family_experiment_count,
                                             result.family_comparison_count))
    result.reasons = reasons
    result.limitations = limitations


def family_statistics(conn: sqlite3.Connection,
                      experiment: Experiment) -> Dict[str, int]:
    """
    How many siblings this hypothesis has (§41, §42, §43).

    A family of fifty will produce a winner by chance. The count travels
    with every decision so that a PASS is read alongside the number of
    attempts it took.
    """
    initialize_experiment_schema(conn)
    family = experiment.hypothesis.family_id
    if not family:
        return {"experiments": 1, "comparisons": 1}
    experiments = conn.execute(
        "SELECT COUNT(*) FROM experiments WHERE family_id = ?",
        (family,)).fetchone()[0] or 1
    comparisons = conn.execute("""
        SELECT COUNT(*) FROM experiment_results x
        JOIN experiments e ON e.experiment_id = x.experiment_id
        WHERE e.family_id = ?
    """, (family,)).fetchone()[0] or 0
    return {"experiments": max(experiments, 1),
            "comparisons": max(comparisons + 1, 1)}


# ======================================================================
# Sensitivity and ablation
# ======================================================================

def sensitivity(conn: sqlite3.Connection, experiment: Experiment,
                parameter: str, values: Sequence[Any]) -> Dict[str, Any]:
    """
    Sweep one parameter and look for a plateau (§51, §52).

    A candidate that only works at one value is a candidate fitted to
    the data. A stable plateau is the shape a real effect makes, and
    the difference is visible only when the neighbours are measured.

    Bounded by `max_variants` — a sweep is the easiest place to spend
    an afternoon of compute by accident.
    """
    if len(values) > experiment.limits.max_variants:
        raise ResourceLimitExceeded(
            f"{len(values)} variants exceeds max_variants="
            f"{experiment.limits.max_variants}")

    rows = load_cohort(conn, experiment)
    metric = experiment.hypothesis.metric
    baseline_metrics, _ = evaluators.evaluate(experiment.baseline, rows)
    surface = []
    for value in values:
        arm = ArmSpec(name=f"{parameter}={value}",
                      evaluator=experiment.candidate.evaluator,
                      parameters={**experiment.candidate.parameters,
                                  parameter: value},
                      complexity=experiment.candidate.complexity)
        try:
            metrics, _ = evaluators.evaluate(arm, rows)
        except evaluators.EvaluatorError as error:
            surface.append({"value": value, "error": str(error)})
            continue
        effect = _effect(baseline_metrics, metrics, metric)
        surface.append({"value": value, "sample_size": metrics.sample_size,
                        "metric": metrics.metric(metric), "effect": effect})

    effects = [point["effect"] for point in surface
               if point.get("effect") is not None]
    positive = [point for point in surface
                if (point.get("effect") or 0) >= experiment.criteria.min_effect]
    shape = "unknown"
    warning = ""
    if len(effects) >= 3:
        if not positive:
            # Distinct from a single-point optimum, and a more useful
            # answer: the parameter does not work anywhere in the range
            # tested, so there is nothing to overfit to and nothing to
            # tune. Reporting this as "single point" would imply an
            # optimum exists.
            shape = "no_effect"
            warning = (
                "no parameter value in the tested range clears the threshold. "
                "This is not a tuning problem: there is no setting of this "
                "parameter at which the candidate beats its baseline here.")
        elif len(positive) == 1:
            shape = "single_point"
            warning = (
                "exactly one parameter value clears the threshold and its "
                "neighbours do not. A single-point optimum surrounded by "
                "failures is the shape overfitting makes, not the shape an "
                "effect makes.")
        elif len(positive) >= max(2, len(effects) // 2):
            shape = "plateau"
            warning = ("the effect holds across neighbouring values, which is "
                       "the shape a real effect makes")
        else:
            shape = "narrow"
            warning = ("the effect holds over a narrow range; treat it as "
                       "provisional")
    return {"parameter": parameter, "surface": surface, "shape": shape,
            "values_clearing_threshold": len(positive),
            "values_tested": len(surface), "note": warning}


def ablation(conn: sqlite3.Connection, experiment: Experiment,
             components: Sequence[str]) -> Dict[str, Any]:
    """
    Remove one component at a time and measure what it was worth (§53).

    Each entry is the candidate WITHOUT that parameter, so a component
    whose removal costs nothing is a component the candidate does not
    need — which is the cheapest complexity reduction available.
    """
    if len(components) > experiment.limits.max_variants:
        raise ResourceLimitExceeded(
            f"{len(components)} ablations exceed max_variants="
            f"{experiment.limits.max_variants}")

    rows = load_cohort(conn, experiment)
    metric = experiment.hypothesis.metric
    full, _ = evaluators.evaluate(experiment.candidate, rows)
    full_value = full.metric(metric)

    results = {}
    for component in components:
        if component not in experiment.candidate.parameters:
            results[component] = {"error": "not a parameter of the candidate"}
            continue
        reduced = {k: v for k, v in experiment.candidate.parameters.items()
                   if k != component}
        arm = ArmSpec(name=f"without {component}",
                      evaluator=experiment.candidate.evaluator,
                      parameters=reduced,
                      complexity=max(1, experiment.candidate.complexity - 1))
        try:
            metrics, _ = evaluators.evaluate(arm, rows)
        except evaluators.EvaluatorError as error:
            results[component] = {"error": str(error)}
            continue
        value = metrics.metric(metric)
        contribution = (None if value is None or full_value is None
                        else full_value - value)
        results[component] = {
            "without_value": value, "sample_size": metrics.sample_size,
            "contribution": contribution,
            "note": ("removing it costs nothing measurable — the candidate "
                     "does not need it"
                     if contribution is not None and abs(contribution) < 0.005
                     else "")}
    return {"full_value": full_value, "metric": metric, "components": results}


# ======================================================================
# Persistence
# ======================================================================

def save_experiment(conn: sqlite3.Connection, experiment: Experiment) -> None:
    """
    Persist a definition.

    Refuses to overwrite a started experiment whose fingerprint has
    changed — the same guard `run()` applies, enforced at the write so
    that a definition cannot be edited even outside a run (§32, §73).
    """
    initialize_experiment_schema(conn)
    stored = conn.execute(
        "SELECT fingerprint, status FROM experiments WHERE experiment_id = ?",
        (experiment.experiment_id,)).fetchone()
    if stored and stored[0] != experiment.fingerprint:
        if ExperimentStatus(stored[1]).is_started:
            raise DefinitionChanged(
                f"Experiment {experiment.experiment_id} is {stored[1]} and its "
                f"definition has changed. Create a new experiment instead of "
                f"editing one whose result is already known.")

    hypothesis = experiment.hypothesis
    conn.execute("""
        INSERT OR REPLACE INTO experiments (
            experiment_id, method_version, name, experiment_type, status,
            description, created_by, family_id, statement, mechanism,
            expected_effect, population, conditions_json, metric,
            minimum_detectable_effect, hypothesis_source, source_reference,
            baseline_name, baseline_evaluator, baseline_params_json,
            baseline_complexity, baseline_description,
            candidate_name, candidate_evaluator,
            candidate_params_json, candidate_complexity, candidate_description,
            changed_variables_json,
            dataset_snapshot_id, dataset_json, dataset_version, feature_version,
            label_version, model_version, strategy_version,
            configuration_version, code_version, protocol_json, criteria_json,
            limits_json, fingerprint, notes_json, created_at, started_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                  ?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        experiment.experiment_id, experiment.method_version, experiment.name,
        experiment.experiment_type.value, experiment.status.value,
        experiment.description, experiment.created_by, hypothesis.family_id,
        hypothesis.statement, hypothesis.mechanism, hypothesis.expected_effect,
        hypothesis.population, json.dumps(hypothesis.conditions, default=str),
        hypothesis.metric, hypothesis.minimum_detectable_effect,
        hypothesis.source.value, hypothesis.source_reference,
        experiment.baseline.name, experiment.baseline.evaluator,
        json.dumps(experiment.baseline.parameters, sort_keys=True, default=str),
        experiment.baseline.complexity, experiment.baseline.description,
        experiment.candidate.name, experiment.candidate.evaluator,
        json.dumps(experiment.candidate.parameters, sort_keys=True, default=str),
        experiment.candidate.complexity, experiment.candidate.description,
        json.dumps(experiment.changed_variables),
        experiment.dataset.snapshot_id,
        json.dumps(experiment.dataset.as_dict(), sort_keys=True, default=str),
        experiment.dataset.dataset_version, experiment.dataset.feature_version,
        experiment.dataset.label_version, experiment.model_version,
        experiment.strategy_version, experiment.configuration_version,
        experiment.code_version or code_version(),
        json.dumps(experiment.protocol.as_dict(), sort_keys=True),
        json.dumps(experiment.criteria.as_dict(), sort_keys=True),
        json.dumps(experiment.limits.as_dict(), sort_keys=True),
        experiment.fingerprint, json.dumps(experiment.notes),
        experiment.created_at.isoformat(),
        datetime.now(timezone.utc).isoformat()
        if experiment.status.is_started else None))
    conn.commit()


def save_run(conn: sqlite3.Connection, run_record: ExperimentRun,
             result: Optional[ExperimentResult] = None) -> None:
    """Persist a run and, when there is one, its result."""
    initialize_experiment_schema(conn)

    def iso(value):
        return value.isoformat() if value else None

    conn.execute("""
        INSERT OR REPLACE INTO experiment_runs (
            run_id, experiment_id, status, seed, environment,
            dataset_snapshot_id, code_version, fingerprint, started_at,
            completed_at, duration_seconds, rows_examined, cache_hit,
            cached_from_run, error, cancelled_reason
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (run_record.run_id, run_record.experiment_id, run_record.status.value,
          run_record.seed, run_record.environment,
          run_record.dataset_snapshot_id, run_record.code_version,
          run_record.fingerprint, iso(run_record.started_at),
          iso(run_record.completed_at), run_record.duration_seconds,
          run_record.rows_examined, int(run_record.cache_hit),
          run_record.cached_from_run, run_record.error,
          run_record.cancelled_reason))

    if result is not None:
        conn.execute("""
            INSERT OR REPLACE INTO experiment_results (
                run_id, experiment_id, metric, baseline_is_json,
                candidate_is_json, baseline_oos_json, candidate_oos_json,
                effect, effect_in_sample, effect_low, effect_high,
                interval_method, robust_slices, robust_slices_passing,
                robustness_json, sensitivity_json, ablation_json,
                complexity_ratio, economically_significant,
                family_experiment_count, family_comparison_count, decision,
                reasons_json, limitations_json, computed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            result.run_id, result.experiment_id, result.metric,
            json.dumps(result.baseline_in_sample.as_dict()),
            json.dumps(result.candidate_in_sample.as_dict()),
            json.dumps(result.baseline_out_of_sample.as_dict()),
            json.dumps(result.candidate_out_of_sample.as_dict()),
            result.effect, result.effect_in_sample, result.effect_low,
            result.effect_high, result.interval_method, result.robust_slices,
            result.robust_slices_passing,
            json.dumps(result.robustness, default=str),
            json.dumps(result.sensitivity, default=str),
            json.dumps(result.ablation, default=str),
            result.complexity_ratio,
            None if result.economically_significant is None
            else int(result.economically_significant),
            result.family_experiment_count, result.family_comparison_count,
            result.decision.value, json.dumps(result.reasons),
            json.dumps(result.limitations),
            iso(result.computed_at)))
    conn.commit()


def load_result(conn: sqlite3.Connection,
                run_id: str) -> Optional[ExperimentResult]:
    """Rebuild a stored result, for cache reuse and for the API."""
    initialize_experiment_schema(conn)
    row = conn.execute("""
        SELECT experiment_id, metric, baseline_is_json, candidate_is_json,
               baseline_oos_json, candidate_oos_json, effect, effect_in_sample,
               effect_low, effect_high, interval_method, robust_slices,
               robust_slices_passing, robustness_json, sensitivity_json,
               ablation_json, complexity_ratio, economically_significant,
               family_experiment_count, family_comparison_count, decision,
               reasons_json, limitations_json
        FROM experiment_results WHERE run_id = ?
    """, (run_id,)).fetchone()
    if row is None:
        return None

    def metrics(raw: str) -> ArmMetrics:
        data = json.loads(raw or "{}")
        found = ArmMetrics()
        for key, value in data.items():
            if hasattr(found, key):
                setattr(found, key, value)
        return found

    return ExperimentResult(
        experiment_id=row[0], run_id=run_id, metric=row[1],
        baseline_in_sample=metrics(row[2]), candidate_in_sample=metrics(row[3]),
        baseline_out_of_sample=metrics(row[4]),
        candidate_out_of_sample=metrics(row[5]),
        effect=row[6], effect_in_sample=row[7], effect_low=row[8],
        effect_high=row[9], interval_method=row[10] or "",
        robust_slices=row[11] or 0, robust_slices_passing=row[12] or 0,
        robustness=json.loads(row[13] or "{}"),
        sensitivity=json.loads(row[14] or "{}"),
        ablation=json.loads(row[15] or "{}"),
        complexity_ratio=row[16],
        economically_significant=None if row[17] is None else bool(row[17]),
        family_experiment_count=row[18] or 1,
        family_comparison_count=row[19] or 1,
        decision=Decision(row[20]),
        reasons=json.loads(row[21] or "[]"),
        limitations=json.loads(row[22] or "[]"))


def set_status(conn: sqlite3.Connection, experiment_id: str,
               status: ExperimentStatus) -> None:
    initialize_experiment_schema(conn)
    conn.execute("UPDATE experiments SET status = ? WHERE experiment_id = ?",
                 (status.value, experiment_id))
    conn.commit()


def cancel(conn: sqlite3.Connection, experiment_id: str, reason: str) -> int:
    """
    Cancel queued or running runs, preserving what they had (§59).

    Partial results stay. An experiment cancelled halfway is evidence
    that something took too long, and deleting the record would lose
    that.
    """
    initialize_experiment_schema(conn)
    affected = conn.execute("""
        UPDATE experiment_runs SET status='cancelled', cancelled_reason=?,
               completed_at=?
        WHERE experiment_id = ? AND status IN ('queued','running')
    """, (reason, datetime.now(timezone.utc).isoformat(),
          experiment_id)).rowcount
    conn.execute("UPDATE experiments SET status='cancelled' "
                 "WHERE experiment_id = ? AND status NOT IN "
                 "('completed','passed','rejected','inconclusive')",
                 (experiment_id,))
    conn.commit()
    return affected
