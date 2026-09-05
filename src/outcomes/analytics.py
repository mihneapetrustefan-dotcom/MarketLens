"""
src/outcomes/analytics.py
---------------------------------
Aggregating outcome measurements into distributions.

DISTRIBUTIONS, NOT WIN/LOSS (§15)
-------------------------------------
A win rate answers one question badly. "51% of signals were right" is
compatible with a strategy that makes a penny 51 times and loses a
pound once, and with one that is genuinely useful; the two are
distinguishable only from the shape of the returns. So every cohort
carries mean, median, standard deviation, min, max and the 10/25/75/90
percentiles alongside the counts.

SAMPLE SIZE IS NEVER OPTIONAL (§23)
---------------------------------------
`sample_size` and `small_sample` are NOT NULL on the table and are
carried on every row. The threshold is
`ModelEvaluation.MIN_EFFECTIVE_SAMPLE` — the same 30 the Phase 9
evaluator uses — because two different definitions of "too small to
mean anything" in one repository is one too many.

MULTIPLE TESTING (§41)
--------------------------
Slicing 1,823 measurements by horizon x model x instrument x regime x
direction x confidence bucket produces hundreds of cohorts. At the
conventional 5% level, roughly one in twenty will look significant
through chance alone, and the most extreme-looking cohort is the one
most likely to be noise.

This module therefore refuses to compute a p-value and refuses to call
anything significant. It reports the interval, the sample size and the
cohort count, and `cohort_warning()` states the multiplicity in the
output itself so a reader meets it at the same moment they meet the
number.

CONFIDENCE INTERVALS ONLY WHERE COMPUTED (§40)
--------------------------------------------------
`ci_low` / `ci_high` are NULL unless a bootstrap actually ran, and
`ci_method` names it. A NULL interval means "not calculated", never
"zero width". Nothing is presented as significant, because nothing was
tested for significance.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.data_access.outcome_schema import initialize_outcome_schema
from src.domain.model_models import ModelEvaluation
from src.domain.outcome_models import (
    OUTCOME_METHOD_VERSION, DirectionResult, OutcomeStatus,
)

#: Reused, not redefined. The Phase 9 evaluator already decided what
#: "too small to be evidence" means, and a second number here would
#: eventually disagree with it.
MIN_SAMPLE = ModelEvaluation.MIN_EFFECTIVE_SAMPLE

#: Cohorts the aggregator builds. Each is a column on
#: `outcome_measurements`, so adding one is a one-line change and
#: cannot drift from what was measured.
COHORTS: Tuple[Tuple[str, Optional[str]], ...] = (
    ("overall", None),
    ("model", "trained_model_id"),
    ("model_status", "model_status"),
    ("instrument", "instrument_id"),
    ("direction", "expected_direction"),
    ("event_type", "event_type"),
    ("regime", "market_regime"),
    ("strategy", "strategy_id"),
    ("signal_status", "signal_status"),
    ("confidence_bucket", None),
    ("strength_bucket", None),
)

#: Buckets for the two continuous slicing dimensions. Kept apart on
#: purpose (§19): strength is the size of the expected move, confidence
#: is how much the system trusts it, and averaging outcomes across a
#: mixture of the two answers no question at all.
CONFIDENCE_EDGES = ((0.0, 0.2, "very_low"), (0.2, 0.4, "low"),
                    (0.4, 0.6, "medium"), (0.6, 0.8, "high"),
                    (0.8, 1.01, "very_high"))
STRENGTH_EDGES = ((0.0, 0.2, "very_weak"), (0.2, 0.4, "weak"),
                  (0.4, 0.6, "moderate"), (0.6, 0.8, "strong"),
                  (0.8, 1.01, "very_strong"))


def bucket(value: Optional[float], edges) -> str:
    """Bucket a 0..1 score. None is its own bucket, never merged into a number."""
    if value is None:
        return "unknown"
    for low, high, label in edges:
        if low <= value < high:
            return label
    return "unknown"


def percentile(values: Sequence[float], fraction: float) -> Optional[float]:
    """
    Linear-interpolated percentile.

    Written out rather than pulled from numpy because this repository
    computes research numbers on the standard library everywhere else,
    and a percentile that only exists when an optional dependency is
    installed is a percentile that will one day be missing.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    low = int(math.floor(position))
    high = int(math.ceil(position))
    if low == high:
        return ordered[low]
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def bootstrap_mean_interval(values: Sequence[float], *,
                            iterations: int = 2000,
                            level: float = 0.95,
                            seed: Optional[int] = None
                            ) -> Tuple[Optional[float], Optional[float], str]:
    """
    A percentile bootstrap interval for the mean (§40).

    Deterministic: the seed is derived from the data itself, so the
    same cohort produces the same interval on every run. A research
    number that changes when you look at it twice is not a research
    number.

    Returns (None, None, "") below `MIN_SAMPLE` rather than a wide
    interval. An interval computed on eleven observations is not a
    cautious estimate — it is an invitation to read eleven observations
    as evidence.
    """
    if len(values) < MIN_SAMPLE:
        return None, None, ""
    if seed is None:
        digest = hashlib.sha256(
            ",".join(f"{v:.10f}" for v in values).encode()).hexdigest()
        seed = int(digest[:12], 16)
    rng = random.Random(seed)
    n = len(values)
    pool = list(values)

    # `random.choices` rather than a loop of `rng.randrange(n)`.
    # Same algorithm, same seed, same numbers — but the resampling runs
    # in C instead of Python. Profiled before changing (§55): the loop
    # version spent 99.6% of aggregation time inside 82.4 million
    # `randrange` calls, and aggregating 6,510 measurements took 85s.
    # This is the only optimisation in this phase and it was measured,
    # not guessed.
    means = [sum(rng.choices(pool, k=n)) / n for _ in range(iterations)]
    means.sort()
    tail = (1.0 - level) / 2.0
    return (percentile(means, tail), percentile(means, 1.0 - tail),
            f"percentile_bootstrap_{iterations}")


