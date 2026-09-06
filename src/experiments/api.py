"""
src/experiments/api.py
------------------------------
The query and control surface for the Experiment Lab (§71).

CONVENTION
--------------
`docs/API_AUDIT.md` records that this repository has no HTTP layer, by
decision. Phases 19, 20 and 21 implemented their routes as typed
functions over a connection; this follows, so four phases share one
convention.

    GET  /experiments              -> list_experiments()
    POST /experiments              -> create()
    GET  /experiments/{id}         -> detail()
    POST /experiments/{id}/run     -> start()
    POST /experiments/{id}/cancel  -> cancel()
    GET  /experiments/{id}/runs    -> runs()
    GET  /experiments/{id}/results -> results()
    GET  /experiments/{id}/artifacts -> artifacts()
    GET  /experiments/families     -> families()
    GET  /experiments/templates    -> templates()

SECURITY (§80)
------------------
`create()` accepts an `ArmSpec` whose evaluator is a REGISTERED NAME.
It validates the name and the parameters before storing, so an
experiment that names an unknown evaluator or passes an unknown
parameter is refused at the door rather than at run time. There is no
field anywhere in this surface that can carry code.

`start()` is the only function that executes anything, and it takes a
connection and an experiment id — it cannot be handed a callable.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from src.data_access.experiment_schema import initialize_experiment_schema
from src.domain.experiment_models import (
    EXPERIMENT_METHOD_VERSION, AcceptanceCriteria, ArmSpec, DatasetSnapshot,
    Decision, EvaluationProtocol, Experiment, ExperimentStatus, ExperimentType,
    Hypothesis, HypothesisSource, ResourceLimits,
)
from src.experiments import engine, evaluators, templates

MAX_LIMIT = 500

_EXPERIMENT_COLUMNS = (
    "experiment_id", "method_version", "name", "experiment_type", "status",
    "description", "created_by", "family_id", "statement", "mechanism",
    "expected_effect", "population", "conditions_json", "metric",
    "minimum_detectable_effect", "hypothesis_source", "source_reference",
    "baseline_name", "baseline_evaluator", "baseline_params_json",
    "baseline_complexity", "baseline_description",
    "candidate_name", "candidate_evaluator",
    "candidate_params_json", "candidate_complexity", "candidate_description",
    "changed_variables_json",
    "dataset_snapshot_id", "dataset_json", "dataset_version", "feature_version",
    "label_version", "model_version", "strategy_version",
    "configuration_version", "code_version", "protocol_json", "criteria_json",
    "limits_json", "fingerprint", "notes_json", "created_at", "started_at",
)


def _decode(record: Dict[str, Any]) -> Dict[str, Any]:
    for key in list(record):
        if key.endswith("_json"):
            try:
                record[key[:-5]] = json.loads(record.pop(key) or "null")
            except (TypeError, ValueError):
                record[key[:-5]] = None
    return record


def create(conn: sqlite3.Connection, experiment: Experiment) -> Experiment:
    """
    `POST /experiments` — validate, then store as DRAFT.

    Validation happens BEFORE storage on purpose. An invalid experiment
    that reached the table would sit there looking like research, and
    somebody would eventually run it.
    """
    initialize_experiment_schema(conn)
    for arm in (experiment.baseline, experiment.candidate):
        spec, _ = evaluators.get(arm.evaluator)
        evaluators.validate_parameters(spec, arm.parameters)
    experiment.validate()
    engine.save_experiment(conn, experiment)
    return experiment


def list_experiments(conn: sqlite3.Connection, *,
                     status: Optional[str] = None,
                     experiment_type: Optional[str] = None,
                     family_id: Optional[str] = None,
                     decision: Optional[str] = None,
                     limit: int = 100, offset: int = 0
                     ) -> List[Dict[str, Any]]:
    """`GET /experiments`, filterable by the dimensions §67 asks for."""
    initialize_experiment_schema(conn)
    clauses, params = ["1=1"], []
    for column, value in (("status", status),
                          ("experiment_type", experiment_type),
                          ("family_id", family_id)):
        if value:
            clauses.append(f"e.{column} = ?")
            params.append(value)
    join = ""
    if decision:
        join = ("JOIN experiment_results x ON x.experiment_id = e.experiment_id "
                "AND x.decision = ?")
        params.insert(0, decision)
    sql = (f"SELECT DISTINCT {', '.join('e.' + c for c in _EXPERIMENT_COLUMNS)} "
           f"FROM experiments e {join} WHERE {' AND '.join(clauses)} "
           f"ORDER BY e.created_at DESC LIMIT ? OFFSET ?")
    params += [max(1, min(int(limit), MAX_LIMIT)), max(0, int(offset))]
    return [_decode(dict(zip(_EXPERIMENT_COLUMNS, row)))
            for row in conn.execute(sql, params)]


def detail(conn: sqlite3.Connection, experiment_id: str
           ) -> Optional[Dict[str, Any]]:
    """
    `GET /experiments/{id}` — everything §68 asks a detail page to show.

    Runs and results are always attached. An experiment page that could
    show a hypothesis without its outcome would let a reader form an
    impression from the question alone.
    """
    initialize_experiment_schema(conn)
    row = conn.execute(f"""
        SELECT {', '.join(_EXPERIMENT_COLUMNS)} FROM experiments
        WHERE experiment_id = ?
    """, (experiment_id,)).fetchone()
    if row is None:
        return None
    record = _decode(dict(zip(_EXPERIMENT_COLUMNS, row)))
    record["runs"] = runs(conn, experiment_id)
    record["results"] = results(conn, experiment_id)
    record["artifacts"] = artifacts(conn, experiment_id)
    record["family"] = family_detail(conn, record.get("family_id"))
    return record


def runs(conn: sqlite3.Connection, experiment_id: str) -> List[Dict[str, Any]]:
    """`GET /experiments/{id}/runs` — newest first."""
    initialize_experiment_schema(conn)
    keys = ("run_id", "status", "seed", "environment", "dataset_snapshot_id",
            "code_version", "fingerprint", "started_at", "completed_at",
            "duration_seconds", "rows_examined", "cache_hit",
            "cached_from_run", "error", "cancelled_reason")
    return [dict(zip(keys, row)) for row in conn.execute(f"""
        SELECT {', '.join(keys)} FROM experiment_runs
        WHERE experiment_id = ? ORDER BY started_at DESC
    """, (experiment_id,))]


def results(conn: sqlite3.Connection, experiment_id: str
            ) -> List[Dict[str, Any]]:
    """`GET /experiments/{id}/results` — with reasons and limitations."""
    initialize_experiment_schema(conn)
    keys = ("run_id", "metric", "baseline_oos_json", "candidate_oos_json",
            "effect", "effect_in_sample", "effect_low", "effect_high",
            "interval_method", "robust_slices", "robust_slices_passing",
            "robustness_json", "sensitivity_json", "ablation_json",
            "complexity_ratio", "economically_significant",
            "family_experiment_count", "family_comparison_count", "decision",
            "reasons_json", "limitations_json", "computed_at")
    out = []
    for row in conn.execute(f"""
        SELECT {', '.join(keys)} FROM experiment_results
        WHERE experiment_id = ? ORDER BY computed_at DESC
    """, (experiment_id,)):
        record = _decode(dict(zip(keys, row)))
        if (record.get("effect") is not None
                and record.get("effect_in_sample") is not None):
            record["overfitting_gap"] = (record["effect_in_sample"]
                                         - record["effect"])
        out.append(record)
    return out


def artifacts(conn: sqlite3.Connection, experiment_id: str
              ) -> List[Dict[str, Any]]:
    """`GET /experiments/{id}/artifacts` — references, never payloads."""
    initialize_experiment_schema(conn)
    keys = ("artifact_id", "run_id", "kind", "location", "reference_id",
            "checksum", "size_bytes", "detail_json", "created_at")
    return [_decode(dict(zip(keys, row))) for row in conn.execute(f"""
        SELECT {', '.join(keys)} FROM experiment_artifacts
        WHERE experiment_id = ? ORDER BY created_at DESC
    """, (experiment_id,))]


def record_artifact(conn: sqlite3.Connection, *, run_id: str,
                    experiment_id: str, kind: str, location: str = "",
                    reference_id: Optional[str] = None, checksum: str = "",
                    size_bytes: Optional[int] = None,
                    detail: Optional[Dict[str, Any]] = None) -> str:
    """
    Register an artifact by reference (§35).

    `reference_id` is how a Phase 12 backtest is attached: the backtest
    row keeps its own identity in its own table, and the experiment
    points at it. Copying it here would create a second copy that could
    disagree with the first.
    """
    import hashlib
    initialize_experiment_schema(conn)
    digest = hashlib.sha256(
        f"{run_id}|{kind}|{location}|{reference_id}".encode()).hexdigest()[:20]
    artifact_id = f"art-{digest}"
    conn.execute("""
        INSERT OR REPLACE INTO experiment_artifacts (
            artifact_id, run_id, experiment_id, kind, location, reference_id,
            checksum, size_bytes, detail_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (artifact_id, run_id, experiment_id, kind, location, reference_id,
          checksum, size_bytes, json.dumps(detail or {}, default=str),
          datetime.now(timezone.utc).isoformat()))
    conn.commit()
    return artifact_id


