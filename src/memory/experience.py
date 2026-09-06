"""
src/memory/experience.py
--------------------------------
Turning outcomes and attributions into structured experience.

    outcome (Phase 19) + attribution (Phase 20) + decision-time context
        -> TradingExperience

WHAT THIS FILE READS AND NEVER WRITES
-----------------------------------------
Reads `outcome_measurements`, `error_attributions`,
`attribution_evidence`, `signals`, `research_observations`,
`instruments`, `securities`, `trained_models`.

Writes `trading_experiences`, `memory_patterns`,
`memory_pattern_evidence`, `memory_snapshots` — and nothing else, ever.
Memory is downstream of everything; it must not be able to change what
it remembers. `tests/memory/test_leakage.py` proves that by parsing
this package's source.

`available_at` — THE ONE THING TO GET RIGHT
-----------------------------------------------
An experience becomes knowable when its outcome window CLOSES, not when
we compute it. `available_at = outcome.window_end`.

Dating experience by `computed_at` would place the entire record at one
instant, and `memory_as_of(2026-08-20)` would return outcomes that had
not yet happened. The leakage would be total and invisible: every
historical study would quietly consult its own future and report
excellent results.

A row with no `window_end` — an outcome still pending, or one that
could never be measured — gets no `available_at` and is INCOMPLETE. It
is kept, because knowing that something could not be measured is
itself worth remembering, but it never enters a point-in-time result.

CONTEXT IS DECISION-TIME ONLY (§8)
--------------------------------------
Every field on `ExperienceContext` was knowable before the outcome.
Nothing is reconstructed from what happened next. A test compares the
context field names against the outcome columns to make sure a result
cannot sneak in wearing a context label.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.data_access.memory_schema import initialize_memory_schema
from src.domain.attribution_models import ATTRIBUTION_METHOD_VERSION
from src.domain.memory_models import (
    CONTEXT_SCHEMA_VERSION, MEMORY_METHOD_VERSION, ExperienceClass,
    ExperienceContext, ExperienceKind, ExperienceQuality, TradingExperience,
    classify_experience, experience_id_for,
)
from src.domain.outcome_models import OUTCOME_METHOD_VERSION


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


_OUTCOME_COLUMNS = (
    "subject_kind", "subject_id", "horizon", "method_version", "status",
    "information_cutoff", "window_end", "simple_return", "expected_return",
    "expected_direction", "realized_direction", "direction_result",
    "mfe", "mae", "time_to_mfe_seconds", "instrument_id", "trained_model_id",
    "model_status", "strategy_id", "market_regime", "event_type",
    "confidence", "strength", "signal_status",
)


def load_outcomes(conn: sqlite3.Connection, *,
                  outcome_version: str = OUTCOME_METHOD_VERSION,
                  since: Optional[str] = None,
                  limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Phase 19 measurements, optionally only those that became knowable
    after `since` — which is how incremental updates avoid rebuilding
    the whole record (§55).
    """
    if not _table_exists(conn, "outcome_measurements"):
        return []
    sql = (f"SELECT {', '.join(_OUTCOME_COLUMNS)} FROM outcome_measurements "
           f"WHERE method_version = ?")
    params: List[Any] = [outcome_version]
    if since:
        sql += " AND window_end IS NOT NULL AND window_end > ?"
        params.append(since)
    sql += " ORDER BY window_end, subject_id, horizon"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return [dict(zip(_OUTCOME_COLUMNS, row)) for row in conn.execute(sql, params)]


def load_attributions(conn: sqlite3.Connection, *,
                      attribution_version: str = ATTRIBUTION_METHOD_VERSION
                      ) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    """
    Phase 20 conclusions, keyed by subject and horizon.

    Primary and contributing are kept apart — §12 links attribution
    without duplicating its logic, and the distinction between "was the
    main cause" and "was involved" is exactly what would be lost by
    flattening them into one list.
    """
    if not _table_exists(conn, "error_attributions"):
        return {}
    out: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for row in conn.execute("""
        SELECT subject_kind, subject_id, horizon, error_type, role,
               confidence, severity, status
        FROM error_attributions
        WHERE method_version = ? AND observability = 'observed'
    """, (attribution_version,)):
        key = (row[0], row[1], row[2])
        record = out.setdefault(key, {
            "primary": None, "contributing": [], "confidence": None,
            "severity": None, "status": None})
        if row[4] == "primary":
            record["primary"] = row[3]
            record["confidence"] = row[5]
            record["severity"] = row[6]
            record["status"] = row[7]
        else:
            record["contributing"].append(row[3])
    return out