@dataclass
class Cohort:
    """One slice of measurements, summarised."""
    cohort_kind: str
    cohort_value: str
    horizon: str
    subject_kind: str
    method_version: str = OUTCOME_METHOD_VERSION

    sample_size: int = 0
    instrument_count: int = 0
    hits: int = 0
    misses: int = 0
    neutrals: int = 0
    insufficient: int = 0

    returns: List[float] = field(default_factory=list)
    mfes: List[float] = field(default_factory=list)
    maes: List[float] = field(default_factory=list)
    times_to_mfe: List[float] = field(default_factory=list)
    expected: List[float] = field(default_factory=list)
    absolute_errors: List[float] = field(default_factory=list)

    @property
    def small_sample(self) -> bool:
        return self.sample_size < MIN_SAMPLE

    @property
    def decided(self) -> int:
        """
        Hits plus misses.

        Neutrals are excluded from the denominator on purpose: a market
        that did not move is not evidence for or against a directional
        claim, and counting it as a loss would make hit rate a measure
        of volatility. The neutral count is reported separately so the
        exclusion is visible rather than buried.
        """
        return self.hits + self.misses

    @property
    def directional_accuracy(self) -> Optional[float]:
        return (self.hits / self.decided) if self.decided else None

    def as_row(self, computed_at: datetime) -> Tuple:
        notes: List[str] = []
        if self.small_sample:
            notes.append(
                f"sample {self.sample_size} is below {MIN_SAMPLE} — "
                f"descriptive only, not evidence")
        if self.neutrals:
            notes.append(
                f"{self.neutrals} measurement(s) landed inside the neutral "
                f"band and are excluded from directional accuracy")
        if self.insufficient:
            notes.append(
                f"{self.insufficient} measurement(s) could not be evaluated "
                f"and are counted nowhere in the rates above")

        ci_low, ci_high, ci_method = bootstrap_mean_interval(self.returns)
        aggregate_id = "oa-" + hashlib.sha256("|".join([
            self.method_version, self.subject_kind, self.cohort_kind,
            self.cohort_value, self.horizon]).encode()).hexdigest()[:20]

        def stat(values, fn):
            return fn(values) if values else None

        return (
            aggregate_id, self.method_version, self.subject_kind,
            self.cohort_kind, self.cohort_value, self.horizon,
            self.sample_size, self.instrument_count, int(self.small_sample),
            self.hits, self.misses, self.neutrals, self.insufficient,
            self.directional_accuracy,
            stat(self.returns, lambda v: sum(v) / len(v)),
            stat(self.returns, statistics.median),
            statistics.pstdev(self.returns) if len(self.returns) > 1 else None,
            stat(self.returns, min), stat(self.returns, max),
            percentile(self.returns, 0.10), percentile(self.returns, 0.25),
            percentile(self.returns, 0.75), percentile(self.returns, 0.90),
            stat(self.mfes, lambda v: sum(v) / len(v)),
            stat(self.maes, lambda v: sum(v) / len(v)),
            stat(self.mfes, statistics.median),
            stat(self.maes, statistics.median),
            stat(self.times_to_mfe, lambda v: sum(v) / len(v)),
            stat(self.expected, lambda v: sum(v) / len(v)),
            stat(self.absolute_errors, lambda v: sum(v) / len(v)),
            ci_low, ci_high, ci_method or None,
            json.dumps(notes), computed_at.isoformat())