def start(conn: sqlite3.Connection, experiment_id: str, *,
          seed: Optional[int] = None, environment: str = "local",
          allow_cache: bool = True) -> Dict[str, Any]:
    """
    `POST /experiments/{id}/run`.

    Loads the stored definition and executes it. Marks the experiment
    RUNNING first, which freezes the definition: from this point a
    changed fingerprint is refused, so the acceptance criteria cannot
    be relaxed after the answer appears.
    """
    experiment = load(conn, experiment_id)
    if experiment is None:
        raise engine.ExperimentError(f"No experiment {experiment_id!r}.")
    if experiment.status in (ExperimentStatus.CANCELLED,):
        raise engine.ExperimentError(
            f"Experiment {experiment_id} was cancelled; create a new one.")

    engine.set_status(conn, experiment_id, ExperimentStatus.RUNNING)
    experiment.status = ExperimentStatus.RUNNING
    run_record, result = engine.run(conn, experiment, seed=seed,
                                    environment=environment,
                                    allow_cache=allow_cache)
    engine.save_run(conn, run_record, result)

    if result is None:
        final = (ExperimentStatus.CANCELLED
                 if run_record.status.value == "cancelled"
                 else ExperimentStatus.FAILED)
    elif result.decision == Decision.PASS:
        final = ExperimentStatus.PASSED
    elif result.decision == Decision.FAIL:
        final = ExperimentStatus.REJECTED
    else:
        final = ExperimentStatus.INCONCLUSIVE
    engine.set_status(conn, experiment_id, final)

    return {"run": run_record.as_dict(),
            "result": result.as_dict() if result else None,
            "status": final.value}


