"""
src/attribution/analytics.py
--------------------------------------
Error profiles: what goes wrong, how often, and under what conditions.

STATISTICAL CAUTION IS BUILT IN, NOT ADDED ON (§62)
-------------------------------------------------------
Every profile row carries `sample_size` and a `small_sample` flag, and
the threshold is the same 30 the Phase 9 evaluator uses — reached
through Phase 19 rather than redefined, so one number governs "too
small to mean anything" across four phases.

`describe()` renders the language §62 asks for: *evidence suggests*
rather than *proved*, and it downgrades to "too few observations to
say" below the threshold rather than reporting a rate that looks like
a finding.

WHAT A RATE HERE DOES AND DOES NOT MEAN
-------------------------------------------
`error_rate` is *of the outcomes that could be assessed*. Outcomes that
were never measured, or whose layer had no evidence, are counted
separately in `not_assessable` and excluded from the denominator.

That matters more than it sounds. Six of the nine layers have no
evidence source in this database, and folding their silence into a
denominator would report an execution error rate of 0% — which reads
as "execution is flawless" and means "execution has never happened".
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.data_access.attribution_schema import initialize_attribution_schema
from src.domain.attribution_models import (
    ATTRIBUTION_METHOD_VERSION, MIN_COHORT_SAMPLE, ErrorType,
)

#: Cohorts the profiler builds (§28). Each is a column on
#: `error_attributions`, so adding one is a one-line change and cannot
#: drift from what was attributed.
PROFILES: Tuple[Tuple[str, Optional[str]], ...] = (
    ("overall", None),
    ("model", "trained_model_id"),
    ("model_status", "model_status"),
    ("strategy", "strategy_id"),
    ("instrument", "instrument_id"),
    ("direction", "expected_direction"),
    ("event_type", "event_type"),
    ("regime", "market_regime"),
    ("signal_status", "signal_status"),
    ("horizon", "horizon"),
    ("confidence_bucket", None),
    ("strength_bucket", None),
)

#: Reused from Phase 19 so confidence and strength keep the same
#: buckets across phases — and stay distinct from one another (§35, §36).
from src.outcomes.analytics import (  # noqa: E402
    CONFIDENCE_EDGES, STRENGTH_EDGES, bucket,
)


@dataclass
class Profile:
    """One cohort's error picture."""
    cohort_kind: str
    cohort_value: str
    #: Outcomes with a primary attribution that could be judged.
    assessed: int = 0
    #: Outcomes whose primary attribution was INSUFFICIENT_EVIDENCE.
    not_assessable: int = 0
    errors: int = 0
    no_error: int = 0
    expected_loss: int = 0
    unknown: int = 0
    requires_review: int = 0
    primary_counts: Dict[str, int] = field(default_factory=dict)
    contributing_counts: Dict[str, int] = field(default_factory=dict)

    @property
    def small_sample(self) -> bool:
        return self.assessed < MIN_COHORT_SAMPLE

    @property
    def error_rate(self) -> Optional[float]:
        """
        Errors as a share of ASSESSED outcomes.

        None when nothing could be assessed — not 0.0. A rate of zero
        asserts "nothing went wrong"; None says "we could not look",
        and the two are opposite claims.
        """
        return (self.errors / self.assessed) if self.assessed else None

    def describe(self) -> str:
        """
        The finding, in the register §62 asks for.

        Never "proved". Below the sample threshold it declines to give
        a rate at all, because a rate reads as a finding and eleven
        observations are not one.
        """
        if not self.assessed:
            return ("no outcome in this cohort could be assessed; nothing "
                    "can be said about it")
        if self.small_sample:
            return (f"only {self.assessed} assessed outcome(s) — too few to "
                    f"say anything about the error rate")
        rate = self.error_rate or 0.0
        top = sorted(self.primary_counts.items(), key=lambda kv: -kv[1])
        leading = top[0][0] if top else "none"
        return (f"evidence suggests a {rate:.0%} error rate over "
                f"{self.assessed} assessed outcome(s); the most frequent "
                f"primary finding is {leading}")

    def as_row(self, method_version: str) -> Dict[str, Any]:
        return {
            "method_version": method_version,
            "cohort_kind": self.cohort_kind, "cohort_value": self.cohort_value,
            "assessed": self.assessed, "not_assessable": self.not_assessable,
            "errors": self.errors, "no_error": self.no_error,
            "expected_loss": self.expected_loss, "unknown": self.unknown,
            "requires_review": self.requires_review,
            "error_rate": self.error_rate,
            "small_sample": self.small_sample,
            "primary_counts": dict(sorted(self.primary_counts.items())),
            "contributing_counts": dict(sorted(self.contributing_counts.items())),
            "description": self.describe(),
        }