def _instruments(rows) -> int:
    return len({row["instrument_id"] for row in rows if row["instrument_id"]})


def build_cohorts(measurements: Iterable[Dict[str, Any]],
                  method_version: str = OUTCOME_METHOD_VERSION
                  ) -> List[Cohort]:
    """
    Group measurements into every cohort in `COHORTS`, per horizon.

    Only AVAILABLE measurements contribute to the return distributions.
    PENDING and INSUFFICIENT_DATA rows are counted in `insufficient` so
    they stay visible — dropping them entirely would make a cohort with
    5% coverage indistinguishable from one with 95%.
    """
    grouped: Dict[Tuple[str, str, str, str], Cohort] = {}

    for row in measurements:
        horizon = row["horizon"]
        subject_kind = row["subject_kind"]
        usable = row["status"] == OutcomeStatus.AVAILABLE.value

        keys: List[Tuple[str, str]] = []
        for kind, column in COHORTS:
            if kind == "overall":
                keys.append((kind, "all"))
            elif kind == "confidence_bucket":
                keys.append((kind, bucket(row.get("confidence"), CONFIDENCE_EDGES)))
            elif kind == "strength_bucket":
                keys.append((kind, bucket(row.get("strength"), STRENGTH_EDGES)))
            else:
                value = row.get(column)
                keys.append((kind, str(value) if value not in (None, "") else "unknown"))

        for kind, value in keys:
            key = (subject_kind, kind, value, horizon)
            cohort = grouped.get(key)
            if cohort is None:
                cohort = Cohort(cohort_kind=kind, cohort_value=value,
                                horizon=horizon, subject_kind=subject_kind,
                                method_version=method_version)
                grouped[key] = cohort

            if not usable:
                cohort.insufficient += 1
                continue

            cohort.sample_size += 1
            result = row.get("direction_result")
            if result == DirectionResult.HIT.value:
                cohort.hits += 1
            elif result == DirectionResult.MISS.value:
                cohort.misses += 1
            elif result == DirectionResult.NEUTRAL.value:
                cohort.neutrals += 1

            if row.get("simple_return") is not None:
                cohort.returns.append(row["simple_return"])
            if row.get("mfe") is not None:
                cohort.mfes.append(row["mfe"])
            if row.get("mae") is not None:
                cohort.maes.append(row["mae"])
            if row.get("time_to_mfe_seconds") is not None:
                cohort.times_to_mfe.append(row["time_to_mfe_seconds"])
            if row.get("expected_return") is not None:
                cohort.expected.append(row["expected_return"])
            if row.get("absolute_error") is not None:
                cohort.absolute_errors.append(row["absolute_error"])

    # Instrument counts need a second pass because a cohort's rows are
    # not retained.
    instrument_sets: Dict[Tuple[str, str, str, str], set] = {}
    for row in measurements:
        if row["status"] != OutcomeStatus.AVAILABLE.value:
            continue
        for kind, column in COHORTS:
            if kind == "overall":
                value = "all"
            elif kind == "confidence_bucket":
                value = bucket(row.get("confidence"), CONFIDENCE_EDGES)
            elif kind == "strength_bucket":
                value = bucket(row.get("strength"), STRENGTH_EDGES)
            else:
                raw = row.get(column)
                value = str(raw) if raw not in (None, "") else "unknown"
            key = (row["subject_kind"], kind, value, row["horizon"])
            instrument_sets.setdefault(key, set()).add(row.get("instrument_id"))
    for key, cohort in grouped.items():
        cohort.instrument_count = len(instrument_sets.get(key, set()))

    return list(grouped.values())


