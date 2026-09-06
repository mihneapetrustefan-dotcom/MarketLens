"""
src/memory/patterns.py
------------------------------
Deterministic pattern extraction: transparent aggregation, not learning.

WHAT A PATTERN IS AND IS NOT
--------------------------------
A pattern says: *these conditions co-occurred with these outcomes, this
many times, over this window.*

It does not say one caused the other. It does not say the next
occurrence will match. §0 and §23 forbid both, and the phrasing in
`MemoryPattern.describe()` is written so that a surface rendering it
cannot accidentally imply either.

No machine learning is used. §23 asks for transparent aggregation
first, and there is a stronger reason: a pattern discovered by a model
would need its own validation, its own leakage story and its own
version, and none of that exists yet. Grouping by stated conditions and
counting is auditable by hand.

THE COMBINATORIAL PROBLEM, AND THE HONEST ANSWER
----------------------------------------------------
Crossing every context dimension produces thousands of cohorts, and at
27 days of history almost all of them hold three experiences. §51 warns
against precomputing every combination and §26 forbids manufacturing
confidence, so:

  * only stated `PATTERN_DEFINITIONS` are built, not the full cross
    product;
  * every pattern carries its sample size and a quality state;
  * below `MIN_PATTERN_SAMPLE` a pattern is WEAK and `describe()`
    refuses to quote a rate at all.

Most patterns in this database will be WEAK. That is the correct
result for a month of data, and reporting it is the point.

EXPERIMENTAL EXPERIENCE IS NEVER POOLED (§6)
------------------------------------------------
Patterns are built from validated experience by default. Experimental
experience — everything produced by an unpromoted model, which today is
all of it — is aggregated separately and `experimental_count` says so
on every row. A pattern that silently mixed the two would describe
research and read as production.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.data_access.memory_schema import initialize_memory_schema
from src.domain.memory_models import (
    MEMORY_METHOD_VERSION, MIN_PATTERN_SAMPLE, STALENESS_DAYS,
    CONTEXT_SCHEMA_VERSION, MemoryConfidence, MemoryPattern, PatternPeriod,
    PatternQuality, assess_confidence, assess_stability, pattern_id_for,
    summarise_distribution,
)

#: The pattern families built, each a tuple of experience columns.
#: Stated rather than generated from a cross product: §51 warns against
#: precomputing every combination, and at this history depth the cross
#: product would be tens of thousands of cohorts of three.
PATTERN_DEFINITIONS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("signal_direction_horizon", ("expected_direction", "horizon")),
    ("event_direction", ("event_type", "expected_direction")),
    ("event_horizon", ("event_type", "horizon")),
    ("model_horizon", ("trained_model_id", "horizon")),
    ("instrument_direction", ("instrument_id", "expected_direction")),
    ("asset_class_direction", ("asset_class", "expected_direction")),
    ("regime_direction", ("market_regime", "expected_direction")),
    ("regime_horizon", ("market_regime", "horizon")),
    ("strategy_horizon", ("strategy_id", "horizon")),
    ("error_horizon", ("primary_error", "horizon")),
    ("sector_direction", ("sector_id", "expected_direction")),
)

#: Sub-periods for stability. Weekly, because the record is 27 days
#: deep — monthly would give one period and no comparison at all.
#: `assess_stability` still reports `insufficient_history` unless two
#: periods each clear the sample threshold, which at this depth they
#: usually will not.
STABILITY_PERIOD_DAYS = 7


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


_EXPERIENCE_COLUMNS = (
    "experience_id", "subject_kind", "subject_id", "horizon", "quality",
    "experience_class", "expected_direction", "expected_return",
    "actual_return", "direction_result", "mfe", "mae", "primary_error",
    "contributing_errors", "trained_model_id", "model_status", "strategy_id",
    "instrument_id", "asset_class", "sector_id", "event_type",
    "market_regime", "signal_confidence", "signal_strength", "available_at",
)


def load_experiences(conn: sqlite3.Connection, *,
                     memory_version: str = MEMORY_METHOD_VERSION,
                     as_of: Optional[str] = None,
                     qualities: Sequence[str] = ("validated", "experimental")
                     ) -> List[Dict[str, Any]]:
    """
    Experience rows, optionally restricted to what was knowable by
    `as_of` (§38).

    The `available_at <= as_of` filter is the point-in-time guarantee.
    Rows with no `available_at` — incomplete experiences — are excluded
    from an as-of query entirely, because an experience with no moment
    of becoming knowable cannot be placed in time.
    """
    initialize_memory_schema(conn)
    placeholders = ",".join("?" * len(qualities))
    sql = (f"SELECT {', '.join(_EXPERIENCE_COLUMNS)} FROM trading_experiences "
           f"WHERE memory_version = ? AND quality IN ({placeholders})")
    params: List[Any] = [memory_version, *qualities]
    if as_of:
        sql += " AND available_at IS NOT NULL AND available_at <= ?"
        params.append(as_of)
    sql += " ORDER BY available_at, experience_id"
    return [dict(zip(_EXPERIENCE_COLUMNS, row))
            for row in conn.execute(sql, params)]


def _period_label(moment: Optional[datetime],
                  anchor: Optional[datetime]) -> str:
    if moment is None or anchor is None:
        return "unknown"
    index = int((moment - anchor).days // STABILITY_PERIOD_DAYS)
    start = anchor + timedelta(days=index * STABILITY_PERIOD_DAYS)
    return f"{start.date().isoformat()}+{STABILITY_PERIOD_DAYS}d"


def _regime_breakdown(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Per-regime numbers, stored beside the all-regime ones (§30).

    Never collapsed into a single score: a pattern that works in one
    regime and fails in another is context-dependent, and one number
    describes neither half.
    """
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("market_regime") or "unknown")].append(row)
    out: Dict[str, Any] = {}
    for regime, members in grouped.items():
        decided = [m for m in members if m["direction_result"] in ("hit", "miss")]
        returns = [m["actual_return"] for m in members
                   if m["actual_return"] is not None]
        out[regime] = {
            "sample_size": len(members),
            "hit_rate": (sum(1 for m in decided if m["direction_result"] == "hit")
                         / len(decided)) if decided else None,
            "mean_return": (sum(returns) / len(returns)) if returns else None,
        }
    return out


