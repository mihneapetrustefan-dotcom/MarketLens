"""
src/attribution/pipeline.py
-----------------------------------
Batch attribution: load evidence, diagnose, persist, queue for review.

IDEMPOTENT BY CONSTRUCTION (§56)
------------------------------------
Identity is
`(subject_kind, subject_id, horizon, method_version, error_type)` and
writes are `INSERT OR REPLACE`. Running twice under the same
methodology replaces each row with an identical one; the row count
cannot change.

Evidence rows are deleted and rewritten for the attribution they
belong to, in the same transaction — otherwise a re-run would
accumulate duplicate evidence behind a stable conclusion, and every
"how many attributions rest on X" query would drift upward.

RECOMPUTATION AND VERSIONS (§44, §45)
-----------------------------------------
A methodology change means a new `ATTRIBUTION_METHOD_VERSION`, which
writes new rows beside the old ones. Nothing historical is destroyed,
and `compare_versions()` exists so two methodologies can be diffed
rather than one silently replacing the other.

WHAT THIS NEVER DOES (§59)
------------------------------
It writes `error_attributions`, `attribution_evidence` and
`attribution_review_queue`, and nothing else. No model, strategy,
threshold, weight, risk limit or capital figure is touched by any path
in this package, and `tests/attribution/test_leakage.py` proves it by
parsing the source.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.attribution.engine import AttributionReport, attribute
from src.data_access.attribution_schema import initialize_attribution_schema
from src.domain.attribution_models import (
    ATTRIBUTION_METHOD_VERSION, AttributionStatus, ErrorAttribution, ErrorType,
)
from src.domain.outcome_models import OUTCOME_METHOD_VERSION

#: Seconds-equivalent of one bar unit, for ordering horizons when
#: deciding which sibling is "later". Nominal 6.5h session for a day —
#: used only for ORDERING, never for measurement.
_UNIT_SECONDS = {"m": 60.0, "h": 3600.0, "d": 6.5 * 3600.0}


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _horizon_sort(value: Optional[float], unit: Optional[str]) -> float:
    return (value or 0.0) * _UNIT_SECONDS.get(unit or "d", _UNIT_SECONDS["d"])


_OUTCOME_COLUMNS = (
    "subject_kind", "subject_id", "horizon", "method_version", "status",
    "simple_return", "expected_return", "expected_direction",
    "realized_direction", "direction_result", "mfe", "mae",
    "time_to_mfe_seconds", "reference_price", "instrument_id",
    "trained_model_id", "model_status", "strategy_id", "market_regime",
    "event_type", "confidence", "strength", "signal_status",
    "horizon_value", "horizon_unit",
)


def load_outcomes(conn: sqlite3.Connection, *,
                  outcome_method_version: str = OUTCOME_METHOD_VERSION,
                  subject_kind: Optional[str] = None,
                  limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Every Phase 19 measurement, as a dict, with a sortable horizon.

    PENDING rows are loaded too. A pending outcome cannot be diagnosed —
    every detector will say so — but excluding it would hide from the
    dashboard how much of the record is not yet assessable, and that
    number is the honest headline.
    """
    if not _table_exists(conn, "outcome_measurements"):
        return []
    sql = (f"SELECT {', '.join(_OUTCOME_COLUMNS)} FROM outcome_measurements "
           f"WHERE method_version = ?")
    params: List[Any] = [outcome_method_version]
    if subject_kind:
        sql += " AND subject_kind = ?"
        params.append(subject_kind)
    sql += " ORDER BY subject_kind, subject_id, horizon_value"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)

    rows = []
    for row in conn.execute(sql, params):
        record = dict(zip(_OUTCOME_COLUMNS, row))
        record["horizon_sort"] = _horizon_sort(record.get("horizon_value"),
                                               record.get("horizon_unit"))
        rows.append(record)
    return rows