_ATTR_COLUMNS = (
    "subject_kind", "subject_id", "horizon", "error_type", "role",
    "confidence", "severity", "status", "observability", "summary",
    "expected_direction", "expected_return", "realized_return", "deviation",
    "instrument_id", "trained_model_id", "model_status", "strategy_id",
    "market_regime", "event_type", "confidence_score", "strength",
    "signal_status",
)


def load_attributions(conn: sqlite3.Connection, *,
                      method_version: str = ATTRIBUTION_METHOD_VERSION,
                      observability: str = "observed") -> List[Dict[str, Any]]:
    """
    Every attribution for a methodology version.

    Filtered to OBSERVED by default. A counterfactual must never fall
    into a profile alongside history and be counted as something that
    happened (§24) — the caller has to ask for hypotheticals by name.
    """
    initialize_attribution_schema(conn)
    return [dict(zip(_ATTR_COLUMNS, row)) for row in conn.execute(f"""
        SELECT {', '.join(_ATTR_COLUMNS)} FROM error_attributions
        WHERE method_version = ? AND observability = ?
    """, (method_version, observability))]


def build_profiles(attributions: Sequence[Dict[str, Any]]) -> List[Profile]:
    """
    Group attributions into every cohort in `PROFILES`.

    Counts are taken from PRIMARY attributions for the headline numbers
    and separately for contributing ones, so "prediction error was the
    main cause" and "prediction error was involved" stay distinguishable
    — which is the whole point of §20.
    """
    grouped: Dict[Tuple[str, str], Profile] = {}

    def keys_for(row: Dict[str, Any]) -> List[Tuple[str, str]]:
        out: List[Tuple[str, str]] = []
        for kind, column in PROFILES:
            if kind == "overall":
                out.append((kind, "all"))
            elif kind == "confidence_bucket":
                out.append((kind, bucket(row.get("confidence_score"),
                                         CONFIDENCE_EDGES)))
            elif kind == "strength_bucket":
                out.append((kind, bucket(row.get("strength"), STRENGTH_EDGES)))
            else:
                value = row.get(column)
                out.append((kind, str(value) if value not in (None, "") else "unknown"))
        return out

    for row in attributions:
        error_type = row["error_type"]
        is_primary = row["role"] == "primary"
        for kind, value in keys_for(row):
            profile = grouped.get((kind, value))
            if profile is None:
                profile = Profile(cohort_kind=kind, cohort_value=value)
                grouped[(kind, value)] = profile

            if is_primary:
                if row["status"] == "insufficient_evidence":
                    profile.not_assessable += 1
                else:
                    profile.assessed += 1
                    if error_type == ErrorType.NO_ERROR.value:
                        profile.no_error += 1
                    elif error_type == ErrorType.EXPECTED_LOSS.value:
                        profile.expected_loss += 1
                    elif error_type == ErrorType.UNKNOWN.value:
                        profile.unknown += 1
                    else:
                        profile.errors += 1
                    profile.primary_counts[error_type] = (
                        profile.primary_counts.get(error_type, 0) + 1)
                if row["status"] == "requires_review":
                    profile.requires_review += 1
            else:
                profile.contributing_counts[error_type] = (
                    profile.contributing_counts.get(error_type, 0) + 1)

    return list(grouped.values())