def _detect_conflict(regime_breakdown: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Do sub-populations of this pattern disagree (§31, §32)?

    §31 is explicit: do not average blindly. Two regimes with hit rates
    on opposite sides of a coin flip are not one pattern with noise;
    they are a context-dependent pattern, and the average describes
    neither.

    Only regimes that individually clear the sample threshold count.
    Without that, one regime with four observations would declare every
    pattern conflicted.
    """
    usable = {name: data for name, data in regime_breakdown.items()
              if name != "unknown"
              and (data.get("sample_size") or 0) >= MIN_PATTERN_SAMPLE
              and data.get("hit_rate") is not None}
    if len(usable) < 2:
        return False, []
    rates = {name: data["hit_rate"] for name, data in usable.items()}
    best, worst = max(rates, key=rates.get), min(rates, key=rates.get)
    if rates[best] > 0.5 >= rates[worst]:
        return True, [
            f"context-dependent: {best} {rates[best]:.0%} versus {worst} "
            f"{rates[worst]:.0%}. Averaging these would describe neither."]
    return False, []


def build_pattern(pattern_type: str, conditions: Dict[str, Any],
                  rows: Sequence[Dict[str, Any]], *,
                  memory_version: str = MEMORY_METHOD_VERSION,
                  now: Optional[datetime] = None) -> MemoryPattern:
    """One cohort, summarised — with every caveat it has earned."""
    now = now or datetime.now(timezone.utc)
    pattern = MemoryPattern(
        pattern_id=pattern_id_for(pattern_type, conditions, memory_version),
        pattern_type=pattern_type, conditions=dict(conditions),
        memory_version=memory_version,
        context_schema_version=CONTEXT_SCHEMA_VERSION,
        sample_size=len(rows),
        instrument_count=len({r["instrument_id"] for r in rows
                              if r.get("instrument_id")}),
        experiment_count=sum(1 for r in rows if r.get("quality") == "experimental"))

    for row in rows:
        result = row.get("direction_result")
        if result == "hit":
            pattern.hits += 1
        elif result == "miss":
            pattern.misses += 1
        elif result == "neutral":
            pattern.neutrals += 1

    if pattern.decided:
        pattern.hit_rate = pattern.hits / pattern.decided

    returns = [r["actual_return"] for r in rows if r["actual_return"] is not None]
    stats = summarise_distribution(returns)
    pattern.mean_return = stats["mean"]
    pattern.median_return = stats["median"]
    pattern.stdev_return = stats["stdev"]

    mfes = [r["mfe"] for r in rows if r.get("mfe") is not None]
    maes = [r["mae"] for r in rows if r.get("mae") is not None]
    pattern.mean_mfe = (sum(mfes) / len(mfes)) if mfes else None
    pattern.mean_mae = (sum(maes) / len(maes)) if maes else None

    pattern.error_counts = dict(Counter(
        r["primary_error"] for r in rows if r.get("primary_error")))
    pattern.class_counts = dict(Counter(
        r["experience_class"] for r in rows if r.get("experience_class")))

    moments = sorted(m for m in (_parse(r.get("available_at")) for r in rows)
                     if m is not None)
    if moments:
        pattern.first_seen, pattern.last_seen = moments[0], moments[-1]

    # ---- stability across sub-periods (§29) -------------------------
    anchor = pattern.first_seen
    periods: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        periods[_period_label(_parse(row.get("available_at")), anchor)].append(row)
    for label in sorted(periods):
        members = periods[label]
        decided = [m for m in members if m["direction_result"] in ("hit", "miss")]
        window_returns = [m["actual_return"] for m in members
                          if m["actual_return"] is not None]
        pattern.periods.append(PatternPeriod(
            label=label, sample_size=len(members),
            hit_rate=(sum(1 for m in decided if m["direction_result"] == "hit")
                      / len(decided)) if decided else None,
            mean_return=(sum(window_returns) / len(window_returns))
            if window_returns else None))

    pattern.stability, stability_notes = assess_stability(pattern.periods)
    pattern.notes.extend(stability_notes)

    # ---- regime dependence and conflict (§30, §31) ------------------
    pattern.regime_breakdown = _regime_breakdown(rows)
    conflicting, conflict_notes = _detect_conflict(pattern.regime_breakdown)
    pattern.contradictions.extend(conflict_notes)

    pattern.confidence = assess_confidence(
        sample_size=pattern.sample_size, stability=pattern.stability,
        stdev_return=pattern.stdev_return, conflicting=conflicting)

    # ---- quality state (§33) ----------------------------------------
    age = ((now - pattern.last_seen).days
           if pattern.last_seen else None)
    if conflicting:
        pattern.quality = PatternQuality.CONFLICTING
    elif pattern.sample_size < MIN_PATTERN_SAMPLE:
        pattern.quality = PatternQuality.WEAK
        pattern.notes.append(
            f"{pattern.sample_size} experience(s), under the "
            f"{MIN_PATTERN_SAMPLE} needed to describe a regularity")
    elif pattern.stability == "unstable":
        pattern.quality = PatternQuality.UNSTABLE
    elif age is not None and age > STALENESS_DAYS:
        pattern.quality = PatternQuality.STALE
        pattern.notes.append(
            f"the most recent supporting experience is {age} days old. "
            f"Kept, not deleted: historical existence and current relevance "
            f"are different things (§49)")
    elif pattern.stability == "insufficient_history":
        # Enough observations, not enough calendar. Worth flagging for a
        # person rather than presenting as confirmed.
        pattern.quality = PatternQuality.REQUIRES_REVIEW
        pattern.notes.append(
            "enough observations but too little history to know whether this "
            "holds outside the window it was measured in")
    else:
        pattern.quality = PatternQuality.CONFIRMED

    if pattern.experiment_count:
        pattern.notes.append(
            f"{pattern.experiment_count} of {pattern.sample_size} supporting "
            f"experience(s) came from an unpromoted model — research "
            f"experience, not production")
    return pattern


def build_all(conn: sqlite3.Connection, *,
              memory_version: str = MEMORY_METHOD_VERSION,
              as_of: Optional[str] = None,
              qualities: Sequence[str] = ("validated", "experimental"),
              now: Optional[datetime] = None
              ) -> Tuple[List[MemoryPattern], Dict[str, List[str]]]:
    """
    Every stated pattern family over the eligible experience.

    Returns `(patterns, evidence)` where evidence maps each pattern to
    the experience ids that formed it (§25) — no pattern is ever stored
    without them.
    """
    rows = load_experiences(conn, memory_version=memory_version,
                            as_of=as_of, qualities=qualities)
    patterns: List[MemoryPattern] = []
    evidence: Dict[str, List[str]] = {}

    for pattern_type, columns in PATTERN_DEFINITIONS:
        grouped: Dict[Tuple, List[Dict[str, Any]]] = defaultdict(list)
        for row in rows:
            key = tuple(row.get(column) for column in columns)
            # A cohort keyed on a missing dimension describes nothing.
            # Skipping is honest; "unknown" cohorts would be the largest
            # patterns in the database and would mean nothing.
            if any(value in (None, "") for value in key):
                continue
            grouped[key].append(row)

        for key, members in grouped.items():
            conditions = dict(zip(columns, key))
            pattern = build_pattern(pattern_type, conditions, members,
                                    memory_version=memory_version, now=now)
            patterns.append(pattern)
            evidence[pattern.pattern_id] = [m["experience_id"] for m in members]

    return patterns, evidence


def find_contradictions(patterns: Sequence[MemoryPattern]) -> List[Dict[str, Any]]:
    """
    Patterns of the same family whose conditions overlap and whose
    outcomes disagree (§32).

    Surfaced rather than resolved. §32 says contradictions are useful
    information for future research, and picking a winner would discard
    exactly the observation worth keeping.
    """
    found: List[Dict[str, Any]] = []
    by_type: Dict[str, List[MemoryPattern]] = defaultdict(list)
    for pattern in patterns:
        if (pattern.sample_size >= MIN_PATTERN_SAMPLE
                and pattern.hit_rate is not None):
            by_type[pattern.pattern_type].append(pattern)

    for pattern_type, members in by_type.items():
        for index, left in enumerate(members):
            for right in members[index + 1:]:
                shared = {k: v for k, v in left.conditions.items()
                          if right.conditions.get(k) == v}
                if not shared:
                    continue
                # Checked in BOTH orderings. The first version compared
                # only `left > 0.5 >= right`, so whether a genuine
                # disagreement was reported depended on the order the
                # two patterns happened to come out of the grouping —
                # a contradiction that appears or vanishes with a sort
                # is worse than one that is never reported at all.
                better, worse = ((left, right)
                                 if left.hit_rate >= right.hit_rate
                                 else (right, left))
                if not (better.hit_rate > 0.5 >= worse.hit_rate):
                    continue
                found.append({
                    "pattern_type": pattern_type,
                    "shared_conditions": shared,
                    "left": better.pattern_id,
                    "left_conditions": better.conditions,
                    "left_hit_rate": better.hit_rate,
                    "left_sample": better.sample_size,
                    "right": worse.pattern_id,
                    "right_conditions": worse.conditions,
                    "right_hit_rate": worse.hit_rate,
                    "right_sample": worse.sample_size,
                    "note": ("these share conditions and disagree. Kept "
                             "visible rather than resolved: which one "
                             "generalises is a research question, not a "
                             "tie-break."),
                })
    return found


def save(conn: sqlite3.Connection, patterns: Sequence[MemoryPattern],
         evidence: Dict[str, List[str]], *,
         memory_version: str = MEMORY_METHOD_VERSION,
         available_at: Optional[Dict[str, Optional[str]]] = None) -> int:
    """
    Persist patterns and the experiences behind them, together.

    A pattern is deleted and rewritten with its evidence in one pass, so
    a rebuild cannot leave a conclusion pointing at evidence that no
    longer supports it.
    """
    initialize_memory_schema(conn)
    available_at = available_at or {}

    def iso(value):
        return value.isoformat() if value else None

    conn.execute("DELETE FROM memory_pattern_evidence WHERE memory_version = ?",
                 (memory_version,))
    conn.execute("DELETE FROM memory_patterns WHERE memory_version = ?",
                 (memory_version,))

    for pattern in patterns:
        conn.execute("""
            INSERT OR REPLACE INTO memory_patterns (
                pattern_id, memory_version, context_schema_version,
                pattern_type, conditions_json, sample_size, instrument_count,
                experimental_count, hits, misses, neutrals, hit_rate,
                mean_return, median_return, stdev_return, mean_mfe, mean_mae,
                error_counts_json, class_counts_json, first_seen, last_seen,
                periods_json, stability, regime_breakdown_json,
                contradictions_json, quality, confidence, notes_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            pattern.pattern_id, pattern.memory_version,
            pattern.context_schema_version, pattern.pattern_type,
            json.dumps(pattern.conditions, sort_keys=True, default=str),
            pattern.sample_size, pattern.instrument_count,
            pattern.experiment_count, pattern.hits, pattern.misses,
            pattern.neutrals, pattern.hit_rate, pattern.mean_return,
            pattern.median_return, pattern.stdev_return, pattern.mean_mfe,
            pattern.mean_mae,
            json.dumps(pattern.error_counts, sort_keys=True),
            json.dumps(pattern.class_counts, sort_keys=True),
            iso(pattern.first_seen), iso(pattern.last_seen),
            json.dumps([p.as_dict() for p in pattern.periods]),
            pattern.stability,
            json.dumps(pattern.regime_breakdown, sort_keys=True),
            json.dumps(pattern.contradictions),
            pattern.quality.value, pattern.confidence.value,
            json.dumps(pattern.notes), iso(pattern.created_at)))

        rows = [(pattern.pattern_id, memory_version, experience_id,
                 available_at.get(experience_id))
                for experience_id in evidence.get(pattern.pattern_id, ())]
        if rows:
            conn.executemany("""
                INSERT OR REPLACE INTO memory_pattern_evidence
                (pattern_id, memory_version, experience_id, available_at)
                VALUES (?,?,?,?)
            """, rows)
    conn.commit()
    return len(patterns)