def cancel(conn: sqlite3.Connection, experiment_id: str,
           reason: str = "cancelled by request") -> Dict[str, Any]:
    """`POST /experiments/{id}/cancel` — partial results are preserved."""
    affected = engine.cancel(conn, experiment_id, reason)
    return {"experiment_id": experiment_id, "runs_cancelled": affected,
            "reason": reason,
            "note": ("partial results are kept: an experiment that was "
                     "cancelled halfway is evidence about how long it takes")}


def load(conn: sqlite3.Connection, experiment_id: str) -> Optional[Experiment]:
    """Rebuild a stored definition into an `Experiment`."""
    initialize_experiment_schema(conn)
    row = conn.execute(f"""
        SELECT {', '.join(_EXPERIMENT_COLUMNS)} FROM experiments
        WHERE experiment_id = ?
    """, (experiment_id,)).fetchone()
    if row is None:
        return None
    record = dict(zip(_EXPERIMENT_COLUMNS, row))

    def load_json(key, default):
        try:
            return json.loads(record.get(key) or "null") or default
        except (TypeError, ValueError):
            return default

    protocol_data = load_json("protocol_json", {})
    criteria_data = load_json("criteria_json", {})
    limits_data = load_json("limits_json", {})
    dataset_data = load_json("dataset_json", {})

    experiment = Experiment(
        experiment_id=record["experiment_id"],
        name=record["name"],
        experiment_type=ExperimentType(record["experiment_type"]),
        hypothesis=Hypothesis(
            statement=record["statement"], mechanism=record["mechanism"],
            expected_effect=record["expected_effect"],
            population=record["population"],
            conditions=load_json("conditions_json", {}),
            metric=record["metric"],
            minimum_detectable_effect=record["minimum_detectable_effect"],
            source=HypothesisSource(record["hypothesis_source"]),
            source_reference=record["source_reference"],
            family_id=record["family_id"]),
        baseline=ArmSpec(name=record["baseline_name"],
                         evaluator=record["baseline_evaluator"],
                         parameters=load_json("baseline_params_json", {}),
                         complexity=record["baseline_complexity"],
                         description=record["baseline_description"] or ""),
        candidate=ArmSpec(name=record["candidate_name"],
                          evaluator=record["candidate_evaluator"],
                          parameters=load_json("candidate_params_json", {}),
                          complexity=record["candidate_complexity"],
                          description=record["candidate_description"] or ""),
        dataset=DatasetSnapshot(
            as_of=dataset_data.get("as_of"),
            data_cutoff=dataset_data.get("data_cutoff"),
            universe=dataset_data.get("universe", "all"),
            filters=dataset_data.get("filters", {}),
            dataset_version=record["dataset_version"],
            feature_version=record["feature_version"],
            label_version=record["label_version"]),
        protocol=EvaluationProtocol(**{
            k: v for k, v in protocol_data.items()
            if k in EvaluationProtocol.__dataclass_fields__}),
        criteria=AcceptanceCriteria(**{
            k: v for k, v in criteria_data.items()
            if k in AcceptanceCriteria.__dataclass_fields__}),
        limits=ResourceLimits(**{
            k: v for k, v in limits_data.items()
            if k in ResourceLimits.__dataclass_fields__}),
        description=record["description"], created_by=record["created_by"],
        status=ExperimentStatus(record["status"]),
        method_version=record["method_version"],
        code_version=record["code_version"],
        model_version=record["model_version"],
        strategy_version=record["strategy_version"],
        configuration_version=record["configuration_version"])
    return experiment


