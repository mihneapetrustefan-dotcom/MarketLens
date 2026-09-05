"""
src/outcomes/api.py
---------------------------
The query surface for outcome intelligence (§46).

WHY THESE ARE FUNCTIONS AND NOT HTTP ROUTES
-----------------------------------------------
§46 asks for `GET /outcomes`, `GET /signals/{id}/outcomes` and the rest,
"following existing API conventions". The existing convention, recorded
in `docs/API_AUDIT.md` and re-verified in Phase 17.5, is that **this
repository has no HTTP layer at all** — no Flask, FastAPI, Django,
aiohttp or uvicorn, and no bound socket anywhere in `src/` or
`scripts/`. It is a set of scheduled scripts writing SQLite and
publishing a static page, and the audit's assessment was *KEEP*.

Adding a web server to satisfy the letter of §46 would introduce the
project's first inbound network surface, contradict a documented
architectural decision, and require an authentication story for a
system that currently has no way to be reached at all. So the routes
are implemented as the shapes the repository actually uses — typed
functions over a connection, the same convention as `ExecutionService`
and the fourteen repository classes:

    GET /outcomes                      -> list_outcomes()
    GET /signals/{id}/outcomes         -> outcomes_for_signal()
    GET /predictions/{id}/outcomes     -> outcomes_for_prediction()
    GET /models/{id}/outcomes          -> outcomes_for_model()
    GET /outcomes/summary              -> summary()
    GET /outcomes/by-horizon           -> by_horizon()
    GET /outcomes/by-regime            -> by_cohort("regime")
    GET /outcomes/by-instrument        -> by_cohort("instrument")

Every one is read-only. Nothing in this module writes.

PAGINATION IS REAL, NOT DECORATIVE
--------------------------------------
`limit` and `offset` exist on the list endpoints because
`outcome_measurements` grows without bound: 6,510 rows for 408 signals
and 549 predictions over seven horizons today, and linearly in every
new signal. An unpaginated list endpoint over a table like that is a
memory incident waiting for a slow week.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from src.data_access.outcome_schema import initialize_outcome_schema
from src.domain.outcome_models import OUTCOME_METHOD_VERSION, OutcomeStatus

#: Columns returned by the list endpoints. Named explicitly rather than
#: `SELECT *` so a schema change cannot silently alter what a caller
#: receives.
_MEASUREMENT_COLUMNS = (
    "subject_kind", "subject_id", "horizon", "method_version", "status",
    "information_cutoff", "window_start", "window_end",
    "reference_price", "reference_at", "reference_rule", "end_price", "end_at",
    "simple_return", "log_return", "mfe", "mae", "mfe_at", "mae_at",
    "time_to_mfe_seconds", "time_to_mae_seconds",
    "expected_direction", "realized_direction", "direction_result",
    "expected_return", "error", "absolute_error",
    "data_source", "data_interval", "bars_observed", "data_as_of",
    "instrument_id", "trained_model_id", "model_status", "strategy_id",
    "market_regime", "event_type", "confidence", "strength", "signal_status",
    "notes_json", "computed_at",
)

_AGGREGATE_COLUMNS = (
    "aggregate_id", "method_version", "subject_kind", "cohort_kind",
    "cohort_value", "horizon", "sample_size", "instrument_count",
    "small_sample", "hits", "misses", "neutrals", "insufficient",
    "directional_accuracy", "mean_return", "median_return", "stdev_return",
    "min_return", "max_return", "p10_return", "p25_return", "p75_return",
    "p90_return", "mean_mfe", "mean_mae", "median_mfe", "median_mae",
    "mean_time_to_mfe", "mean_expected_return", "mean_absolute_error",
    "ci_low", "ci_high", "ci_method", "notes_json", "computed_at",
)

#: A hard ceiling regardless of what a caller asks for.
MAX_LIMIT = 1000


def _rows(conn, sql, params, columns) -> List[Dict[str, Any]]:
    out = []
    for row in conn.execute(sql, params):
        record = dict(zip(columns, row))
        if record.get("notes_json"):
            try:
                record["notes"] = json.loads(record.pop("notes_json"))
            except (TypeError, ValueError):
                record["notes"] = []
        else:
            record.pop("notes_json", None)
            record["notes"] = []
        out.append(record)
    return out


def list_outcomes(conn: sqlite3.Connection, *,
                  subject_kind: Optional[str] = None,
                  horizon: Optional[str] = None,
                  status: Optional[str] = None,
                  instrument_id: Optional[str] = None,
                  trained_model_id: Optional[str] = None,
                  method_version: str = OUTCOME_METHOD_VERSION,
                  limit: int = 100,
                  offset: int = 0) -> List[Dict[str, Any]]:
    """`GET /outcomes` — filtered, paginated, newest cutoff first."""
    initialize_outcome_schema(conn)
    clauses = ["method_version = ?"]
    params: List[Any] = [method_version]
    for column, value in (("subject_kind", subject_kind), ("horizon", horizon),
                          ("status", status), ("instrument_id", instrument_id),
                          ("trained_model_id", trained_model_id)):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    sql = (f"SELECT {', '.join(_MEASUREMENT_COLUMNS)} FROM outcome_measurements "
           f"WHERE {' AND '.join(clauses)} "
           f"ORDER BY information_cutoff DESC, horizon LIMIT ? OFFSET ?")
    params += [max(1, min(int(limit), MAX_LIMIT)), max(0, int(offset))]
    return _rows(conn, sql, params, _MEASUREMENT_COLUMNS)


def outcomes_for_signal(conn: sqlite3.Connection, signal_id: str, *,
                        method_version: str = OUTCOME_METHOD_VERSION
                        ) -> List[Dict[str, Any]]:
    """
    `GET /signals/{id}/outcomes` — every horizon for one signal.

    Ordered shortest horizon first, so the result reads as a decay
    curve for that signal rather than as an arbitrary list.
    """
    initialize_outcome_schema(conn)
    sql = (f"SELECT {', '.join(_MEASUREMENT_COLUMNS)} FROM outcome_measurements "
           f"WHERE subject_kind = 'signal' AND subject_id = ? "
           f"AND method_version = ? "
           f"ORDER BY CASE horizon_unit WHEN 'm' THEN horizon_value "
           f"  WHEN 'h' THEN horizon_value * 60 "
           f"  ELSE horizon_value * 390 END")
    return _rows(conn, sql, (signal_id, method_version), _MEASUREMENT_COLUMNS)


def outcomes_for_prediction(conn: sqlite3.Connection, prediction_id: str, *,
                            method_version: str = OUTCOME_METHOD_VERSION
                            ) -> List[Dict[str, Any]]:
    """`GET /predictions/{id}/outcomes`."""
    initialize_outcome_schema(conn)
    sql = (f"SELECT {', '.join(_MEASUREMENT_COLUMNS)} FROM outcome_measurements "
           f"WHERE subject_kind = 'prediction' AND subject_id = ? "
           f"AND method_version = ? ORDER BY horizon_value")
    return _rows(conn, sql, (prediction_id, method_version), _MEASUREMENT_COLUMNS)


def outcomes_for_model(conn: sqlite3.Connection, trained_model_id: str, *,
                       method_version: str = OUTCOME_METHOD_VERSION
                       ) -> List[Dict[str, Any]]:
    """
    `GET /models/{id}/outcomes` — the model's REALIZED record.

    Deliberately separate from `model_evaluations`, which holds its
    TRAINING metrics (§44). A model that scored well on a held-out
    split and badly in the forward record is exactly the case worth
    seeing, and pooling the two would hide it.
    """
    initialize_outcome_schema(conn)
    sql = """
        SELECT cohort_kind, cohort_value, horizon, sample_size,
               instrument_count, small_sample, hits, misses, neutrals,
               insufficient, directional_accuracy, mean_return, median_return,
               stdev_return, mean_mfe, mean_mae, ci_low, ci_high, ci_method
        FROM outcome_aggregates
        WHERE method_version = ? AND cohort_kind = 'model' AND cohort_value = ?
    """
    keys = ("cohort_kind", "cohort_value", "horizon", "sample_size",
            "instrument_count", "small_sample", "hits", "misses", "neutrals",
            "insufficient", "directional_accuracy", "mean_return",
            "median_return", "stdev_return", "mean_mfe", "mean_mae",
            "ci_low", "ci_high", "ci_method")
    return [dict(zip(keys, row))
            for row in conn.execute(sql, (method_version, trained_model_id))]


def summary(conn: sqlite3.Connection, *,
            method_version: str = OUTCOME_METHOD_VERSION) -> Dict[str, Any]:
    """
    `GET /outcomes/summary` — coverage first, then results.

    Coverage comes first on purpose. "51% directional accuracy" means
    something very different at 90% coverage than at 30%, and a summary
    that led with the rate would invite the reader to skip the number
    that qualifies it.
    """
    initialize_outcome_schema(conn)
    by_status = dict(conn.execute("""
        SELECT status, COUNT(*) FROM outcome_measurements
        WHERE method_version = ? GROUP BY status
    """, (method_version,)))
    total = sum(by_status.values())
    available = by_status.get(OutcomeStatus.AVAILABLE.value, 0)

    verdicts = dict(conn.execute("""
        SELECT direction_result, COUNT(*) FROM outcome_measurements
        WHERE method_version = ? AND status = 'available'
        GROUP BY direction_result
    """, (method_version,)))
    hits = verdicts.get("hit", 0)
    misses = verdicts.get("miss", 0)

    subjects = dict(conn.execute("""
        SELECT subject_kind, COUNT(DISTINCT subject_id)
        FROM outcome_measurements WHERE method_version = ? GROUP BY subject_kind
    """, (method_version,)))

    aggregate_count = conn.execute(
        "SELECT COUNT(*) FROM outcome_aggregates WHERE method_version = ?",
        (method_version,)).fetchone()[0]

    return {
        "method_version": method_version,
        "measurements": total,
        "by_status": by_status,
        "coverage": (available / total) if total else None,
        "subjects": subjects,
        "hits": hits,
        "misses": misses,
        "neutrals": verdicts.get("neutral", 0),
        # None, not 0.0, when nothing was decided. A hit rate of zero
        # and no measurements at all are different facts.
        "directional_accuracy": (hits / (hits + misses)) if (hits + misses) else None,
        "cohorts": aggregate_count,
        "horizons": [row[0] for row in conn.execute("""
            SELECT DISTINCT horizon FROM outcome_measurements
            WHERE method_version = ?
            ORDER BY CASE horizon_unit WHEN 'm' THEN horizon_value
                     WHEN 'h' THEN horizon_value * 60
                     ELSE horizon_value * 390 END
        """, (method_version,))],
    }


def by_horizon(conn: sqlite3.Connection, *,
               subject_kind: str = "signal",
               method_version: str = OUTCOME_METHOD_VERSION
               ) -> List[Dict[str, Any]]:
    """`GET /outcomes/by-horizon` — the decay curve (§14)."""
    from src.outcomes.analytics import decay_curve
    return decay_curve(conn, subject_kind=subject_kind,
                       method_version=method_version)


def by_cohort(conn: sqlite3.Connection, cohort_kind: str, *,
              horizon: Optional[str] = None,
              subject_kind: str = "signal",
              min_sample: int = 0,
              method_version: str = OUTCOME_METHOD_VERSION
              ) -> List[Dict[str, Any]]:
    """
    `GET /outcomes/by-regime`, `/by-instrument`, and every other slice.

    `min_sample` filters out cohorts too small to mean anything. It
    defaults to 0 — showing everything, flagged — because hiding small
    cohorts entirely makes coverage look better than it is.
    """
    initialize_outcome_schema(conn)
    clauses = ["method_version = ?", "subject_kind = ?", "cohort_kind = ?"]
    params: List[Any] = [method_version, subject_kind, cohort_kind]
    if horizon:
        clauses.append("horizon = ?")
        params.append(horizon)
    if min_sample:
        clauses.append("sample_size >= ?")
        params.append(int(min_sample))
    sql = (f"SELECT {', '.join(_AGGREGATE_COLUMNS)} FROM outcome_aggregates "
           f"WHERE {' AND '.join(clauses)} "
           f"ORDER BY sample_size DESC, cohort_value")
    return _rows(conn, sql, params, _AGGREGATE_COLUMNS)


def model_quality_versus_outcome(conn: sqlite3.Connection, *,
                                 horizon: str = "5d",
                                 method_version: str = OUTCOME_METHOD_VERSION
                                 ) -> List[Dict[str, Any]]:
    """
    §45: the evaluator's verdict next to the realized record.

    The question this exists to make answerable is whether
    `beats_all_baselines` — the gate Phase 18 wired into inference —
    actually predicts forward performance. Right now no model passes it,
    so the comparison has one column filled and the other constant;
    that is itself the finding, and it becomes interesting the moment a
    model is promoted.

    Deliberately NOT a score or a verdict. It returns the two sets of
    numbers side by side and leaves the comparison to a person.
    """
    initialize_outcome_schema(conn)
    rows = conn.execute("""
        SELECT m.trained_model_id, m.model_qualified_id, m.status,
               e.metrics_json, e.beats_all_baselines, e.small_sample,
               e.cluster_count,
               a.sample_size, a.directional_accuracy, a.mean_return,
               a.median_return, a.small_sample, a.ci_low, a.ci_high
        FROM trained_models m
        LEFT JOIN model_evaluations e ON e.trained_model_id = m.trained_model_id
        LEFT JOIN outcome_aggregates a
               ON a.cohort_kind = 'model' AND a.cohort_value = m.trained_model_id
              AND a.horizon = ? AND a.method_version = ?
              AND a.subject_kind = 'signal'
        ORDER BY m.trained_at DESC
    """, (horizon, method_version)).fetchall()

    out = []
    for row in rows:
        try:
            metrics = json.loads(row[3] or "{}")
        except (TypeError, ValueError):
            metrics = {}
        out.append({
            "trained_model_id": row[0],
            "model_qualified_id": row[1],
            "status": row[2],
            # Training-time evidence
            "r_squared": metrics.get("r_squared"),
            "training_directional_accuracy": metrics.get("directional_accuracy"),
            "beats_all_baselines": row[4],
            "training_small_sample": row[5],
            "cluster_count": row[6],
            # Forward, realized evidence
            "outcome_sample_size": row[7],
            "outcome_directional_accuracy": row[8],
            "outcome_mean_return": row[9],
            "outcome_median_return": row[10],
            "outcome_small_sample": row[11],
            "outcome_ci_low": row[12],
            "outcome_ci_high": row[13],
            "horizon": horizon,
        })
    return out
