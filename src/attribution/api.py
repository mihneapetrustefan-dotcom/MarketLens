"""
src/attribution/api.py
------------------------------
The query surface for error attribution (§53), plus export (§57) and
the counterfactual foundation (§23, §24).

CONVENTION
--------------
`docs/API_AUDIT.md` records that this repository has no HTTP layer, by
decision, assessed *KEEP*. Phase 19 implemented its routes as typed
functions over a connection; this follows that, so the two phases have
one convention rather than two.

    GET /error-attribution        -> list_attributions()
    GET /signals/{id}/errors      -> errors_for_signal()
    GET /predictions/{id}/errors  -> errors_for_prediction()
    GET /models/{id}/errors       -> errors_for_model()
    GET /errors/summary           -> summary()
    GET /errors/by-type           -> by_type()
    GET /errors/by-model          -> by_model()
    GET /errors/by-regime         -> by_regime()
    GET /errors/review            -> review_queue()

All read-only. Everything that lists paginates.

COUNTERFACTUALS ARE ARCHITECTURE, NOT ANSWERS (§23, §24)
------------------------------------------------------------
`counterfactual()` returns the QUESTION, the inputs available to
answer it, and `observability='hypothetical'`. It does not compute an
alternative outcome, because computing one honestly needs an execution
model, a fill model and a sizing rule, and none of the three exists in
this database.

What it does provide is the shape: a named question, the observed
facts that bear on it, and a label that makes it impossible to read as
history. §23 asks for the architecture only, and inventing the numbers
would be exactly the fabrication §0 forbids.
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
from typing import Any, Dict, List, Optional

from src.attribution import analytics
from src.data_access.attribution_schema import initialize_attribution_schema
from src.domain.attribution_models import (
    ATTRIBUTION_METHOD_VERSION, ErrorType, Observability,
)

MAX_LIMIT = 1000

_COLUMNS = (
    "subject_kind", "subject_id", "horizon", "method_version", "error_type",
    "role", "confidence", "severity", "status", "observability", "summary",
    "expected_direction", "expected_return", "realized_return", "deviation",
    "instrument_id", "trained_model_id", "model_status", "strategy_id",
    "market_regime", "event_type", "confidence_score", "strength",
    "signal_status", "outcome_method_version", "attributed_at",
)


def _with_evidence(conn: sqlite3.Connection,
                   rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Attach the evidence to each attribution.

    Always attached, never optional. §22 requires every attribution to
    reference evidence, and an endpoint that could return a conclusion
    without its evidence would make it easy to build a consumer that
    never looks at any.
    """
    for row in rows:
        row["evidence"] = [
            {"kind": kind, "statement": statement, "source": source,
             "value": value, "comparison": comparison,
             "detail": json.loads(detail or "{}")}
            for kind, statement, source, value, comparison, detail
            in conn.execute("""
                SELECT kind, statement, source, value, comparison, detail_json
                FROM attribution_evidence
                WHERE subject_kind=? AND subject_id=? AND horizon=?
                  AND method_version=? AND error_type=?
                ORDER BY position
            """, (row["subject_kind"], row["subject_id"], row["horizon"],
                  row["method_version"], row["error_type"]))
        ]
    return rows


def list_attributions(conn: sqlite3.Connection, *,
                      error_type: Optional[str] = None,
                      role: Optional[str] = None,
                      status: Optional[str] = None,
                      confidence: Optional[str] = None,
                      subject_kind: Optional[str] = None,
                      horizon: Optional[str] = None,
                      trained_model_id: Optional[str] = None,
                      instrument_id: Optional[str] = None,
                      observability: str = Observability.OBSERVED.value,
                      method_version: str = ATTRIBUTION_METHOD_VERSION,
                      limit: int = 100, offset: int = 0
                      ) -> List[Dict[str, Any]]:
    """
    `GET /error-attribution`.

    Defaults to OBSERVED. A caller has to ask for hypotheticals by
    name, so a counterfactual cannot arrive in a result set that reads
    as history (§24).
    """
    initialize_attribution_schema(conn)
    clauses = ["method_version = ?", "observability = ?"]
    params: List[Any] = [method_version, observability]
    for column, value in (("error_type", error_type), ("role", role),
                          ("status", status), ("confidence", confidence),
                          ("subject_kind", subject_kind), ("horizon", horizon),
                          ("trained_model_id", trained_model_id),
                          ("instrument_id", instrument_id)):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    sql = (f"SELECT {', '.join(_COLUMNS)} FROM error_attributions "
           f"WHERE {' AND '.join(clauses)} "
           f"ORDER BY attributed_at DESC, subject_id, horizon LIMIT ? OFFSET ?")
    params += [max(1, min(int(limit), MAX_LIMIT)), max(0, int(offset))]
    rows = [dict(zip(_COLUMNS, row)) for row in conn.execute(sql, params)]
    return _with_evidence(conn, rows)


