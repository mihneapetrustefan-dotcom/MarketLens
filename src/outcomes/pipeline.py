"""
src/outcomes/pipeline.py
--------------------------------
Batch outcome measurement: signals and predictions, many horizons.

    subject  ->  resolve window  ->  load future bars  ->  measure
             ->  store  ->  aggregate

IDEMPOTENT BY CONSTRUCTION (§30)
------------------------------------
The primary key is
`(subject_kind, subject_id, horizon, method_version)` and writes are
`INSERT OR REPLACE`. Running twice with the same methodology replaces
each row with an identical one; the row COUNT cannot change. That is
stronger than "we remember what we did", because it survives a crash
halfway through and it needs no bookkeeping table of its own.

REPROCESSING (§31)
----------------------
Three things make a stored measurement stale, and they are handled
differently on purpose:

  * NEW MARKET DATA — a PENDING row whose window has since closed.
    Re-measured under the same version, which is a correction to a
    measurement that was explicitly incomplete, not a rewrite of a
    reported one.
  * A METHODOLOGY CHANGE — a new `OUTCOME_METHOD_VERSION`. Writes NEW
    rows beside the old ones. Nothing historical is touched.
  * A DATA CORRECTION — `--rescore` re-measures AVAILABLE rows under
    the current version. Off by default, because silently rewriting a
    number somebody has already read is the thing §31 warns about.

WHAT THIS NEVER DOES
------------------------
It does not write to `signals`, `predictions`, `research_features` or
`trained_models`. Outcomes flow one way. `tests/outcomes/test_leakage.py`
checks that by reading the source of this package.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from src.data_access.outcome_schema import initialize_outcome_schema
from src.domain.outcome_models import (
    DEFAULT_HORIZONS, OUTCOME_METHOD_VERSION, OutcomeMeasurement,
    OutcomeStatus, OutcomeWindow, SubjectKind, parse_horizons,
)
from src.outcomes.measurement import INTERVAL_FOR_UNIT, load_bars, measure

DATA_SOURCE = "price_candle_cache"


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


@dataclass
class MeasurementReport:
    """What one pass did, and what it declined to do."""
    subjects: int = 0
    measured: int = 0
    pending: int = 0
    insufficient: int = 0
    invalid: int = 0
    skipped_existing: int = 0
    skipped_no_direction: int = 0
    horizons: List[str] = field(default_factory=list)
    data_as_of: Optional[datetime] = None
    method_version: str = OUTCOME_METHOD_VERSION

    def count(self, outcome: OutcomeMeasurement) -> None:
        if outcome.status == OutcomeStatus.AVAILABLE:
            self.measured += 1
        elif outcome.status == OutcomeStatus.PENDING:
            self.pending += 1
        elif outcome.status == OutcomeStatus.INVALID:
            self.invalid += 1
        else:
            self.insufficient += 1

    def as_dict(self) -> Dict[str, Any]:
        return {
            "subjects": self.subjects, "measured": self.measured,
            "pending": self.pending, "insufficient": self.insufficient,
            "invalid": self.invalid, "skipped_existing": self.skipped_existing,
            "horizons": self.horizons,
            "method_version": self.method_version,
            "data_as_of": self.data_as_of.isoformat() if self.data_as_of else None,
        }


def data_as_of(conn: sqlite3.Connection) -> Optional[datetime]:
    """
    The newest price bar we hold, over all instruments.

    This is the clock that decides PENDING versus INSUFFICIENT_DATA
    (§32, §33). Wall-clock time would be wrong: the question is not
    "has enough time passed in the world" but "has enough time passed
    in the data we actually have", and those differ by however stale
    the price cache is.
    """
    if not _table_exists(conn, "price_candle_cache"):
        return None
    row = conn.execute("SELECT MAX(timestamp) FROM price_candle_cache").fetchone()
    return _parse(row[0]) if row else None


def load_signal_subjects(conn: sqlite3.Connection,
                         limit: Optional[int] = None,
                         since: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Signals to measure, with the context needed to slice them later.

    EVERY signal is measured, including suppressed and expired ones
    (§38). A suppressed signal still made a claim about the market, and
    "what would have happened had we not suppressed it" is the only way
    to find out whether the suppression rule is any good. They are
    outcomes, never trades — no order existed and none is implied.

    The model comes through `signal_contributions`, which is the direct
    link Phase 18 verified for all 408 signals. `model_status` is read
    live so an experimental signal stays labelled experimental.
    """
    if not _table_exists(conn, "signals"):
        return []
    has_contributions = _table_exists(conn, "signal_contributions")
    has_models = _table_exists(conn, "trained_models")

    sql = """
        SELECT s.signal_id, s.instrument_id, s.direction, s.status,
               s.strength, s.confidence, s.expected_return,
               s.source_information_cutoff, s.strategy_id,
               s.market_regime, s.event_type, s.observation_id
        FROM signals s
        WHERE s.source_information_cutoff IS NOT NULL
    """
    params: List[Any] = []
    if since:
        sql += " AND s.source_information_cutoff >= ?"
        params.append(since)
    sql += " ORDER BY s.source_information_cutoff DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(sql, params).fetchall()

    # Model attribution is looked up SEPARATELY and guarded, for the
    # reason Phase 17.5 recorded: joining an optional table into the
    # main query makes it a hard dependency, and a missing table would
    # silently return no subjects at all rather than unattributed ones.
    models: Dict[str, tuple] = {}
    if has_contributions:
        model_sql = """
            SELECT c.signal_id, c.trained_model_id{status}
            FROM signal_contributions c{join}
        """.format(
            status=", m.status" if has_models else ", NULL",
            join=(" LEFT JOIN trained_models m "
                  "ON m.trained_model_id = c.trained_model_id") if has_models else "")
        try:
            for signal_id, trained_model_id, status in conn.execute(model_sql):
                # Weakest contributor wins, matching the dashboard: a
                # signal is only as validated as its least-validated input.
                if signal_id not in models or status != "active":
                    models[signal_id] = (trained_model_id, status)
        except sqlite3.OperationalError:
            models = {}

    subjects = []
    for row in rows:
        trained_model_id, model_status = models.get(row[0], (None, None))
        subjects.append({
            "subject_kind": SubjectKind.SIGNAL,
            "subject_id": row[0],
            "instrument_id": row[1],
            "direction": row[2],
            "signal_status": row[3],
            "strength": row[4],
            "confidence": row[5],
            "expected_return": row[6],
            "cutoff": _parse(row[7]),
            "strategy_id": row[8],
            "market_regime": row[9],
            "event_type": row[10],
            "trained_model_id": trained_model_id,
            "model_status": model_status,
        })
    return subjects


