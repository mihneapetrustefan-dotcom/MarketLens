"""
src/challengers/evaluation.py
---------------------------------------
Phase 24 §11-§21, §36-§39 — running a fair comparison.

WHAT THIS ADDS OVER THE PHASE 22 EXPERIMENT THAT PRODUCED THE CANDIDATE
---------------------------------------------------------------------------
The experiment behind a candidate was one chronological split on one
window. A challenger evaluation is harder on purpose:

    walk-forward folds      does it hold as the window rolls forward
    time slices             does it hold in each sub-period
    instrument slices       or is it one instrument's history
    horizon slices          or one horizon's
    sensitivity             is the parameter a plateau or a spike
    complexity              what did the gain cost in moving parts
    economic significance   is the difference large enough to matter

WHAT IT DOES NOT REBUILD (§11, §73)
---------------------------------------
No second backtester, no second splitter, no second baseline registry,
no second bootstrap, no second multiple-testing rule. Phase 9 owns the
purged/embargoed splitter, Phase 12 owns the backtester, Phase 22 owns
the evaluators, baselines and interval, Phase 23 owns the governance
ledgers. This module orchestrates them and forms a verdict.

THE SLICES ARE THE POINT
----------------------------
`SliceResult` rows are kept individually and never averaged (§39). A
challenger that leads in three contexts and trails in three is
CONTEXT_DEPENDENT, which is a real finding about where the change
helps. Collapsing it into one mean would describe neither case and
would read as a modest global win.
"""

from __future__ import annotations

import json
import sqlite3
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.data_access.challenger_schema import initialize_challenger_schema
from src.domain.autoresearch_models import overfitting_warnings
from src.domain.challenger_models import (
    CHALLENGER_METHOD_VERSION, Challenger, ChallengerDecision,
    ChallengerLimits, ChallengerResult, ChallengerStatus, LimitExceeded,
    RunEnvironment, RunStatus, SliceResult, _digest, build_scorecard, decide,
    utcnow,
)
from src.domain.experiment_models import (
    ArmSpec, DatasetSnapshot, EvaluationProtocol, ResourceLimits,
    bootstrap_difference, economic_significance,
)


class EvaluationRefused(Exception):
    """The comparison cannot proceed. Always with a reason."""


def _run_id(challenger: Challenger, seed: int) -> str:
    return "chr-" + _digest({"c": challenger.challenger_id,
                             "v": challenger.version,
                             "f": challenger.fingerprint,
                             "s": seed, "t": utcnow()})[:20]


# ======================================================================
# Cohort
# ======================================================================

def load_cohort(conn: sqlite3.Connection, challenger: Challenger,
                *, limits: Optional[ChallengerLimits] = None
                ) -> List[Dict[str, Any]]:
    """
    The rows this comparison is allowed to see.

    Delegates to Phase 22's `load_cohort`, which applies Phase 21's
    `available_at` point-in-time key. A shim carries the dataset and
    the row cap because that function takes an experiment-shaped
    object; writing a second cohort loader here would be a second
    definition of what "the data" means.
    """
    from src.experiments import engine as experiment_engine

    limits = limits or ChallengerLimits()
    shim = SimpleNamespace(
        dataset=DatasetSnapshot(data_cutoff=challenger.dataset_cutoff),
        limits=ResourceLimits(max_rows=limits.max_rows))
    return experiment_engine.load_cohort(conn, shim)


def _arms(challenger: Challenger) -> Tuple[ArmSpec, ArmSpec]:
    baseline = ArmSpec(name=challenger.baseline.name,
                       evaluator=challenger.baseline.evaluator,
                       parameters=dict(challenger.baseline.parameters),
                       complexity=challenger.baseline.complexity)
    candidate = ArmSpec(name=challenger.name[:80],
                        evaluator=challenger.change.evaluator,
                        parameters=dict(challenger.change.parameters),
                        complexity=challenger.change.complexity)
    return baseline, candidate


def _metric(rows: Sequence[Dict[str, Any]], arm: ArmSpec, metric: str
            ) -> Tuple[Optional[float], int, Dict[str, Any]]:
    """One arm's metric on one slice, via Phase 22's evaluators."""
    from src.experiments import evaluators
    if not rows:
        return None, 0, {}
    try:
        measured, _kept = evaluators.evaluate(arm, list(rows))
    except evaluators.EvaluatorError:
        return None, 0, {}
    return measured.metric(metric), measured.sample_size, measured.as_dict()