def load_evidence_counts(conn: sqlite3.Connection, *,
                         attribution_version: str = ATTRIBUTION_METHOD_VERSION
                         ) -> Dict[Tuple[str, str, str], int]:
    """How many facts back each subject's attributions."""
    if not _table_exists(conn, "attribution_evidence"):
        return {}
    return {
        (row[0], row[1], row[2]): row[3] for row in conn.execute("""
            SELECT subject_kind, subject_id, horizon, COUNT(*)
            FROM attribution_evidence WHERE method_version = ?
            GROUP BY 1,2,3
        """, (attribution_version,))
    }


def load_decision_context(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """
    Decision-time context per signal (§8).

    Only fields knowable BEFORE the outcome. `volatility_percentile` and
    `relative_volume` were computed from data up to the information
    cutoff by Phase 10 and are safe; the realised return is not here and
    must never be.
    """
    if not _table_exists(conn, "signals"):
        return {}
    keys = ("signal_id", "signal_type", "strategy_id", "strategy_version",
            "market_regime", "volatility_percentile", "relative_volume",
            "data_quality_level", "event_type", "event_id", "observation_id")
    try:
        return {row[0]: dict(zip(keys, row)) for row in conn.execute(f"""
            SELECT {', '.join(keys)} FROM signals
        """)}
    except sqlite3.OperationalError:
        return {}


def load_instrument_context(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """
    Asset class and sector, for instrument and sector memory (§19).

    Guarded and looked up separately: the registry is optional, and the
    Phase 17.5 lesson was that joining an optional table into a main
    query turns it into a hard dependency that silently empties the
    result.
    """
    if not _table_exists(conn, "instruments"):
        return {}
    try:
        return {
            row[0]: {"asset_class": row[1], "sector_id": row[2]}
            for row in conn.execute("""
                SELECT i.instrument_id, i.asset_class, co.sector_id
                FROM instruments i
                LEFT JOIN securities se ON se.security_id = i.security_id
                LEFT JOIN companies co ON co.company_id = se.company_id
            """)
        }
    except sqlite3.OperationalError:
        return {}


def load_unexpectedness(conn: sqlite3.Connection, *,
                        outcome_version: str = OUTCOME_METHOD_VERSION
                        ) -> Dict[Tuple[str, str], Tuple[Optional[float], Optional[float], int]]:
    """
    The cohort percentile band Phase 19 computed, for expected versus
    unexpected classification (§13).

    Reused rather than recomputed: one definition of "unusual" across
    three phases.
    """
    if not _table_exists(conn, "outcome_aggregates"):
        return {}
    return {
        (row[0], row[1]): (row[2], row[3], row[4] or 0)
        for row in conn.execute("""
            SELECT subject_kind, horizon, p10_return, p90_return, sample_size
            FROM outcome_aggregates
            WHERE method_version = ? AND cohort_kind='overall'
              AND cohort_value='all'
        """, (outcome_version,))
    }


def build_experience(outcome: Dict[str, Any], *,
                     attribution: Optional[Dict[str, Any]] = None,
                     evidence_count: int = 0,
                     signal_context: Optional[Dict[str, Any]] = None,
                     instrument_context: Optional[Dict[str, Any]] = None,
                     unexpected: Optional[bool] = None,
                     memory_version: str = MEMORY_METHOD_VERSION,
                     attribution_version: str = ATTRIBUTION_METHOD_VERSION
                     ) -> TradingExperience:
    """
    One experience, assembled. Never invented.

    Quality is decided by completeness and by the Phase 18 model status,
    in that order:

      INCOMPLETE    no measured outcome, or no attribution to explain it
      EXPERIMENTAL  complete, but produced by a model nobody promoted
      VALIDATED     complete and production-facing

    §6 says do not discard experimental data, so it is kept and marked.
    A pattern built from unpromoted-model output describes research, not
    the system, and pooling the two would make that indistinguishable.
    """
    signal_context = signal_context or {}
    instrument_context = instrument_context or {}
    attribution = attribution or {}

    context = ExperienceContext(
        schema_version=CONTEXT_SCHEMA_VERSION,
        instrument_id=outcome.get("instrument_id") or "",
        asset_class=instrument_context.get("asset_class"),
        sector_id=instrument_context.get("sector_id"),
        event_type=outcome.get("event_type") or signal_context.get("event_type"),
        event_id=signal_context.get("event_id"),
        market_regime=outcome.get("market_regime") or signal_context.get("market_regime"),
        volatility_percentile=signal_context.get("volatility_percentile"),
        relative_volume=signal_context.get("relative_volume"),
        data_quality=signal_context.get("data_quality_level"),
        signal_type=signal_context.get("signal_type"),
        strategy_id=outcome.get("strategy_id") or signal_context.get("strategy_id"),
        strategy_version=signal_context.get("strategy_version"),
        horizon=outcome.get("horizon") or "",
        information_cutoff=_parse(outcome.get("information_cutoff")))

    kind = (ExperienceKind.SIGNAL if outcome.get("subject_kind") == "signal"
            else ExperienceKind.PREDICTION)

    experience = TradingExperience(
        experience_id=experience_id_for(
            outcome.get("subject_kind", ""), outcome.get("subject_id", ""),
            outcome.get("horizon", ""), memory_version),
        kind=kind, memory_version=memory_version,
        subject_kind=outcome.get("subject_kind", ""),
        subject_id=outcome.get("subject_id", ""),
        horizon=outcome.get("horizon", ""),
        outcome_method_version=outcome.get("method_version") or "",
        attribution_method_version=attribution_version,
        trained_model_id=outcome.get("trained_model_id"),
        model_status=outcome.get("model_status"),
        strategy_id=outcome.get("strategy_id"),
        observation_id=signal_context.get("observation_id"),
        information_cutoff=_parse(outcome.get("information_cutoff")),
        # The point-in-time key: when the window CLOSED.
        available_at=_parse(outcome.get("window_end")),
        expected_direction=outcome.get("expected_direction") or "",
        expected_return=outcome.get("expected_return"),
        expected_horizon=outcome.get("horizon") or "",
        signal_confidence=outcome.get("confidence"),
        signal_strength=outcome.get("strength"),
        actual_return=outcome.get("simple_return"),
        actual_direction=outcome.get("realized_direction"),
        direction_result=outcome.get("direction_result"),
        mfe=outcome.get("mfe"), mae=outcome.get("mae"),
        time_to_mfe_seconds=outcome.get("time_to_mfe_seconds"),
        primary_error=attribution.get("primary"),
        contributing_errors=list(attribution.get("contributing") or ()),
        attribution_confidence=attribution.get("confidence"),
        attribution_severity=attribution.get("severity"),
        evidence_count=evidence_count,
        context=context)

    experience.experience_class = classify_experience(
        direction_result=experience.direction_result,
        primary_error=experience.primary_error,
        actual_return=experience.actual_return,
        unexpected=unexpected)

    # ---- eligibility (§6) -------------------------------------------
    reasons: List[str] = []
    if outcome.get("status") != "available":
        reasons.append(f"the outcome is {outcome.get('status')}, not measured")
    if experience.available_at is None:
        reasons.append("no window close time, so it has no point in time at "
                       "which it became knowable")
    if not experience.primary_error:
        reasons.append("no error attribution explains it")

    if reasons:
        experience.quality = ExperienceQuality.INCOMPLETE
        experience.notes.extend(reasons)
        experience.notes.append(
            "kept rather than discarded: knowing that something could not be "
            "measured is itself worth remembering, but it will never enter a "
            "point-in-time result")
    elif (experience.model_status or "").lower() != "active":
        experience.quality = ExperienceQuality.EXPERIMENTAL
        experience.notes.append(
            f"produced by a model with status "
            f"{experience.model_status or 'unknown'} — research experience, "
            f"kept but never pooled with production experience")
    else:
        experience.quality = ExperienceQuality.VALIDATED

    return experience


def build_all(conn: sqlite3.Connection, *,
              memory_version: str = MEMORY_METHOD_VERSION,
              outcome_version: str = OUTCOME_METHOD_VERSION,
              attribution_version: str = ATTRIBUTION_METHOD_VERSION,
              since: Optional[str] = None,
              limit: Optional[int] = None) -> List[TradingExperience]:
    """
    Build experience for every measured outcome.

    All lookups are loaded once and indexed in memory rather than
    queried per outcome (§57): 6,510 outcomes with a per-row join would
    be an N+1 problem four times over.
    """
    outcomes = load_outcomes(conn, outcome_version=outcome_version,
                             since=since, limit=limit)
    attributions = load_attributions(conn, attribution_version=attribution_version)
    evidence = load_evidence_counts(conn, attribution_version=attribution_version)
    signals = load_decision_context(conn)
    instruments = load_instrument_context(conn)
    bands = load_unexpectedness(conn, outcome_version=outcome_version)

    from src.domain.memory_models import MIN_PATTERN_SAMPLE

    experiences = []
    for outcome in outcomes:
        key = (outcome["subject_kind"], outcome["subject_id"],
               outcome["horizon"])
        band = bands.get((outcome["subject_kind"], outcome["horizon"]))
        unexpected: Optional[bool] = None
        if band and outcome.get("simple_return") is not None:
            low, high, sample = band
            # Below the sample threshold the cohort cannot say what is
            # unusual, so nothing is claimed either way.
            if low is not None and high is not None and sample >= MIN_PATTERN_SAMPLE:
                unexpected = not (low <= outcome["simple_return"] <= high)

        experiences.append(build_experience(
            outcome,
            attribution=attributions.get(key),
            evidence_count=evidence.get(key, 0),
            signal_context=signals.get(outcome["subject_id"]),
            instrument_context=instruments.get(outcome.get("instrument_id") or ""),
            unexpected=unexpected,
            memory_version=memory_version,
            attribution_version=attribution_version))
    return experiences


def save(conn: sqlite3.Connection,
         experiences: Iterable[TradingExperience]) -> int:
    """
    Persist. `INSERT OR REPLACE` on the natural key (§54), so a rebuild
    replaces and cannot duplicate.

    Raw experience is append-oriented in spirit (§53): a rebuild under
    the SAME memory version reproduces byte-identical rows because every
    input is deterministic, and a CHANGE of meaning requires a new
    memory version, which writes new rows beside the old ones.
    """
    initialize_memory_schema(conn)

    def iso(value):
        return value.isoformat() if value else None

    written = 0
    for experience in experiences:
        context = experience.context
        conn.execute("""
            INSERT OR REPLACE INTO trading_experiences (
                experience_id, memory_version, kind, subject_kind, subject_id,
                horizon, outcome_method_version, attribution_method_version,
                trained_model_id, model_status, strategy_id, observation_id,
                information_cutoff, available_at, expected_direction,
                expected_return, expected_horizon, signal_confidence,
                signal_strength, actual_return, actual_direction,
                direction_result, mfe, mae, time_to_mfe_seconds,
                primary_error, contributing_errors, attribution_confidence,
                attribution_severity, evidence_count, context_schema_version,
                context_json, market_regime, event_type, instrument_id,
                asset_class, sector_id, experience_class, quality,
                notes_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                      ?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            experience.experience_id, experience.memory_version,
            experience.kind.value, experience.subject_kind,
            experience.subject_id, experience.horizon,
            experience.outcome_method_version,
            experience.attribution_method_version,
            experience.trained_model_id, experience.model_status,
            experience.strategy_id, experience.observation_id,
            iso(experience.information_cutoff), iso(experience.available_at),
            experience.expected_direction, experience.expected_return,
            experience.expected_horizon, experience.signal_confidence,
            experience.signal_strength, experience.actual_return,
            experience.actual_direction, experience.direction_result,
            experience.mfe, experience.mae, experience.time_to_mfe_seconds,
            experience.primary_error,
            json.dumps(experience.contributing_errors),
            experience.attribution_confidence, experience.attribution_severity,
            experience.evidence_count, context.schema_version,
            json.dumps(context.as_dict(), sort_keys=True),
            context.market_regime, context.event_type, context.instrument_id,
            context.asset_class, context.sector_id,
            experience.experience_class.value, experience.quality.value,
            json.dumps(experience.notes), iso(experience.created_at)))
        written += 1
    conn.commit()
    return written