def model_error_profile(conn: sqlite3.Connection, *,
                        method_version: str = ATTRIBUTION_METHOD_VERSION
                        ) -> List[Dict[str, Any]]:
    """Per model (§30): which failure modes recur, and on what sample."""
    rows = load_attributions(conn, method_version=method_version)
    profiles = {p.cohort_value: p for p in build_profiles(rows)
                if p.cohort_kind == "model"}
    return [profile.as_row(method_version)
            for profile in sorted(profiles.values(),
                                  key=lambda p: -p.assessed)]


def signal_error_profile(conn: sqlite3.Connection, *,
                         method_version: str = ATTRIBUTION_METHOD_VERSION
                         ) -> List[Dict[str, Any]]:
    """Per signal state (§31): suppressed, active, superseded, expired."""
    rows = load_attributions(conn, method_version=method_version)
    return [p.as_row(method_version) for p in build_profiles(rows)
            if p.cohort_kind == "signal_status"]


def regime_error_profile(conn: sqlite3.Connection, *,
                         method_version: str = ATTRIBUTION_METHOD_VERSION
                         ) -> List[Dict[str, Any]]:
    """Per regime (§32). Empty while `market_regime` is unpopulated."""
    rows = load_attributions(conn, method_version=method_version)
    return [p.as_row(method_version) for p in build_profiles(rows)
            if p.cohort_kind == "regime"]


def instrument_error_profile(conn: sqlite3.Connection, *,
                             method_version: str = ATTRIBUTION_METHOD_VERSION,
                             min_assessed: int = MIN_COHORT_SAMPLE
                             ) -> List[Dict[str, Any]]:
    """
    Per instrument (§33), above a sample floor by default.

    §33 warns against overfitting to one asset. The floor is a default
    rather than a filter baked into the query, so a caller can still
    ask for everything — and what comes back is flagged.
    """
    rows = load_attributions(conn, method_version=method_version)
    profiles = [p for p in build_profiles(rows)
                if p.cohort_kind == "instrument" and p.assessed >= min_assessed]
    return [p.as_row(method_version)
            for p in sorted(profiles, key=lambda p: -(p.error_rate or 0))]


def coverage(conn: sqlite3.Connection, *,
             method_version: str = ATTRIBUTION_METHOD_VERSION) -> Dict[str, Any]:
    """
    How much of the record could be diagnosed at all.

    Reported before any rate, for the same reason Phase 19 leads with
    outcome coverage: an error rate over a third of the data means
    something different from one over all of it.
    """
    initialize_attribution_schema(conn)
    by_status = dict(conn.execute("""
        SELECT status, COUNT(*) FROM error_attributions
        WHERE method_version = ? AND role = 'primary' AND observability='observed'
        GROUP BY status
    """, (method_version,)))
    total = sum(by_status.values())
    assessable = total - by_status.get("insufficient_evidence", 0)
    return {
        "method_version": method_version,
        "subjects": total,
        "assessable": assessable,
        "coverage": (assessable / total) if total else None,
        "by_status": by_status,
        "by_primary_type": dict(conn.execute("""
            SELECT error_type, COUNT(*) FROM error_attributions
            WHERE method_version = ? AND role = 'primary'
              AND observability = 'observed'
            GROUP BY error_type ORDER BY 2 DESC
        """, (method_version,))),
        "by_contributing_type": dict(conn.execute("""
            SELECT error_type, COUNT(*) FROM error_attributions
            WHERE method_version = ? AND role = 'contributing'
              AND observability = 'observed'
            GROUP BY error_type ORDER BY 2 DESC
        """, (method_version,))),
        "by_confidence": dict(conn.execute("""
            SELECT confidence, COUNT(*) FROM error_attributions
            WHERE method_version = ? AND role = 'primary'
            GROUP BY confidence
        """, (method_version,))),
        "by_severity": dict(conn.execute("""
            SELECT severity, COUNT(*) FROM error_attributions
            WHERE method_version = ? AND role = 'primary'
            GROUP BY severity
        """, (method_version,))),
        "review_open": conn.execute("""
            SELECT COUNT(*) FROM attribution_review_queue
            WHERE method_version = ? AND state = 'open'
        """, (method_version,)).fetchone()[0],
        "evidence_rows": conn.execute("""
            SELECT COUNT(*) FROM attribution_evidence WHERE method_version = ?
        """, (method_version,)).fetchone()[0],
    }