def load_prediction_subjects(conn: sqlite3.Connection,
                             limit: Optional[int] = None,
                             since: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Predictions to measure (§3, §4).

    A prediction is a NUMBER, not a direction, so its direction is
    derived from the sign of `predicted_value` purely to make the
    excursion arithmetic well-defined. That derived direction is not a
    trading claim — the signal layer is what turns a number into a
    claim — and keeping the two measurements separate is the whole
    point of §2: a model can be directionally right while the signal
    built on it is suppressed, and vice versa.

    Abstentions are skipped. A model that declined to predict made no
    claim, and scoring the absence of a claim would make abstaining a
    way to improve one's record.
    """
    if not (_table_exists(conn, "predictions")
            and _table_exists(conn, "research_observations")):
        return []

    sql = """
        SELECT p.prediction_id, o.instrument_id, p.predicted_value,
               p.confidence, p.information_cutoff, p.trained_model_id,
               o.market_regime, o.event_type, p.observation_id
        FROM predictions p
        JOIN research_observations o ON o.observation_id = p.observation_id
        WHERE p.information_cutoff IS NOT NULL
          AND COALESCE(p.is_abstention, 0) = 0
          AND p.predicted_value IS NOT NULL
    """
    params: List[Any] = []
    if since:
        sql += " AND p.information_cutoff >= ?"
        params.append(since)
    sql += " ORDER BY p.information_cutoff DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)

    statuses: Dict[str, str] = {}
    if _table_exists(conn, "trained_models"):
        statuses = dict(conn.execute(
            "SELECT trained_model_id, status FROM trained_models"))

    subjects = []
    for row in conn.execute(sql, params):
        predicted = row[2]
        subjects.append({
            "subject_kind": SubjectKind.PREDICTION,
            "subject_id": row[0],
            "instrument_id": row[1],
            "direction": "long" if predicted > 0 else ("short" if predicted < 0 else "neutral"),
            "expected_return": predicted,
            "confidence": row[3],
            "cutoff": _parse(row[4]),
            "trained_model_id": row[5],
            "model_status": statuses.get(row[5]),
            "market_regime": row[6],
            "event_type": row[7],
        })
    return subjects


def existing_identities(conn: sqlite3.Connection, method_version: str,
                        statuses: Sequence[str]) -> set:
    """Which (kind, id, horizon) rows already hold a settled measurement."""
    if not _table_exists(conn, "outcome_measurements"):
        return set()
    placeholders = ",".join("?" * len(statuses))
    return {
        tuple(row) for row in conn.execute(f"""
            SELECT subject_kind, subject_id, horizon FROM outcome_measurements
            WHERE method_version = ? AND status IN ({placeholders})
        """, (method_version, *statuses))
    }


def save(conn: sqlite3.Connection,
         outcomes: Iterable[OutcomeMeasurement]) -> int:
    """
    Persist. INSERT OR REPLACE on the natural key, so a second run over
    the same subjects and methodology cannot add a row (§30).
    """
    import json

    initialize_outcome_schema(conn)

    def iso(value):
        return value.isoformat() if value else None

    written = 0
    for outcome in outcomes:
        conn.execute("""
            INSERT OR REPLACE INTO outcome_measurements (
                subject_kind, subject_id, horizon, method_version,
                horizon_value, horizon_unit, status,
                information_cutoff, window_start, window_end,
                reference_price, reference_at, reference_rule,
                end_price, end_at, simple_return, log_return,
                mfe, mae, mfe_at, mae_at,
                time_to_mfe_seconds, time_to_mae_seconds,
                expected_direction, realized_direction, direction_result,
                expected_return, error, absolute_error,
                data_source, data_interval, bars_observed, data_as_of,
                instrument_id, trained_model_id, model_status, strategy_id,
                market_regime, event_type, confidence, strength,
                signal_status, notes_json, computed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                      ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            outcome.subject_kind.value, outcome.subject_id,
            outcome.horizon.key, outcome.method_version,
            outcome.horizon.horizon_value, outcome.horizon.horizon_unit,
            outcome.status.value,
            iso(outcome.information_cutoff), iso(outcome.window_start),
            iso(outcome.window_end),
            outcome.reference_price, iso(outcome.reference_at),
            outcome.reference_rule.value,
            outcome.end_price, iso(outcome.end_at),
            outcome.simple_return, outcome.log_return,
            outcome.mfe, outcome.mae, iso(outcome.mfe_at), iso(outcome.mae_at),
            outcome.time_to_mfe_seconds, outcome.time_to_mae_seconds,
            outcome.expected_direction, outcome.realized_direction,
            outcome.direction_result.value,
            outcome.expected_return, outcome.error, outcome.absolute_error,
            outcome.data_source, outcome.data_interval, outcome.bars_observed,
            iso(outcome.data_as_of),
            outcome.instrument_id, outcome.trained_model_id,
            outcome.model_status, outcome.strategy_id, outcome.market_regime,
            outcome.event_type, outcome.confidence, outcome.strength,
            outcome.signal_status, json.dumps(outcome.notes),
            iso(outcome.computed_at) or datetime.now(timezone.utc).isoformat()))
        written += 1
    conn.commit()
    return written