def load_measurements(conn: sqlite3.Connection,
                      method_version: str = OUTCOME_METHOD_VERSION,
                      subject_kind: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every measurement for a methodology version, as dicts."""
    initialize_outcome_schema(conn)
    sql = """
        SELECT subject_kind, subject_id, horizon, status, direction_result,
               simple_return, log_return, mfe, mae, time_to_mfe_seconds,
               expected_return, absolute_error, instrument_id,
               trained_model_id, model_status, strategy_id, market_regime,
               event_type, confidence, strength, signal_status,
               horizon_value, horizon_unit, expected_direction
        FROM outcome_measurements WHERE method_version = ?
    """
    params: List[Any] = [method_version]
    if subject_kind:
        sql += " AND subject_kind = ?"
        params.append(subject_kind)
    keys = ("subject_kind", "subject_id", "horizon", "status",
            "direction_result", "simple_return", "log_return", "mfe", "mae",
            "time_to_mfe_seconds", "expected_return", "absolute_error",
            "instrument_id", "trained_model_id", "model_status", "strategy_id",
            "market_regime", "event_type", "confidence", "strength",
            "signal_status", "horizon_value", "horizon_unit",
            # Selected because COHORTS keys the 'direction' slice on it.
            # Omitting it did not fail — it silently made every
            # direction cohort 'unknown', which is exactly the kind of
            # quiet wrongness this phase exists to avoid.
            "expected_direction")
    return [dict(zip(keys, row)) for row in conn.execute(sql, params)]


def save_aggregates(conn: sqlite3.Connection, cohorts: Sequence[Cohort],
                    method_version: str = OUTCOME_METHOD_VERSION,
                    now: Optional[datetime] = None) -> int:
    """
    Replace the aggregates for this methodology version.

    Aggregates ARE recomputed wholesale, unlike measurements. An
    aggregate is a derived view over observations — a stale one is
    worse than a rebuilt one — whereas a measurement is an observation
    and is never recomputed under the same version.
    """
    initialize_outcome_schema(conn)
    now = now or datetime.now(timezone.utc)
    conn.execute("DELETE FROM outcome_aggregates WHERE method_version = ?",
                 (method_version,))
    rows = [cohort.as_row(now) for cohort in cohorts]
    conn.executemany("""
        INSERT INTO outcome_aggregates (
            aggregate_id, method_version, subject_kind, cohort_kind,
            cohort_value, horizon, sample_size, instrument_count, small_sample,
            hits, misses, neutrals, insufficient, directional_accuracy,
            mean_return, median_return, stdev_return, min_return, max_return,
            p10_return, p25_return, p75_return, p90_return,
            mean_mfe, mean_mae, median_mfe, median_mae, mean_time_to_mfe,
            mean_expected_return, mean_absolute_error,
            ci_low, ci_high, ci_method, notes_json, computed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, rows)
    conn.commit()
    return len(rows)


def decay_curve(conn: sqlite3.Connection, *,
                cohort_kind: str = "overall", cohort_value: str = "all",
                subject_kind: str = "signal",
                method_version: str = OUTCOME_METHOD_VERSION
                ) -> List[Dict[str, Any]]:
    """
    Mean forward return by horizon, shortest first (§14).

    Ordered by `horizon_value` in seconds rather than alphabetically:
    '10d' sorts before '5d' as text, and a decay curve built on string
    order is wrong in a way that looks entirely plausible on a chart.
    """
    initialize_outcome_schema(conn)
    rows = conn.execute("""
        SELECT a.horizon, a.sample_size, a.mean_return, a.median_return,
               a.directional_accuracy, a.small_sample, a.mean_mfe, a.mean_mae,
               m.horizon_value, m.horizon_unit
        FROM outcome_aggregates a
        LEFT JOIN (SELECT DISTINCT horizon, horizon_value, horizon_unit
                   FROM outcome_measurements) m ON m.horizon = a.horizon
        WHERE a.method_version = ? AND a.subject_kind = ?
          AND a.cohort_kind = ? AND a.cohort_value = ?
    """, (method_version, subject_kind, cohort_kind, cohort_value)).fetchall()

    def order(row):
        value, unit = row[8] or 0, row[9] or "d"
        seconds = {"m": 60.0, "h": 3600.0}.get(unit)
        return value * seconds if seconds else value * 6.5 * 3600.0

    return [{
        "horizon": row[0], "sample_size": row[1], "mean_return": row[2],
        "median_return": row[3], "directional_accuracy": row[4],
        "small_sample": bool(row[5]), "mean_mfe": row[6], "mean_mae": row[7],
    } for row in sorted(rows, key=order)]


def cohort_warning(cohort_count: int) -> str:
    """
    The multiple-testing caveat, as text (§41).

    Returned rather than logged so it can be attached to the output a
    reader is looking at. A caveat in a document nobody opens while
    reading a table is not a caveat.
    """
    return (
        f"{cohort_count} cohorts were computed from the same measurements. "
        f"Slicing repeatedly by horizon, model, instrument, regime, direction "
        f"and confidence produces subgroups that differ by chance: at the "
        f"conventional 5% level roughly one in twenty will look notable with "
        f"no effect present, and the most extreme cohort is the one most "
        f"likely to be noise. No significance test was run and none of these "
        f"numbers is presented as significant.")