def _errors_for_subject(conn, subject_kind: str, subject_id: str,
                        method_version: str) -> List[Dict[str, Any]]:
    initialize_attribution_schema(conn)
    rows = [dict(zip(_COLUMNS, row)) for row in conn.execute(f"""
        SELECT {', '.join(_COLUMNS)} FROM error_attributions
        WHERE subject_kind = ? AND subject_id = ? AND method_version = ?
        ORDER BY CASE role WHEN 'primary' THEN 0 ELSE 1 END, horizon
    """, (subject_kind, subject_id, method_version))]
    return _with_evidence(conn, rows)


def errors_for_signal(conn: sqlite3.Connection, signal_id: str, *,
                      method_version: str = ATTRIBUTION_METHOD_VERSION
                      ) -> List[Dict[str, Any]]:
    """`GET /signals/{id}/errors` — primary first, then contributing."""
    return _errors_for_subject(conn, "signal", signal_id, method_version)


def errors_for_prediction(conn: sqlite3.Connection, prediction_id: str, *,
                          method_version: str = ATTRIBUTION_METHOD_VERSION
                          ) -> List[Dict[str, Any]]:
    """`GET /predictions/{id}/errors`."""
    return _errors_for_subject(conn, "prediction", prediction_id, method_version)


def errors_for_model(conn: sqlite3.Connection, trained_model_id: str, *,
                     method_version: str = ATTRIBUTION_METHOD_VERSION
                     ) -> List[Dict[str, Any]]:
    """`GET /models/{id}/errors` — the model's failure profile (§50)."""
    return [row for row in analytics.model_error_profile(
        conn, method_version=method_version)
        if row["cohort_value"] == trained_model_id]


def summary(conn: sqlite3.Connection, *,
            method_version: str = ATTRIBUTION_METHOD_VERSION) -> Dict[str, Any]:
    """`GET /errors/summary` — coverage first, then the distribution."""
    return analytics.coverage(conn, method_version=method_version)


def by_type(conn: sqlite3.Connection, *,
            method_version: str = ATTRIBUTION_METHOD_VERSION
            ) -> List[Dict[str, Any]]:
    """
    `GET /errors/by-type`.

    Primary and contributing counts side by side, because "was usually
    the main cause" and "was often involved" are different claims and
    a single count conflates them.
    """
    initialize_attribution_schema(conn)
    primary = dict(conn.execute("""
        SELECT error_type, COUNT(*) FROM error_attributions
        WHERE method_version = ? AND role='primary' AND observability='observed'
        GROUP BY error_type
    """, (method_version,)))
    contributing = dict(conn.execute("""
        SELECT error_type, COUNT(*) FROM error_attributions
        WHERE method_version = ? AND role='contributing'
          AND observability='observed'
        GROUP BY error_type
    """, (method_version,)))
    out = []
    for member in ErrorType:
        name = member.value
        if not (primary.get(name) or contributing.get(name)):
            continue
        out.append({
            "error_type": name, "is_error": member.is_error,
            "primary": primary.get(name, 0),
            "contributing": contributing.get(name, 0),
            "total": primary.get(name, 0) + contributing.get(name, 0),
        })
    return sorted(out, key=lambda row: -row["total"])


def by_model(conn: sqlite3.Connection, *,
             method_version: str = ATTRIBUTION_METHOD_VERSION
             ) -> List[Dict[str, Any]]:
    """`GET /errors/by-model` (§30, §50)."""
    return analytics.model_error_profile(conn, method_version=method_version)