def _series(rows: Sequence[Dict[str, Any]], arm: ArmSpec, metric: str
            ) -> List[float]:
    """
    Per-observation values for the bootstrap.

    Directional accuracy becomes a 0/1 series over decided outcomes;
    neutrals are excluded for the reason Phases 19 and 21 both gave — a
    market that did not move is not evidence for or against a
    directional claim.
    """
    from src.experiments import evaluators
    try:
        _measured, kept = evaluators.evaluate(arm, list(rows))
    except evaluators.EvaluatorError:
        return []
    if metric == "directional_accuracy":
        return [1.0 if row.get("direction_result") == "hit" else 0.0
                for row in kept
                if row.get("direction_result") in ("hit", "miss")]
    values = [row.get("actual_return") for row in kept]
    return [float(v) for v in values if v is not None]


# ======================================================================
# The evaluation
# ======================================================================

def evaluate(conn: sqlite3.Connection, challenger: Challenger, *,
             environment: RunEnvironment = RunEnvironment.RESEARCH,
             limits: Optional[ChallengerLimits] = None,
             allow_cache: bool = True,
             metric: str = "directional_accuracy"
             ) -> Tuple[Dict[str, Any], Optional[ChallengerResult]]:
    """
    Run one comparison. Returns `(run, result)`.

    `result` is None when the run failed — and the run still carries
    its reason, because a failed comparison that leaves no trace is one
    that will be run again by accident.
    """
    from src.autoresearch import governance
    from src.experiments import engine as experiment_engine

    initialize_challenger_schema(conn)
    limits = limits or ChallengerLimits()
    plan = challenger.plan
    started = time.time()
    seed = plan.seed

    # --- the definition may not have moved (§10) ------------------
    stored = conn.execute("""
        SELECT fingerprint, status FROM challengers
        WHERE challenger_id = ? AND version = ?
    """, (challenger.challenger_id, challenger.version)).fetchone()
    if stored and stored[0] != challenger.fingerprint:
        raise EvaluationRefused(
            "challenger %s v%d has changed since it was stored (%s -> %s). "
            "A comparison whose baseline or plan can move after the numbers "
            "exist is not a comparison; create a new version."
            % (challenger.challenger_id, challenger.version,
               stored[0][:12], challenger.fingerprint[:12]))

    run = {
        "run_id": _run_id(challenger, seed),
        "challenger_id": challenger.challenger_id,
        "challenger_version": challenger.version,
        "method_version": CHALLENGER_METHOD_VERSION,
        "environment": environment.value,
        "status": RunStatus.RUNNING.value,
        "seed": seed,
        "fingerprint": challenger.fingerprint,
        "dataset_cutoff": challenger.dataset_cutoff,
        "code_version": challenger.code_version,
        "rows_examined": 0, "cache_hit": 0, "cached_from_run": None,
        "error": "", "cancelled_reason": "",
        "queued_at": utcnow(), "started_at": utcnow(),
        "completed_at": None, "duration_seconds": None,
    }

    # --- reuse only on identical inputs (§56) ---------------------
    if allow_cache:
        cached = conn.execute("""
            SELECT r.run_id FROM challenger_runs r
            JOIN challenger_results x ON x.run_id = r.run_id
            WHERE r.fingerprint = ? AND r.seed = ? AND r.status = 'completed'
              AND r.cache_hit = 0 AND r.environment = ?
            ORDER BY r.completed_at DESC LIMIT 1
        """, (challenger.fingerprint, seed, environment.value)).fetchone()
        if cached:
            # Reuse is never silent. The fingerprint contains the
            # dataset cutoff, so a grown record cannot hit this path --
            # the Phase 23.5 defect, closed by construction.
            previous = load_result(conn, cached[0])
            if previous is not None:
                run.update({"status": RunStatus.COMPLETED.value,
                            "cache_hit": 1, "cached_from_run": cached[0],
                            "completed_at": utcnow(),
                            "duration_seconds": 0.0})
                previous.run_id = run["run_id"]
                previous.limitations.append(
                    "CACHED RESULT: reused from run %s, which ran the "
                    "identical definition on the identical record. Not a "
                    "fresh measurement." % cached[0])
                save_run(conn, run, previous)
                return run, previous

    try:
        rows = load_cohort(conn, challenger, limits=limits)
    except Exception as exc:
        run.update({"status": RunStatus.FAILED.value, "error": str(exc)[:400],
                    "completed_at": utcnow()})
        save_run(conn, run, None)
        return run, None

    run["rows_examined"] = len(rows)
    if not rows:
        run.update({"status": RunStatus.FAILED.value,
                    "error": "the cohort is empty; there is nothing to compare",
                    "completed_at": utcnow()})
        save_run(conn, run, None)
        return run, None

    baseline_arm, challenger_arm = _arms(challenger)
    train, test = experiment_engine.chronological_split(
        rows, plan.holdout_fraction)

    # --- protected test governance (§14, §21) ---------------------
    window_start = str(test[0].get("available_at") or "") if test else ""
    window_end = str(test[-1].get("available_at") or "") if test else ""
    try:
        governance.assert_window_allowed(conn, starts_at=window_start,
                                         ends_at=window_end)
    except governance.ProtectedWindowRefused as exc:
        run.update({"status": RunStatus.FAILED.value, "error": str(exc)[:400],
                    "completed_at": utcnow()})
        save_run(conn, run, None)
        return run, None

    # --- headline comparison --------------------------------------
    base_oos, base_n, base_metrics = _metric(test, baseline_arm, metric)
    cand_oos, cand_n, cand_metrics = _metric(test, challenger_arm, metric)
    base_is, _n, _m = _metric(train, baseline_arm, metric)
    cand_is, _n2, _m2 = _metric(train, challenger_arm, metric)

    effect = (None if base_oos is None or cand_oos is None
              else cand_oos - base_oos)
    effect_in_sample = (None if base_is is None or cand_is is None
                        else cand_is - base_is)

    low, high, _method = bootstrap_difference(
        _series(test, baseline_arm, metric),
        _series(test, challenger_arm, metric),
        iterations=plan.bootstrap_iterations, seed=seed)

    # --- walk-forward (§12) ---------------------------------------
    folds = folds_favourable = 0
    walk_forward_note = ""
    if plan.walk_forward:
        # A REAL Phase 22 protocol, not an invented shim.
        #
        # The first version passed a namespace carrying only
        # `walk_forward_folds` and `holdout_fraction`. Phase 9's
        # splitter needs `label_horizon_days`, `embargo_days`,
        # `train_months`, `test_months`, `step_months` and `expanding`,
        # so it raised AttributeError -- which a bare `except
        # Exception` turned into "0 folds". The comparison then
        # reported stability as unmeasured, and the gate let that pass.
        #
        # Two lessons the project has already paid for, together: build
        # the real object rather than a lookalike, and never let a
        # broad except convert a bug into an empty result.
        shim = SimpleNamespace(
            protocol=EvaluationProtocol(),
            dataset=DatasetSnapshot(data_cutoff=challenger.dataset_cutoff))
        slices = experiment_engine.walk_forward_slices(rows, shim)
        if not slices:
            walk_forward_note = (
                "the record spans %.0f days, which is shorter than one "
                "walk-forward window, so no fold could be generated"
                % _span_days(rows))
        for _label, _fold_train, fold_test in slices:
            base_value, _bn, _bm = _metric(fold_test, baseline_arm, metric)
            cand_value, _cn, _cm = _metric(fold_test, challenger_arm, metric)
            if base_value is None or cand_value is None:
                continue
            folds += 1
            if cand_value > base_value:
                folds_favourable += 1

    # --- contexts, preserved rather than averaged (§17, §39) ------
    slice_results: List[SliceResult] = []
    slice_results.extend(_time_slices(rows, baseline_arm, challenger_arm,
                                      metric, plan.robustness_slices))
    slice_results.extend(_grouped_slices(test, baseline_arm, challenger_arm,
                                         metric, "horizon"))
    slice_results.extend(_grouped_slices(test, baseline_arm, challenger_arm,
                                         metric, "instrument_id", limit=5))

    measured = [s for s in slice_results if s.effect is not None]
    robust_total = len(measured)
    robust_favourable = sum(1 for s in measured if s.effect > 0)

    # --- sensitivity (§18) ----------------------------------------
    sensitivity = _sensitivity(test, challenger, challenger_arm, metric,
                               base_oos, limits)

    # --- complexity and economics (§19, §20) ----------------------
    complexity_ratio = challenger.complexity_ratio
    economic_ok, economic_note = (None, "")
    if effect is not None:
        economic_ok, economic_note = economic_significance(effect, metric)

    # --- selection-bias context (§15, §22) ------------------------
    from src.challengers import registry
    family_count = registry.family_challenger_count(conn, challenger.family_id)
    run_count = conn.execute("""
        SELECT COUNT(*) FROM challenger_runs
        WHERE challenger_id = ? AND status = 'completed'
    """, (challenger.challenger_id,)).fetchone()[0] + 1
    reuse = governance.window_use_count(conn, window_start, window_end)

    instruments = int(cand_metrics.get("instrument_count") or 0)
    warnings = overfitting_warnings(
        effect=effect, effect_in_sample=effect_in_sample,
        sensitivity_shape=str(sensitivity.get("shape") or ""),
        instrument_count=instruments,
        regime_count=1,          # market_regime is NULL throughout
        variants=len(plan.sensitivity_values),
        window_reuse_count=reuse,
        span_days=_span_days(rows),
        slices_total=robust_total, slices_passing=robust_favourable)

    result = ChallengerResult(
        run_id=run["run_id"], challenger_id=challenger.challenger_id,
        challenger_version=challenger.version, metric=metric,
        baseline_out_of_sample=base_metrics,
        challenger_out_of_sample=cand_metrics,
        effect=effect, effect_in_sample=effect_in_sample,
        effect_low=low, effect_high=high,
        walk_forward_folds=folds, walk_forward_folds_favourable=folds_favourable,
        robust_slices=robust_total, robust_slices_favourable=robust_favourable,
        slices=slice_results, sensitivity=sensitivity,
        complexity_ratio=complexity_ratio,
        economically_significant=economic_ok, economic_note=economic_note,
        family_challenger_count=family_count, family_run_count=run_count,
        window_reuse_count=reuse, warnings=warnings)

    result.scorecard = build_scorecard(result, plan)
    verdict, reasons = decide(result, plan)
    result.decision = verdict
    result.reasons = reasons

    result.limitations.append(
        "the held-out half runs %s to %s; the challenger was never fitted "
        "or selected on it" % (window_start[:10], window_end[:10]))
    if walk_forward_note:
        result.limitations.append(walk_forward_note)
    if reuse >= 1:
        result.limitations.append(
            "this evaluation window has been tested %d time(s) before by "
            "other work; a result found on a repeatedly-used window deserves "
            "less weight than one found on a fresh one" % reuse)
    if family_count > 1:
        result.limitations.append(
            "%d challengers exist in this hypothesis family; with that many "
            "attempts one apparently good result is expected by chance"
            % family_count)
    if challenger.experimental_basis:
        result.limitations.append(
            "EXPERIMENTAL BASIS: no model has been promoted, so the baseline "
            "is a signal rule rather than a validated production model")
    for warning in warnings:
        result.limitations.append("overfitting shape present: " + warning)
    result.validate()

    governance.record_window_use(
        conn, starts_at=window_start, ends_at=window_end,
        hypothesis_id=challenger.hypothesis_id,
        experiment_id=challenger.experiment_id,
        family_id=challenger.family_id)

    run.update({"status": RunStatus.COMPLETED.value,
                "completed_at": utcnow(),
                "duration_seconds": round(time.time() - started, 3)})
    save_run(conn, run, result)
    return run, result