def integrity_check(conn: sqlite3.Connection, *,
                    method_version: str = ATTRIBUTION_METHOD_VERSION
                    ) -> Dict[str, int]:
    """
    §67, as a query rather than a promise.

    Every count here must be zero. Run by the CLI at the end of each
    pass and asserted by a test, because "every attribution references
    a valid outcome" is the sort of claim that is true until one day it
    quietly is not.
    """
    initialize_attribution_schema(conn)
    valid_types = {member.value for member in ErrorType}
    valid_confidence = {"high", "medium", "low", "insufficient_evidence"}

    def scalar(sql: str, params: tuple = ()) -> int:
        try:
            return conn.execute(sql, params).fetchone()[0]
        except sqlite3.OperationalError:
            return 0

    placeholders = ",".join("?" * len(valid_types))
    confidence_placeholders = ",".join("?" * len(valid_confidence))
    return {
        "orphan_attributions": scalar(f"""
            SELECT COUNT(*) FROM error_attributions a
            WHERE a.method_version = ? AND NOT EXISTS (
                SELECT 1 FROM outcome_measurements o
                WHERE o.subject_kind = a.subject_kind
                  AND o.subject_id = a.subject_id AND o.horizon = a.horizon)
        """, (method_version,)),
        "attributions_without_evidence": scalar("""
            SELECT COUNT(*) FROM error_attributions a
            WHERE a.method_version = ? AND NOT EXISTS (
                SELECT 1 FROM attribution_evidence e
                WHERE e.subject_kind = a.subject_kind
                  AND e.subject_id = a.subject_id AND e.horizon = a.horizon
                  AND e.method_version = a.method_version
                  AND e.error_type = a.error_type)
        """, (method_version,)),
        "orphan_evidence": scalar("""
            SELECT COUNT(*) FROM attribution_evidence e
            WHERE e.method_version = ? AND NOT EXISTS (
                SELECT 1 FROM error_attributions a
                WHERE a.subject_kind = e.subject_kind
                  AND a.subject_id = e.subject_id AND a.horizon = e.horizon
                  AND a.method_version = e.method_version
                  AND a.error_type = e.error_type)
        """, (method_version,)),
        "invalid_error_type": scalar(f"""
            SELECT COUNT(*) FROM error_attributions
            WHERE method_version = ? AND error_type NOT IN ({placeholders})
        """, (method_version, *sorted(valid_types))),
        "invalid_confidence": scalar(f"""
            SELECT COUNT(*) FROM error_attributions
            WHERE method_version = ? AND confidence NOT IN ({confidence_placeholders})
        """, (method_version, *sorted(valid_confidence))),
        "subjects_without_a_primary": scalar("""
            SELECT COUNT(*) FROM (
                SELECT subject_kind, subject_id, horizon
                FROM error_attributions WHERE method_version = ?
                GROUP BY 1,2,3
                HAVING SUM(CASE WHEN role='primary' THEN 1 ELSE 0 END) != 1)
        """, (method_version,)),
        "hypothetical_counted_as_observed": scalar("""
            SELECT COUNT(*) FROM error_attributions
            WHERE method_version = ? AND observability NOT IN ('observed','hypothetical')
        """, (method_version,)),
    }