def by_regime(conn: sqlite3.Connection, *,
              method_version: str = ATTRIBUTION_METHOD_VERSION
              ) -> List[Dict[str, Any]]:
    """`GET /errors/by-regime` (§32)."""
    return analytics.regime_error_profile(conn, method_version=method_version)


def review_queue(conn: sqlite3.Connection, *,
                 state: str = "open",
                 method_version: str = ATTRIBUTION_METHOD_VERSION,
                 limit: int = 100) -> List[Dict[str, Any]]:
    """`GET /errors/review` — the cases the rules could not decide (§48)."""
    initialize_attribution_schema(conn)
    keys = ("review_id", "subject_kind", "subject_id", "horizon", "reason",
            "candidate_types", "recommended_check", "severity", "state",
            "queued_at")
    rows = []
    for row in conn.execute(f"""
        SELECT {', '.join(keys)} FROM attribution_review_queue
        WHERE method_version = ? AND state = ?
        ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                               WHEN 'medium' THEN 2 WHEN 'low' THEN 3
                               ELSE 4 END, queued_at DESC
        LIMIT ?
    """, (method_version, state, max(1, min(int(limit), MAX_LIMIT)))):
        record = dict(zip(keys, row))
        try:
            record["candidate_types"] = json.loads(record["candidate_types"])
        except (TypeError, ValueError):
            record["candidate_types"] = []
        rows.append(record)
    return rows


# ======================================================================
# Counterfactual foundation (§23, §24)
# ======================================================================

#: The questions a future counterfactual engine will answer. Named here
#: so the vocabulary is fixed before anything computes against it.
COUNTERFACTUAL_QUESTIONS = {
    "earlier_entry": "What if the position had opened at the favourable extreme?",
    "half_size": "What if the position had been half the size?",
    "risk_not_rejected": "What if risk had approved this?",
    "better_fill": "What if execution had matched the decision price?",
    "different_horizon": "What if the horizon had been the one that worked?",
    "different_model": "What if another model had scored this observation?",
}


def counterfactual(conn: sqlite3.Connection, subject_kind: str,
                   subject_id: str, horizon: str, question: str, *,
                   method_version: str = ATTRIBUTION_METHOD_VERSION
                   ) -> Dict[str, Any]:
    """
    The scaffolding for a counterfactual — never an answer (§23, §24).

    Returns the question, the observed facts that bear on it, what
    would be needed to answer it, and `observability='hypothetical'`.

    It deliberately computes no alternative outcome. Doing that
    honestly needs an execution model, a fill model and a sizing rule,
    and none exists in this database. A number produced without them
    would be a fabrication wearing a result's clothes, and §24's whole
    concern is that such a number never becomes readable as history.
    """
    if question not in COUNTERFACTUAL_QUESTIONS:
        raise ValueError(
            f"Unknown counterfactual {question!r}. Known: "
            + ", ".join(sorted(COUNTERFACTUAL_QUESTIONS)))

    initialize_attribution_schema(conn)
    outcome = conn.execute("""
        SELECT simple_return, mfe, mae, time_to_mfe_seconds, reference_price,
               end_price, expected_direction, expected_return, status
        FROM outcome_measurements
        WHERE subject_kind=? AND subject_id=? AND horizon=?
    """, (subject_kind, subject_id, horizon)).fetchone()

    observed: Dict[str, Any] = {}
    if outcome:
        observed = dict(zip(
            ("simple_return", "mfe", "mae", "time_to_mfe_seconds",
             "reference_price", "end_price", "expected_direction",
             "expected_return", "status"), outcome))

    requirements = {
        "earlier_entry": ["an entry rule", "an exit rule"],
        "half_size": ["a position size", "a risk budget"],
        "risk_not_rejected": ["a risk decision record", "a sizing rule"],
        "better_fill": ["an order", "a fill", "a slippage model"],
        "different_horizon": [],   # answerable today from the sibling rows
        "different_model": ["a second trained model scoring the same observation"],
    }[question]

    return {
        "observability": Observability.HYPOTHETICAL.value,
        "question": COUNTERFACTUAL_QUESTIONS[question],
        "question_key": question,
        "subject_kind": subject_kind, "subject_id": subject_id,
        "horizon": horizon, "method_version": method_version,
        "observed": observed,
        "requires": requirements,
        "answerable_now": not requirements,
        "result": None,
        "note": ("This is a hypothetical framing, not a result. No "
                 "alternative outcome is computed: doing so honestly "
                 "requires "
                 + (", ".join(requirements) if requirements
                    else "inputs that do exist — see the sibling horizons")
                 + ". Never present this as something that happened."),
    }


