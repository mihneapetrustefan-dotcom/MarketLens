"""
src/autoresearch/governance.py
----------------------------------------
Phase 23 §21-§25, §74 — the rules that stop a researcher fooling itself.

Four separate defences live here, and they guard four different
mistakes:

1. **Decision-time leakage** (§74). A cohort defined on a field that
   only exists AFTER the outcome cannot be a filter. This one is not
   hypothetical: the first triage run on real data produced two
   top-priority questions asking whether excluding signals whose
   `primary_error` is `prediction_error` improves accuracy. That
   sounds reasonable and is unimplementable — `primary_error` is Phase
   20's verdict about what went wrong, knowable only once the outcome
   is in. A filter cannot consult it, and a backtest that does will
   look superb.

   46 of this database's memory patterns are keyed on `primary_error`.
   Without this check every one of them was a candidate research
   question.

2. **Protected test regions** (§23). Data the autonomous researcher may
   not touch at all, so that something is left to be wrong against.

3. **Data snooping** (§22). A ledger counting how often each window has
   been tested. A researcher permitted to retune against the same
   held-out period will eventually find something, and no statistical
   correction applied afterwards can undo it.

4. **Multiple testing** (§21). Counting hypotheses, variants and
   comparisons so a conclusion can state how many attempts stand
   behind it.

WHY THESE ARE REFUSALS RATHER THAN WARNINGS
-----------------------------------------------
A warning attached to a result is read once and then read past. A
refusal changes what gets run. Leakage and protected-window violations
raise; snooping and multiple-testing produce numbers that travel with
the conclusion.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.data_access.autoresearch_schema import initialize_autoresearch_schema
from src.domain.autoresearch_models import _digest, utcnow


class LeakageRefused(Exception):
    """A hypothesis would require knowing the future. Never softened."""


class ProtectedWindowRefused(Exception):
    """Research tried to reach into a region reserved from it (§23)."""


# ======================================================================
# 1. Decision-time knowability
# ======================================================================

#: Fields a decision could actually consult at the moment it is made.
#: Everything here is either context (what was true) or configuration
#: (what the system chose); nothing is derived from the outcome.
DECISION_TIME_FIELDS = frozenset({
    "event_type", "expected_direction", "expected_return", "horizon",
    "asset_class", "sector_id", "instrument_id", "market_regime",
    "signal_strength", "signal_confidence", "confidence_score",
    "strategy_id", "trained_model_id", "model_status", "subject_kind",
    "quality", "threshold", "direction", "min_strength", "min_confidence",
})

#: Fields that exist only because the outcome is already known. A
#: cohort keyed on any of these describes hindsight, and a filter built
#: from it cannot be run forward.
OUTCOME_DERIVED_FIELDS = frozenset({
    "primary_error", "contributing_errors", "direction_result",
    "experience_class", "actual_return", "realized_return", "deviation",
    "mfe", "mae", "hit_rate", "was_correct", "error_type", "severity",
    "outcome_status", "window_end",
})


def leaking_fields(condition: Dict[str, Any]) -> List[str]:
    """
    Which parts of a cohort definition are only knowable afterwards.

    Unknown field names are NOT treated as leaking. Guessing would make
    the check unpredictable as the schema grows, and a false refusal
    trains people to disable the guard. Unknown names are reported
    separately by `unknown_fields` so they can be classified
    deliberately.
    """
    return sorted(key for key in condition
                  if key in OUTCOME_DERIVED_FIELDS)


def unknown_fields(condition: Dict[str, Any]) -> List[str]:
    """Condition keys classified neither way — for a human to place."""
    return sorted(key for key in condition
                  if key not in DECISION_TIME_FIELDS
                  and key not in OUTCOME_DERIVED_FIELDS)


def assert_decision_time(condition: Dict[str, Any]) -> None:
    """Raise if a cohort could not be identified at decision time."""
    leaking = leaking_fields(condition)
    if leaking:
        raise LeakageRefused(
            "cohort is defined on %s, which %s only knowable after the "
            "outcome. A filter cannot consult it at decision time, so any "
            "improvement measured this way is hindsight rather than a "
            "result." % (", ".join(leaking),
                         "are" if len(leaking) > 1 else "is"))


# ======================================================================
# 2. Protected windows
# ======================================================================

def declare_window(conn: sqlite3.Connection, *, label: str,
                   starts_at: str, ends_at: str,
                   policy: str = "protected", reason: str = "") -> str:
    """
    Reserve a period from autonomous research (§23).

    `protected` means the researcher may not evaluate on it at all.
    `monitored` means it may, and every touch is counted.
    """
    if policy not in ("protected", "monitored"):
        raise ValueError("policy must be 'protected' or 'monitored'")
    initialize_autoresearch_schema(conn)
    window_id = "win-" + _digest({"label": label, "starts": starts_at,
                                  "ends": ends_at})[:16]
    conn.execute("""
        INSERT OR REPLACE INTO autoresearch_protected_windows (
            window_id, label, starts_at, ends_at, policy, reason, created_at
        ) VALUES (?,?,?,?,?,?,?)
    """, (window_id, label, starts_at, ends_at, policy, reason, utcnow()))
    conn.commit()
    return window_id


def protected_windows(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    initialize_autoresearch_schema(conn)
    keys = ("window_id", "label", "starts_at", "ends_at", "policy", "reason")
    return [dict(zip(keys, row)) for row in conn.execute("""
        SELECT window_id, label, starts_at, ends_at, policy, reason
        FROM autoresearch_protected_windows ORDER BY starts_at
    """)]


def assert_window_allowed(conn: sqlite3.Connection, *,
                          starts_at: Optional[str],
                          ends_at: Optional[str]) -> None:
    """
    Refuse research that would evaluate inside a protected region.

    Overlap, not containment: a cohort that merely clips the edge of a
    protected window has still seen part of it, and "only a little" is
    not a property that survives repetition.
    """
    if not starts_at or not ends_at:
        return
    for window in protected_windows(conn):
        if window["policy"] != "protected":
            continue
        if starts_at <= window["ends_at"] and ends_at >= window["starts_at"]:
            raise ProtectedWindowRefused(
                "the requested period %s..%s overlaps protected window %r "
                "(%s..%s). %s"
                % (starts_at, ends_at, window["label"], window["starts_at"],
                   window["ends_at"],
                   window["reason"] or "This region is reserved so that "
                   "something remains for a final, un-tuned-against test."))


# ======================================================================
# 3. Data snooping ledger
# ======================================================================

def window_key(starts_at: Optional[str], ends_at: Optional[str]) -> str:
    """A stable key for an evaluation period, to the day."""
    return "%s..%s" % ((starts_at or "")[:10], (ends_at or "")[:10])


def record_window_use(conn: sqlite3.Connection, *, starts_at: Optional[str],
                      ends_at: Optional[str], hypothesis_id: str = "",
                      experiment_id: str = "", family_id: str = "",
                      cycle_id: Optional[str] = None) -> str:
    """Count one touch of an evaluation period (§22)."""
    initialize_autoresearch_schema(conn)
    key = window_key(starts_at, ends_at)
    usage_id = "use-" + _digest({"key": key, "h": hypothesis_id,
                                 "e": experiment_id, "t": utcnow()})[:20]
    conn.execute("""
        INSERT OR REPLACE INTO autoresearch_window_usage (
            usage_id, window_key, hypothesis_id, experiment_id, family_id,
            cycle_id, used_at
        ) VALUES (?,?,?,?,?,?,?)
    """, (usage_id, key, hypothesis_id, experiment_id, family_id, cycle_id,
          utcnow()))
    conn.commit()
    return usage_id


def window_use_count(conn: sqlite3.Connection, starts_at: Optional[str],
                     ends_at: Optional[str]) -> int:
    initialize_autoresearch_schema(conn)
    return conn.execute(
        "SELECT COUNT(*) FROM autoresearch_window_usage WHERE window_key = ?",
        (window_key(starts_at, ends_at),)).fetchone()[0]


def snooping_report(conn: sqlite3.Connection, *, limit: int = 20
                    ) -> List[Dict[str, Any]]:
    """
    Which evaluation periods have been tested how often (§22).

    A window with a high count is not proof of anything wrong. It is
    the number a reader needs in order to discount a result found on
    the fortieth pass over the same fortnight.
    """
    initialize_autoresearch_schema(conn)
    return [{"window": row[0], "uses": row[1], "distinct_hypotheses": row[2]}
            for row in conn.execute("""
        SELECT window_key, COUNT(*), COUNT(DISTINCT hypothesis_id)
        FROM autoresearch_window_usage
        GROUP BY window_key ORDER BY 2 DESC LIMIT ?
    """, (limit,))]


# ======================================================================
# 4. Multiple testing
# ======================================================================

def multiple_testing_state(conn: sqlite3.Connection,
                           family_id: str = "") -> Dict[str, Any]:
    """
    How many attempts stand behind a conclusion (§21).

    Counts hypotheses, the experiments they became, and the distinct
    comparisons those experiments make. The last is the one that
    matters: Phase 22 found three differently-named experiments making
    one comparison, and a family count would have called that three
    attempts when it was one.
    """
    initialize_autoresearch_schema(conn)
    where = "WHERE family_id = ?" if family_id else ""
    params: Tuple[Any, ...] = (family_id,) if family_id else ()

    hypotheses = conn.execute(
        "SELECT COUNT(*) FROM autoresearch_hypotheses " + where,
        params).fetchone()[0]
    distinct_claims = conn.execute(
        "SELECT COUNT(DISTINCT claim_fingerprint) FROM autoresearch_hypotheses "
        + where, params).fetchone()[0]
    experiments = conn.execute(
        "SELECT COUNT(*) FROM autoresearch_hypotheses "
        + (where + " AND " if where else "WHERE ")
        + "experiment_id IS NOT NULL AND experiment_id != ''",
        params).fetchone()[0]

    return {
        "hypotheses": hypotheses,
        "distinct_claims": distinct_claims,
        "repeated_claims": max(0, hypotheses - distinct_claims),
        "experiments": experiments,
        "note": _multiple_testing_note(hypotheses, distinct_claims),
    }


def _multiple_testing_note(hypotheses: int, distinct_claims: int) -> str:
    if hypotheses <= 1:
        return ("one hypothesis, so no multiple-testing correction applies. "
                "That stops being true as soon as a second is tested.")
    repeated = hypotheses - distinct_claims
    base = ("%d hypotheses have been tested here; with that many attempts, "
            "one apparently significant result is expected by chance alone."
            % hypotheses)
    if repeated:
        base += (" %d of them repeat a claim another already makes, so the "
                 "evidence is thinner than the count suggests." % repeated)
    return base