def _span_days(rows: Sequence[Dict[str, Any]]) -> float:
    from datetime import datetime
    stamps = [str(r.get("available_at") or "") for r in rows]
    stamps = [s for s in stamps if s]
    if len(stamps) < 2:
        return 0.0
    try:
        first = datetime.fromisoformat(min(stamps).replace("Z", "+00:00"))
        last = datetime.fromisoformat(max(stamps).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0.0
    return max((last - first).total_seconds() / 86400.0, 0.0)


def _time_slices(rows, baseline_arm, challenger_arm, metric, count
                 ) -> List[SliceResult]:
    from src.experiments import engine as experiment_engine
    try:
        pieces = experiment_engine.time_slices(rows, count=count)
    except Exception:
        return []
    # Phase 22 returns (label, rows) pairs and the label already names
    # the date range, which is more useful than "period 2 of 3".
    results = []
    for label, chunk in pieces:
        base_value, _bn, _bm = _metric(chunk, baseline_arm, metric)
        cand_value, cand_n, _cm = _metric(chunk, challenger_arm, metric)
        results.append(SliceResult(
            kind="period", label=label,
            baseline_metric=base_value, challenger_metric=cand_value,
            sample_size=cand_n))
    return results


def _grouped_slices(rows, baseline_arm, challenger_arm, metric, key,
                    limit: int = 6) -> List[SliceResult]:
    """
    One slice per value of a column — horizon, instrument, and so on.

    Groups below the minimum sample are skipped rather than reported
    with a number: a 4-observation instrument slice that happens to
    favour the challenger is noise, and including it would let a
    genuinely global result be labelled context-dependent by accident.
    """
    from src.domain.challenger_models import MIN_CHALLENGER_SAMPLE
    groups: Dict[Any, List[Dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row.get(key), []).append(row)
    ordered = sorted(groups.items(), key=lambda kv: -len(kv[1]))[:limit]
    results = []
    for value, members in ordered:
        if value in (None, "") or len(members) < MIN_CHALLENGER_SAMPLE:
            continue
        base_value, _bn, _bm = _metric(members, baseline_arm, metric)
        cand_value, cand_n, _cm = _metric(members, challenger_arm, metric)
        if cand_n < MIN_CHALLENGER_SAMPLE:
            continue
        results.append(SliceResult(
            kind=key, label=str(value), baseline_metric=base_value,
            challenger_metric=cand_value, sample_size=cand_n))
    return results


def _sensitivity(rows, challenger, challenger_arm, metric, baseline_metric,
                 limits) -> Dict[str, Any]:
    """
    Sweep the parameter's neighbours (§18).

    A plateau is the shape a real effect makes; a single point
    surrounded by failures is the shape overfitting makes. Only numeric
    parameters have neighbours, so a categorical cohort returns "not
    applicable" rather than a fabricated surface.
    """
    values = list(challenger.plan.sensitivity_values)
    if not values:
        return {"shape": "not_applicable",
                "note": ("this challenger changes no numeric parameter, so "
                         "it has no neighbouring values to sweep")}
    if len(values) > limits.max_variants:
        raise LimitExceeded(
            "%d sweep values exceed the %d-variant limit"
            % (len(values), limits.max_variants))

    numeric_key = None
    for key, value in sorted(challenger_arm.parameters.items()):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric_key = key
            break
    if numeric_key is None:
        return {"shape": "not_applicable", "note": "no numeric parameter"}

    surface = []
    for value in values:
        arm = ArmSpec(name=challenger_arm.name,
                      evaluator=challenger_arm.evaluator,
                      parameters={**challenger_arm.parameters,
                                  numeric_key: value},
                      complexity=challenger_arm.complexity)
        measured, sample, _m = _metric(rows, arm, metric)
        effect = (None if measured is None or baseline_metric is None
                  else measured - baseline_metric)
        surface.append({"value": value, "metric": measured,
                        "effect": effect, "sample_size": sample})

    positive = [point for point in surface
                if point["effect"] is not None and point["effect"] > 0]
    if not positive:
        shape, note = "no_effect", (
            "no value in the swept range beats the baseline. This is not a "
            "tuning problem: there is no setting at which this change helps.")
    elif len(positive) == 1:
        shape, note = "single_point", (
            "exactly one value beats the baseline and its neighbours do not. "
            "A single-point optimum is the shape overfitting makes.")
    else:
        shape, note = "plateau", (
            "%d of %d neighbouring values beat the baseline, which is the "
            "shape a real effect makes" % (len(positive), len(surface)))
    return {"parameter": numeric_key, "surface": surface, "shape": shape,
            "values_tested": len(surface),
            "values_clearing_baseline": len(positive), "note": note}


# ======================================================================
# Persistence
# ======================================================================

def save_run(conn: sqlite3.Connection, run: Dict[str, Any],
             result: Optional[ChallengerResult]) -> None:
    """Store a run and, if it produced one, its result."""
    initialize_challenger_schema(conn)
    conn.execute("""
        INSERT OR REPLACE INTO challenger_runs (
            run_id, challenger_id, challenger_version, method_version,
            environment, status, seed, fingerprint, dataset_cutoff,
            code_version, rows_examined, cache_hit, cached_from_run, error,
            cancelled_reason, queued_at, started_at, completed_at,
            duration_seconds
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (run["run_id"], run["challenger_id"], run["challenger_version"],
          run["method_version"], run["environment"], run["status"],
          run["seed"], run["fingerprint"], run["dataset_cutoff"],
          run["code_version"], run["rows_examined"], run["cache_hit"],
          run["cached_from_run"], run["error"], run["cancelled_reason"],
          run["queued_at"], run["started_at"], run["completed_at"],
          run["duration_seconds"]))

    if result is not None:
        conn.execute("""
            INSERT OR REPLACE INTO challenger_results (
                run_id, challenger_id, challenger_version, method_version,
                metric, baseline_oos_json, challenger_oos_json, effect,
                effect_in_sample, effect_low, effect_high,
                walk_forward_folds, walk_forward_favourable, robust_slices,
                robust_favourable, slices_json, sensitivity_json,
                complexity_ratio, economically_significant, economic_note,
                family_challenger_count, family_run_count,
                window_reuse_count, warnings_json, scorecard_json,
                decision, reasons_json, limitations_json, computed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (result.run_id, result.challenger_id, result.challenger_version,
              CHALLENGER_METHOD_VERSION, result.metric,
              json.dumps(result.baseline_out_of_sample, default=str),
              json.dumps(result.challenger_out_of_sample, default=str),
              result.effect, result.effect_in_sample, result.effect_low,
              result.effect_high, result.walk_forward_folds,
              result.walk_forward_folds_favourable, result.robust_slices,
              result.robust_slices_favourable,
              json.dumps([s.as_dict() for s in result.slices], default=str),
              json.dumps(result.sensitivity, default=str),
              result.complexity_ratio,
              None if result.economically_significant is None
              else int(result.economically_significant),
              result.economic_note, result.family_challenger_count,
              result.family_run_count, result.window_reuse_count,
              json.dumps(result.warnings),
              json.dumps(result.scorecard.as_dict() if result.scorecard else {}),
              result.decision.value, json.dumps(result.reasons),
              json.dumps(result.limitations), result.computed_at))
    conn.commit()


_RESULT_COLUMNS = (
    "run_id", "challenger_id", "challenger_version", "metric",
    "baseline_oos_json", "challenger_oos_json", "effect", "effect_in_sample",
    "effect_low", "effect_high", "walk_forward_folds",
    "walk_forward_favourable", "robust_slices", "robust_favourable",
    "slices_json", "sensitivity_json", "complexity_ratio",
    "economically_significant", "economic_note", "family_challenger_count",
    "family_run_count", "window_reuse_count", "warnings_json",
    "scorecard_json", "decision", "reasons_json", "limitations_json",
    "computed_at",
)


def load_result(conn: sqlite3.Connection, run_id: str
                ) -> Optional[ChallengerResult]:
    initialize_challenger_schema(conn)
    row = conn.execute(
        "SELECT %s FROM challenger_results WHERE run_id = ?"
        % ", ".join(_RESULT_COLUMNS), (run_id,)).fetchone()
    if row is None:
        return None
    record = dict(zip(_RESULT_COLUMNS, row))

    def load_json(key, default):
        try:
            return json.loads(record.get(key) or "null") or default
        except (TypeError, ValueError):
            return default

    result = ChallengerResult(
        run_id=record["run_id"], challenger_id=record["challenger_id"],
        challenger_version=record["challenger_version"],
        metric=record["metric"],
        baseline_out_of_sample=load_json("baseline_oos_json", {}),
        challenger_out_of_sample=load_json("challenger_oos_json", {}),
        effect=record["effect"], effect_in_sample=record["effect_in_sample"],
        effect_low=record["effect_low"], effect_high=record["effect_high"],
        walk_forward_folds=record["walk_forward_folds"],
        walk_forward_folds_favourable=record["walk_forward_favourable"],
        robust_slices=record["robust_slices"],
        robust_slices_favourable=record["robust_favourable"],
        slices=[SliceResult(kind=s.get("kind", ""), label=s.get("label", ""),
                            baseline_metric=s.get("baseline_metric"),
                            challenger_metric=s.get("challenger_metric"),
                            sample_size=s.get("sample_size", 0))
                for s in load_json("slices_json", [])],
        sensitivity=load_json("sensitivity_json", {}),
        complexity_ratio=record["complexity_ratio"],
        economically_significant=(
            None if record["economically_significant"] is None
            else bool(record["economically_significant"])),
        economic_note=record["economic_note"],
        family_challenger_count=record["family_challenger_count"],
        family_run_count=record["family_run_count"],
        window_reuse_count=record["window_reuse_count"],
        warnings=load_json("warnings_json", []),
        decision=ChallengerDecision(record["decision"]),
        reasons=load_json("reasons_json", []),
        limitations=load_json("limitations_json", []),
        computed_at=record["computed_at"])
    return result