def load_signals(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """Signal state, for the signal-translation detector."""
    if not _table_exists(conn, "signals"):
        return {}
    keys = ("signal_id", "status", "direction", "strength", "confidence",
            "suppression_note", "observation_id", "valid_until",
            "source_information_cutoff")
    try:
        return {row[0]: dict(zip(keys, row)) for row in conn.execute(f"""
            SELECT {', '.join(keys)} FROM signals
        """)}
    except sqlite3.OperationalError:
        return {}


def load_observations(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """Decision-time data quality, keyed by observation (§38)."""
    if not _table_exists(conn, "research_observations"):
        return {}
    keys = ("observation_id", "quality_level", "market_regime",
            "information_cutoff", "dataset_version")
    return {row[0]: dict(zip(keys, row)) for row in conn.execute(f"""
        SELECT {', '.join(keys)} FROM research_observations
    """)}


def load_subject_observations(conn: sqlite3.Connection) -> Dict[Tuple[str, str], str]:
    """Map each subject to its observation, so data quality can be found."""
    mapping: Dict[Tuple[str, str], str] = {}
    if _table_exists(conn, "signals"):
        try:
            for signal_id, observation_id in conn.execute(
                    "SELECT signal_id, observation_id FROM signals"):
                if observation_id:
                    mapping[("signal", signal_id)] = observation_id
        except sqlite3.OperationalError:
            pass
    if _table_exists(conn, "predictions"):
        try:
            for prediction_id, observation_id in conn.execute(
                    "SELECT prediction_id, observation_id FROM predictions"):
                if observation_id:
                    mapping[("prediction", prediction_id)] = observation_id
        except sqlite3.OperationalError:
            pass
    return mapping


def load_execution_evidence(conn: sqlite3.Connection
                            ) -> Dict[str, Dict[str, Any]]:
    """
    Per-signal execution evidence, for the four detectors that could
    never fire (Phase 25).

    THE BUG THIS FIXES
    ----------------------
    `run()` passed `position=None, risk_decision=None, fill=None,
    portfolio=None` as literals, with a comment saying they were absent
    by construction. That was true when it was written -- no order had
    ever been placed -- and it stopped being true the moment Phase 25's
    loop produced its first fill. The literals would have kept the
    sizing, risk, execution and portfolio detectors reporting "no fill
    exists" forever, over a database full of fills.

    Keyed by `signal_id`, which is the join Phase 25 preserves end to
    end: signal -> decision -> intent -> order -> fill. An outcome
    whose subject is a signal therefore finds its own trade, and one
    that never traded finds nothing and the detectors correctly report
    the input as missing.
    """
    if not _table_exists(conn, "trade_outcomes"):
        return {}
    evidence: Dict[str, Dict[str, Any]] = {}
    try:
        rows = conn.execute("""
            SELECT signal_id, decision_price, fill_price, quantity,
                   entry_price, slippage_bps, commission, instrument_id,
                   order_id, outcome_id
              FROM trade_outcomes
             WHERE signal_id IS NOT NULL AND signal_id != ''
             ORDER BY entry_at
        """)
    except sqlite3.OperationalError:
        return {}
    for row in rows:
        # A signal that traded more than once keeps its FIRST trade.
        # The outcome being attributed is about the signal's claim, and
        # the trade that acted on it first is the one that tested it.
        evidence.setdefault(str(row[0]), {
            "decision_price": row[1], "fill_price": row[2],
            "quantity": row[3], "entry_price": row[4],
            "slippage_bps": row[5], "commission": row[6],
            "instrument_id": row[7], "order_id": row[8],
            "outcome_id": row[9]})
    return evidence


def load_risk_decisions(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """
    The risk verdict behind each signal's trade, by signal id.

    Joined through `trade_outcomes` rather than read from
    `risk_decisions` directly: a decision that never produced a trade
    has no bearing on a trade outcome, and including it would let the
    risk detector fire on a decision that blocked something else.
    """
    if not (_table_exists(conn, "trade_outcomes")
            and _table_exists(conn, "risk_decisions")):
        return {}
    try:
        rows = conn.execute("""
            SELECT o.signal_id, d.state, d.summary, d.decision_id,
                   (SELECT GROUP_CONCAT(v.constraint_id)
                      FROM risk_violations v
                     WHERE v.decision_id = d.decision_id
                       AND v.remediated = 0)
              FROM trade_outcomes o
              JOIN risk_decisions d ON d.decision_id = o.decision_id
             WHERE o.signal_id IS NOT NULL AND o.signal_id != ''
        """)
    except sqlite3.OperationalError:
        return {}
    # The keys are the ones `detect_risk_error` reads: `is_approved` and
    # `violated_limits`. Named to that detector's contract rather than
    # to the column names, because the detector is the consumer and a
    # mismatch here would silently produce "the risk decision matched
    # its recorded policy" on every row -- a clean bill of health from
    # a check that read nothing.
    return {str(r[0]): {
        "decision_id": r[3], "state": r[1], "summary": r[2],
        "is_approved": str(r[1]) in ("approved", "reduced"),
        "violated_limits": [v for v in str(r[4] or "").split(",") if v],
    } for r in rows}


def load_positions(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """
    The position each signal's trade established, with its risk budget.

    `risk_budget` comes from the account equity recorded on the cycle
    that placed the trade. Without a budget the sizing detector still
    reports the input as missing rather than dividing by a number
    nobody chose.
    """
    if not (_table_exists(conn, "trade_outcomes")
            and _table_exists(conn, "loop_account_states")):
        return {}
    # THE BUDGET AT THE TIME OF THE TRADE, not the latest one.
    #
    # An earlier version took `ORDER BY observed_at DESC LIMIT 1` -- the
    # most recent equity -- and applied it to every attribution
    # including trades from months earlier. That is future information
    # in a diagnosis, which is the one thing this project has been
    # built to prevent everywhere else. It made no visible difference
    # while the account was static and would have made a silent one the
    # moment equity moved.
    #
    # The join runs trade -> order -> lineage -> cycle -> the account
    # state recorded for THAT cycle.
    if not _table_exists(conn, "trade_lineage"):
        return {}
    try:
        rows = conn.execute("""
            SELECT o.signal_id, o.quantity, o.entry_price, o.instrument_id,
                   a.equity
              FROM trade_outcomes o
              JOIN trade_lineage l ON l.order_id = o.order_id
              JOIN loop_account_states a ON a.cycle_id = l.cycle_id
             WHERE o.signal_id IS NOT NULL AND o.signal_id != ''
               AND a.equity IS NOT NULL AND a.equity > 0
        """)
    except sqlite3.OperationalError:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for signal_id, quantity, entry_price, instrument_id, equity in rows:
        out.setdefault(str(signal_id), {
            "quantity": quantity, "risk_budget": float(equity),
            "entry_price": entry_price, "instrument_id": instrument_id})
    return out


def load_cohorts(conn: sqlite3.Connection, *,
                 outcome_method_version: str = OUTCOME_METHOD_VERSION
                 ) -> Dict[Tuple[str, str, str, str], Dict[str, Any]]:
    """
    Phase 19 aggregates, for the expected/unexpected judgement.

    Keyed `(subject_kind, cohort_kind, cohort_value, horizon)`.
    """
    if not _table_exists(conn, "outcome_aggregates"):
        return {}
    keys = ("subject_kind", "cohort_kind", "cohort_value", "horizon",
            "sample_size", "directional_accuracy", "mean_return",
            "p10_return", "p90_return", "small_sample")
    out = {}
    for row in conn.execute(f"""
        SELECT {', '.join(keys)} FROM outcome_aggregates WHERE method_version = ?
    """, (outcome_method_version,)):
        record = dict(zip(keys, row))
        out[(record["subject_kind"], record["cohort_kind"],
             record["cohort_value"], record["horizon"])] = record
    return out


def existing_identities(conn: sqlite3.Connection,
                        method_version: str) -> set:
    """Subjects already attributed under this methodology."""
    if not _table_exists(conn, "error_attributions"):
        return set()
    return {tuple(row) for row in conn.execute("""
        SELECT DISTINCT subject_kind, subject_id, horizon
        FROM error_attributions WHERE method_version = ?
    """, (method_version,))}


def save(conn: sqlite3.Connection,
         attributions: Iterable[ErrorAttribution]) -> int:
    """
    Persist conclusions and their evidence together.

    Evidence for an attribution is deleted and rewritten rather than
    appended, so a re-run cannot accumulate duplicates behind a stable
    conclusion.
    """
    initialize_attribution_schema(conn)
    written = 0
    for attribution in attributions:
        # Refuses to write an attribution with no evidence. An
        # explanation without evidence is an opinion stored as a fact.
        attribution.require_evidence()

        conn.execute("""
            INSERT OR REPLACE INTO error_attributions (
                subject_kind, subject_id, horizon, method_version, error_type,
                role, confidence, severity, status, observability, summary,
                expected_direction, expected_return, realized_return, deviation,
                instrument_id, trained_model_id, model_status, strategy_id,
                market_regime, event_type, confidence_score, strength,
                signal_status, outcome_method_version, attributed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            attribution.subject_kind, attribution.subject_id,
            attribution.horizon, attribution.method_version,
            attribution.error_type.value, attribution.role.value,
            attribution.confidence.value, attribution.severity.value,
            attribution.status.value, attribution.observability.value,
            attribution.summary, attribution.expected_direction,
            attribution.expected_return, attribution.realized_return,
            attribution.deviation, attribution.instrument_id,
            attribution.trained_model_id, attribution.model_status,
            attribution.strategy_id, attribution.market_regime,
            attribution.event_type, attribution.confidence_score,
            attribution.strength, attribution.signal_status,
            attribution.outcome_method_version,
            attribution.attributed_at.isoformat()))

        conn.execute("""
            DELETE FROM attribution_evidence
            WHERE subject_kind=? AND subject_id=? AND horizon=?
              AND method_version=? AND error_type=?
        """, (attribution.subject_kind, attribution.subject_id,
              attribution.horizon, attribution.method_version,
              attribution.error_type.value))

        for position, item in enumerate(attribution.evidence):
            digest = hashlib.sha256("|".join([
                attribution.subject_kind, attribution.subject_id,
                attribution.horizon, attribution.method_version,
                attribution.error_type.value, str(position), item.kind,
            ]).encode()).hexdigest()[:24]
            conn.execute("""
                INSERT OR REPLACE INTO attribution_evidence (
                    evidence_id, subject_kind, subject_id, horizon,
                    method_version, error_type, kind, statement, source,
                    value, comparison, detail_json, position
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (f"ev-{digest}", attribution.subject_kind,
                  attribution.subject_id, attribution.horizon,
                  attribution.method_version, attribution.error_type.value,
                  item.kind, item.statement, item.source, item.value,
                  item.comparison, json.dumps(item.detail, sort_keys=True),
                  position))
        written += 1
    conn.commit()
    return written


def queue_for_review(conn: sqlite3.Connection, outcome: Dict[str, Any],
                     reason: str, candidates: Sequence[str],
                     severity: str, method_version: str,
                     now: Optional[datetime] = None) -> None:
    """
    Record a case a person should look at (§48).

    Never auto-closed. The queue exists because the rules could not
    decide, so a rule must not decide it is finished either.
    """
    initialize_attribution_schema(conn)
    now = now or datetime.now(timezone.utc)
    digest = hashlib.sha256("|".join([
        outcome.get("subject_kind", ""), outcome.get("subject_id", ""),
        outcome.get("horizon", ""), method_version]).encode()).hexdigest()[:24]
    recommendation = (
        "compare the candidate layers by hand against the price path and the "
        "signal record; if one is clearly upstream of the other, encode the "
        "distinction as a rule and bump the methodology version"
        if len(candidates) > 1 else
        "inspect the price path around the information cutoff and check "
        "whether an input the detectors cannot see explains the result")
    conn.execute("""
        INSERT OR REPLACE INTO attribution_review_queue (
            review_id, subject_kind, subject_id, horizon, method_version,
            reason, candidate_types, recommended_check, severity, state,
            queued_at
        ) VALUES (?,?,?,?,?,?,?,?,?,
                  COALESCE((SELECT state FROM attribution_review_queue
                            WHERE review_id = ?), 'open'), ?)
    """, (f"rq-{digest}", outcome.get("subject_kind"), outcome.get("subject_id"),
          outcome.get("horizon"), method_version, reason,
          json.dumps(list(candidates)), recommendation, severity,
          f"rq-{digest}", now.isoformat()))
    conn.commit()


def run(conn: sqlite3.Connection, *,
        method_version: str = ATTRIBUTION_METHOD_VERSION,
        outcome_method_version: str = OUTCOME_METHOD_VERSION,
        subject_kind: Optional[str] = None,
        limit: Optional[int] = None,
        recompute: bool = False
        ) -> Tuple[List[ErrorAttribution], List[Dict[str, Any]], AttributionReport]:
    """
    Diagnose every measured outcome.

    Returns `(attributions, review_cases, report)`. Nothing is written —
    persistence is the caller's, so a dry run is the default shape.
    That is the convention `inference.score()` and
    `outcomes.pipeline.run()` both use.
    """
    outcomes = load_outcomes(conn,
                             outcome_method_version=outcome_method_version,
                             subject_kind=subject_kind, limit=limit)
    signals = load_signals(conn)
    observations = load_observations(conn)
    subject_to_observation = load_subject_observations(conn)
    cohorts = load_cohorts(conn, outcome_method_version=outcome_method_version)
    fills = load_execution_evidence(conn)
    risk_decisions = load_risk_decisions(conn)
    positions = load_positions(conn)
    already = set() if recompute else existing_identities(conn, method_version)

    # Siblings, so the horizon detector can see the same subject at
    # other windows. Grouped once rather than queried per outcome.
    by_subject: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for outcome in outcomes:
        by_subject.setdefault(
            (outcome["subject_kind"], outcome["subject_id"]), []).append(outcome)

    report = AttributionReport(method_version=method_version)
    attributions: List[ErrorAttribution] = []
    review_cases: List[Dict[str, Any]] = []

    for outcome in outcomes:
        report.outcomes_seen += 1
        key = (outcome["subject_kind"], outcome["subject_id"],
               outcome["horizon"])
        if key in already:
            report.skipped_existing += 1
            continue

        subject_key = (outcome["subject_kind"], outcome["subject_id"])
        siblings = [row for row in by_subject.get(subject_key, ())
                    if row["horizon"] != outcome["horizon"]]
        signal = (signals.get(outcome["subject_id"])
                  if outcome["subject_kind"] == "signal" else None)
        observation_id = subject_to_observation.get(subject_key)
        observation = observations.get(observation_id) if observation_id else None
        regime_cohort = cohorts.get(
            (outcome["subject_kind"], "regime",
             str(outcome.get("market_regime")), outcome["horizon"]))
        overall_cohort = cohorts.get(
            (outcome["subject_kind"], "overall", "all", outcome["horizon"]))

        produced, results, review_reason = attribute(
            outcome, cohort=overall_cohort, method_version=method_version,
            siblings=siblings, signal=signal, observation=observation,
            regime_cohort=regime_cohort,
            # Loaded, not hard-coded. These four were literal `None`
            # until Phase 25, which was correct while no order had ever
            # been placed and would have gone on reporting "no fill
            # exists" over a database full of fills. A signal that
            # never traded still yields None here, and the detector
            # still names the missing input -- which is the behaviour
            # the literals were standing in for.
            position=positions.get(outcome["subject_id"]),
            risk_decision=risk_decisions.get(outcome["subject_id"]),
            fill=fills.get(outcome["subject_id"]),
            # Portfolio-level concentration is not measured per trade
            # yet: §17's detector needs a concentration figure against a
            # limit, and Phase 25 records exposure without a per-cycle
            # concentration measure. Left None so the detector says so.
            portfolio=None)

        for result in results:
            report.note_layer(result)

        for attribution in produced:
            attributions.append(attribution)
            report.findings += 1
            name = attribution.error_type.value
            report.by_type[name] = report.by_type.get(name, 0) + 1

        primary = next((a for a in produced
                        if a.role.value == "primary"), None)
        if primary is not None:
            if primary.error_type == ErrorType.NO_ERROR:
                report.no_error += 1
            elif primary.error_type == ErrorType.EXPECTED_LOSS:
                report.expected_loss += 1
            elif primary.error_type == ErrorType.UNKNOWN:
                if primary.status == AttributionStatus.INSUFFICIENT_EVIDENCE:
                    report.insufficient += 1
                else:
                    report.unknown += 1
            else:
                report.attributed += 1

        if review_reason:
            report.requires_review += 1
            review_cases.append({
                "outcome": outcome, "reason": review_reason,
                "candidates": [a.error_type.value for a in produced],
                "severity": (primary.severity.value if primary else "info"),
            })

    return attributions, review_cases, report


def compare_versions(conn: sqlite3.Connection, left: str,
                     right: str) -> List[Dict[str, Any]]:
    """
    Where two methodologies disagree (§45).

    Returns one row per subject whose PRIMARY error type differs
    between versions — which is the question a methodology change
    actually raises: what conclusions did this change, and were those
    changes intended?
    """
    initialize_attribution_schema(conn)
    keys = ("subject_kind", "subject_id", "horizon", "left_type",
            "left_confidence", "right_type", "right_confidence")
    return [dict(zip(keys, row)) for row in conn.execute("""
        SELECT a.subject_kind, a.subject_id, a.horizon,
               a.error_type, a.confidence, b.error_type, b.confidence
        FROM error_attributions a
        JOIN error_attributions b
          ON b.subject_kind = a.subject_kind AND b.subject_id = a.subject_id
         AND b.horizon = a.horizon AND b.role = 'primary'
        WHERE a.method_version = ? AND b.method_version = ?
          AND a.role = 'primary' AND a.error_type != b.error_type
        ORDER BY a.subject_id, a.horizon
    """, (left, right))]