# ======================================================================
# Research export (§57)
# ======================================================================

def export_rows(conn: sqlite3.Connection, *,
                method_version: str = ATTRIBUTION_METHOD_VERSION,
                include_hypothetical: bool = False) -> List[Dict[str, Any]]:
    """
    A flat research table: attribution joined to its outcome.

    Excludes hypotheticals by default and carries `observability` as a
    column regardless, so a downstream consumer that ignores the flag
    still has it in the file rather than having to know to ask.
    """
    initialize_attribution_schema(conn)
    clause = "" if include_hypothetical else " AND a.observability = 'observed'"
    keys = ("subject_kind", "subject_id", "horizon", "method_version",
            "error_type", "role", "confidence", "severity", "status",
            "observability", "summary", "expected_direction",
            "expected_return", "realized_return", "deviation",
            "instrument_id", "trained_model_id", "model_status",
            "strategy_id", "market_regime", "event_type", "confidence_score",
            "strength", "signal_status", "outcome_status", "simple_return",
            "log_return", "mfe", "mae", "time_to_mfe_seconds",
            "direction_result", "evidence_count")
    return [dict(zip(keys, row)) for row in conn.execute(f"""
        SELECT a.subject_kind, a.subject_id, a.horizon, a.method_version,
               a.error_type, a.role, a.confidence, a.severity, a.status,
               a.observability, a.summary, a.expected_direction,
               a.expected_return, a.realized_return, a.deviation,
               a.instrument_id, a.trained_model_id, a.model_status,
               a.strategy_id, a.market_regime, a.event_type,
               a.confidence_score, a.strength, a.signal_status,
               o.status, o.simple_return, o.log_return, o.mfe, o.mae,
               o.time_to_mfe_seconds, o.direction_result,
               (SELECT COUNT(*) FROM attribution_evidence e
                 WHERE e.subject_kind=a.subject_kind AND e.subject_id=a.subject_id
                   AND e.horizon=a.horizon AND e.method_version=a.method_version
                   AND e.error_type=a.error_type)
        FROM error_attributions a
        LEFT JOIN outcome_measurements o
               ON o.subject_kind = a.subject_kind AND o.subject_id = a.subject_id
              AND o.horizon = a.horizon
        WHERE a.method_version = ?{clause}
        ORDER BY a.subject_kind, a.subject_id, a.horizon, a.role
    """, (method_version,))]


def export_csv(conn: sqlite3.Connection, path: str, *,
               method_version: str = ATTRIBUTION_METHOD_VERSION,
               include_hypothetical: bool = False) -> int:
    """
    Write the research export to CSV.

    CSV rather than Parquet: Parquet needs pyarrow, and this repository
    computes research numbers on the standard library everywhere else.
    A format that only works when an optional dependency is installed
    is a format that will one day not work.
    """
    rows = export_rows(conn, method_version=method_version,
                       include_hypothetical=include_hypothetical)
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    if not rows:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write("")
        return 0
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def export_evidence_csv(conn: sqlite3.Connection, path: str, *,
                        method_version: str = ATTRIBUTION_METHOD_VERSION) -> int:
    """The evidence table, so a conclusion can be re-derived from source."""
    initialize_attribution_schema(conn)
    keys = ("subject_kind", "subject_id", "horizon", "error_type", "kind",
            "statement", "source", "value", "comparison", "position")
    rows = [dict(zip(keys, row)) for row in conn.execute(f"""
        SELECT {', '.join(keys)} FROM attribution_evidence
        WHERE method_version = ?
        ORDER BY subject_kind, subject_id, horizon, error_type, position
    """, (method_version,))]
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        if rows:
            writer = csv.DictWriter(handle, fieldnames=keys)
            writer.writeheader()
            writer.writerows(rows)
    return len(rows)