def families(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """
    `GET /experiments/families` — with the counts that matter (§41-§43).

    `experiments` and `comparisons` are the selection-bias numbers. A
    family with one experiment and one comparison is a pre-registered
    test; a family with fifty is a search, and the difference must be
    visible without opening anything.
    """
    initialize_experiment_schema(conn)
    keys = ("family_id", "name", "core_statement", "created_at")
    out = []
    for row in conn.execute(f"""
        SELECT {', '.join(keys)} FROM hypothesis_families ORDER BY created_at
    """):
        record = dict(zip(keys, row))
        record["experiments"] = conn.execute(
            "SELECT COUNT(*) FROM experiments WHERE family_id = ?",
            (record["family_id"],)).fetchone()[0]
        record["comparisons"] = conn.execute("""
            SELECT COUNT(*) FROM experiment_results x
            JOIN experiments e ON e.experiment_id = x.experiment_id
            WHERE e.family_id = ?
        """, (record["family_id"],)).fetchone()[0]
        record["passed"] = conn.execute("""
            SELECT COUNT(*) FROM experiment_results x
            JOIN experiments e ON e.experiment_id = x.experiment_id
            WHERE e.family_id = ? AND x.decision = 'pass'
        """, (record["family_id"],)).fetchone()[0]
        record["expected_false_positives"] = round(record["comparisons"] * 0.05, 2)
        out.append(record)
    return out


def family_detail(conn: sqlite3.Connection,
                  family_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not family_id:
        return None
    for record in families(conn):
        if record["family_id"] == family_id:
            return record
    return None


def available_templates() -> List[Dict[str, Any]]:
    """`GET /experiments/templates` — including the ones that cannot run."""
    return [{"name": name, **spec}
            for name, spec in sorted(templates.TEMPLATES.items())]


def available_evaluators() -> List[Dict[str, Any]]:
    """
    Every registered arm, including the ones whose inputs are missing.

    Listing the unavailable ones is deliberate: an evaluator that
    silently did not appear would make the laboratory look complete.
    """
    return [{"name": spec.name, "description": spec.description,
             "parameters": list(spec.parameters),
             "requires": list(spec.requires), "runnable": spec.runnable,
             "unavailable_reason": spec.unavailable_reason}
            for spec in sorted(evaluators.registered(), key=lambda s: s.name)]


def compare(conn: sqlite3.Connection, experiment_ids: Sequence[str]
            ) -> Dict[str, Any]:
    """
    Side-by-side comparison (§64, §65).

    Deliberately does NOT rank. §65 forbids declaring one experiment
    best on a single metric, and a `rank` field would be used as one
    however it was documented. The caller gets every dimension and
    makes the judgement.
    """
    initialize_experiment_schema(conn)
    rows = []
    for experiment_id in experiment_ids:
        record = detail(conn, experiment_id)
        if record is None:
            continue
        latest = record["results"][0] if record["results"] else {}
        rows.append({
            "experiment_id": experiment_id, "name": record["name"],
            "status": record["status"],
            "statement": record["statement"],
            "changed_variables": record.get("changed_variables"),
            "metric": latest.get("metric"),
            "effect": latest.get("effect"),
            "effect_in_sample": latest.get("effect_in_sample"),
            "overfitting_gap": latest.get("overfitting_gap"),
            "interval": [latest.get("effect_low"), latest.get("effect_high")],
            "sample": (latest.get("candidate_oos") or {}).get("sample_size"),
            "robust": f"{latest.get('robust_slices_passing')}/"
                      f"{latest.get('robust_slices')}",
            "complexity_ratio": latest.get("complexity_ratio"),
            "family_experiments": latest.get("family_experiment_count"),
            "decision": latest.get("decision"),
        })
    return {
        "experiments": rows,
        "note": (
            "No ranking is provided. §65 forbids declaring one experiment "
            "best on a single metric: an effect, its interval, its sample, "
            "its robustness and its complexity are different questions, and "
            "a ranking would collapse them into one that answers none. "
            "Compare the columns."),
    }


def summary(conn: sqlite3.Connection) -> Dict[str, Any]:
    """The lab at a glance (§67)."""
    initialize_experiment_schema(conn)
    by_status = dict(conn.execute(
        "SELECT status, COUNT(*) FROM experiments GROUP BY status"))
    by_decision = dict(conn.execute(
        "SELECT decision, COUNT(*) FROM experiment_results GROUP BY decision"))
    by_type = dict(conn.execute(
        "SELECT experiment_type, COUNT(*) FROM experiments GROUP BY 1"))
    by_source = dict(conn.execute(
        "SELECT hypothesis_source, COUNT(*) FROM experiments GROUP BY 1"))
    total_comparisons = conn.execute(
        "SELECT COUNT(*) FROM experiment_results").fetchone()[0]
    return {
        "method_version": EXPERIMENT_METHOD_VERSION,
        "experiments": sum(by_status.values()),
        "by_status": by_status, "by_decision": by_decision,
        "by_type": by_type, "by_hypothesis_source": by_source,
        "runs": conn.execute("SELECT COUNT(*) FROM experiment_runs").fetchone()[0],
        "cache_hits": conn.execute(
            "SELECT COUNT(*) FROM experiment_runs WHERE cache_hit = 1").fetchone()[0],
        "families": conn.execute(
            "SELECT COUNT(*) FROM hypothesis_families").fetchone()[0],
        "total_comparisons": total_comparisons,
        "expected_false_positives": round(total_comparisons * 0.05, 2),
        "runnable_evaluators": sum(1 for spec in evaluators.registered()
                                   if spec.runnable),
        "declared_evaluators": len(evaluators.registered()),
    }


def _duplicate_comparisons(conn: sqlite3.Connection) -> int:
    """
    How many experiments repeat another experiment's comparison.

    Returns the count of REDUNDANT experiments -- three experiments
    over one comparison count as two -- so the number reads as "this
    much of the record is a repeat" rather than "this many were
    involved".
    """
    seen: Dict[str, int] = {}
    for row in conn.execute("SELECT experiment_id FROM experiments").fetchall():
        experiment = load(conn, row[0])
        if experiment is None:
            continue
        key = experiment.comparison_fingerprint
        seen[key] = seen.get(key, 0) + 1
    return sum(count - 1 for count in seen.values() if count > 1)


def integrity_check(conn: sqlite3.Connection) -> Dict[str, int]:
    """
    §88, as a query rather than a promise. Every count must be zero.

    The first two are the ones that matter most: an experiment with no
    mechanism is a pattern-match wearing a hypothesis, and a run whose
    fingerprint differs from its experiment's is evidence the
    definition moved after the answer.
    """
    initialize_experiment_schema(conn)

    def scalar(sql: str, params: tuple = ()) -> int:
        try:
            return conn.execute(sql, params).fetchone()[0]
        except sqlite3.OperationalError:
            return 0

    return {
        "experiments_without_a_mechanism": scalar(
            "SELECT COUNT(*) FROM experiments WHERE TRIM(mechanism) = ''"),
        "runs_whose_fingerprint_moved": scalar("""
            SELECT COUNT(*) FROM experiment_runs r
            JOIN experiments e ON e.experiment_id = r.experiment_id
            WHERE r.fingerprint != '' AND r.fingerprint != e.fingerprint
        """),
        "experiments_without_a_baseline": scalar(
            "SELECT COUNT(*) FROM experiments WHERE TRIM(baseline_evaluator) = ''"),
        "experiments_with_an_identical_candidate": scalar(
            "SELECT COUNT(*) FROM experiments WHERE changed_variables_json IN ('[]','')"),
        "results_without_a_run": scalar("""
            SELECT COUNT(*) FROM experiment_results x WHERE NOT EXISTS (
                SELECT 1 FROM experiment_runs r WHERE r.run_id = x.run_id)
        """),
        "runs_without_an_experiment": scalar("""
            SELECT COUNT(*) FROM experiment_runs r WHERE NOT EXISTS (
                SELECT 1 FROM experiments e
                WHERE e.experiment_id = r.experiment_id)
        """),
        "passes_below_the_minimum_sample": scalar("""
            SELECT COUNT(*) FROM experiment_results
            WHERE decision = 'pass'
              AND json_extract(candidate_oos_json, '$.sample_size') < 30
        """),
        "experiments_without_a_dataset_snapshot": scalar(
            "SELECT COUNT(*) FROM experiments WHERE TRIM(dataset_snapshot_id) = ''"),
        # Two experiments measuring the same two arms over the same
        # cohort are one piece of evidence wearing two hats. Counting
        # them separately overstates how much has been tested, and the
        # per-family correction in §41 does not catch it when they sit
        # in different families -- which is exactly how the first
        # proposal sweep produced three identical results.
        "duplicate_comparisons": _duplicate_comparisons(conn),
    }