def run(conn: sqlite3.Connection, *,
        horizons: Sequence[str] = DEFAULT_HORIZONS,
        subject_kinds: Sequence[SubjectKind] = (SubjectKind.SIGNAL,),
        limit: Optional[int] = None,
        since: Optional[str] = None,
        rescore: bool = False,
        method_version: str = OUTCOME_METHOD_VERSION,
        now: Optional[datetime] = None) -> tuple:
    """
    Measure everything measurable and return `(outcomes, report)`.

    Nothing is written here — persistence is the caller's, so a dry run
    is the default shape rather than a flag threaded through every
    layer. That is the same convention `inference.score()` uses.
    """
    windows = parse_horizons(horizons)
    as_of = data_as_of(conn) or now
    report = MeasurementReport(horizons=[w.key for w in windows],
                               data_as_of=as_of, method_version=method_version)

    subjects: List[Dict[str, Any]] = []
    if SubjectKind.SIGNAL in subject_kinds:
        subjects += load_signal_subjects(conn, limit, since)
    if SubjectKind.PREDICTION in subject_kinds:
        subjects += load_prediction_subjects(conn, limit, since)
    report.subjects = len(subjects)

    # A settled row is skipped unless --rescore. PENDING is never
    # skipped: it exists precisely to be revisited once the data
    # catches up (§31, §33).
    settled = existing_identities(
        conn, method_version,
        (OutcomeStatus.AVAILABLE.value, OutcomeStatus.INSUFFICIENT_DATA.value,
         OutcomeStatus.INVALID.value)) if not rescore else set()

    # Bars are loaded once per (instrument, interval) and reused across
    # every horizon, rather than once per horizon per subject. With
    # seven horizons that is a sevenfold reduction in queries, and it is
    # the difference between a minute and ten.
    bar_cache: Dict[tuple, List[Dict[str, Any]]] = {}
    outcomes: List[OutcomeMeasurement] = []

    for subject in subjects:
        cutoff = subject.get("cutoff")
        instrument_id = subject.get("instrument_id")
        direction = (subject.get("direction") or "").strip().lower()
        if cutoff is None or not instrument_id:
            report.skipped_no_direction += 1
            continue
        if direction in ("", "no_signal", "none"):
            # An abstention is not a claim, so there is nothing to score.
            report.skipped_no_direction += 1
            continue

        for window in windows:
            identity = (subject["subject_kind"].value, subject["subject_id"],
                        window.key)
            if identity in settled:
                report.skipped_existing += 1
                continue

            interval = INTERVAL_FOR_UNIT[window.horizon_unit]
            key = (instrument_id, interval)
            if key not in bar_cache:
                bar_cache[key] = load_bars(conn, instrument_id, interval, cutoff)

            outcome = measure(
                subject["subject_kind"], subject["subject_id"], window,
                cutoff=cutoff, direction=direction, bars=bar_cache[key],
                data_as_of=as_of, expected_return=subject.get("expected_return"),
                interval=interval, data_source=DATA_SOURCE,
                method_version=method_version,
                instrument_id=instrument_id,
                trained_model_id=subject.get("trained_model_id"),
                model_status=subject.get("model_status"),
                strategy_id=subject.get("strategy_id"),
                market_regime=subject.get("market_regime"),
                event_type=subject.get("event_type"),
                confidence=subject.get("confidence"),
                strength=subject.get("strength"),
                signal_status=subject.get("signal_status"))
            outcomes.append(outcome)
            report.count(outcome)

    return outcomes, report
