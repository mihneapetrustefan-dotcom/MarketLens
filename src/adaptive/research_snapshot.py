"""
src/adaptive/research_snapshot.py
--------------------------------------------------
Phase 25.9I-F.7: holdout selection, fold feasibility and the frozen
handoff F.6B must consume.

THE HOLDOUT RULE IS INHERITED, NOT INVENTED
-------------------------------------------
`HOLDOUT_FRACTION = 0.20` is the rule Phase 25.9 used, carried over
unchanged. That is deliberate and it is the strongest available defence
against holdout shopping: the rule predates this dataset, so it cannot
have been chosen to suit it.

Applying it mechanically is the whole method. The boundary is

    earliest_information_time + 0.80 * span, floored to the UTC day

and nothing about the result is allowed to feed back into the choice. If
the resulting research region cannot support a legal fold design, the
answer is DATASET STILL INSUFFICIENT -- not a different fraction.

Phase 25.9I-F.6 failed on exactly this and did the right thing: it
refused to move a boundary after seeing that the old one produced zero
folds. The same refusal is encoded here, in `HoldoutDecision`, which
will not construct if `observed_model_outcome` is set.

SPLIT POLICY, ALSO CARRIED OVER
-------------------------------
Purge horizons, embargo, test window, step and the three-fold minimum
are Phase 25.9's and F.6's, unchanged. They are stated here so a
pre-registration can quote them; they are not new numbers.

READS ROWS, NEVER VALUES
------------------------
Every query counts rows, instruments, clusters and timestamps. No label
value is read, so the protected horizon cannot be touched -- and it is
excluded from eligibility regardless.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from src.adaptive.sample_adequacy import MIN_EFFECTIVE_SAMPLE, MIN_VALID_FOLDS
from src.domain.corpus_recovery_models import (
    FoldFeasibility, HoldoutDecision, ResearchHandoff, ResearchSnapshot,
)

UTC = timezone.utc

#: Phase 25.9's holdout fraction, unchanged. See the module docstring.
HOLDOUT_FRACTION = 0.20

#: Carried over from Phase 25.9 / F.6 without modification.
PURGE_DAYS: Dict[str, float] = {"d1": 3.0, "d3": 5.0, "d5": 7.0, "d10": 14.0}
EMBARGO_DAYS = 6.0
TEST_DAYS = 7
STEP_DAYS = 7

#: The horizon that is sealed. Named so it can be excluded; never queried
#: for a value, and never eligible.
PROTECTED_HORIZONS: Tuple[str, ...] = ("d20",)

#: Intraday horizons need distinct sessions, not bars. F.6's floor.
MIN_INTRADAY_SESSIONS = 30

#: The canonical gate a candidate must pass. Named, not reimplemented.
QUALIFICATION_GATE = "src.domain.model_models.ModelEvaluation.is_deployable"

HOLDOUT_RULE_TEXT = (
    "The holdout is the final 20% of the snapshot's information-time span, "
    "floored to the start of the UTC day. The fraction is Phase 25.9's, "
    "carried over unchanged so that it predates this dataset. The boundary is "
    "computed from the information range alone and is never adjusted after any "
    "fold count or model outcome is observed."
)


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def is_protected(horizon_id: str) -> bool:
    return horizon_id in PROTECTED_HORIZONS


# ======================================================================
# The holdout
# ======================================================================

def information_range(conn: sqlite3.Connection
                      ) -> Tuple[Optional[datetime], Optional[datetime]]:
    row = conn.execute("""
        SELECT MIN(information_cutoff), MAX(information_cutoff)
        FROM research_observations WHERE quality_level != 'invalid'
    """).fetchone()
    if not row:
        return None, None
    return _parse(row[0]), _parse(row[1])


def deterministic_holdout(conn: sqlite3.Connection, *, snapshot_id: str,
                          decided_at: Optional[datetime] = None,
                          fraction: float = HOLDOUT_FRACTION) -> HoldoutDecision:
    """
    Apply the inherited rule to the measured information range.

    No argument of this function is a model outcome, a fold count or a
    score, and there is no code path by which one could become one.
    """
    earliest, latest = information_range(conn)
    if earliest is None or latest is None:
        raise ValueError("the snapshot holds no valid observations")
    span = latest - earliest
    if span.total_seconds() <= 0:
        raise ValueError("the snapshot spans no time")

    boundary = earliest + timedelta(
        seconds=span.total_seconds() * (1.0 - fraction))
    boundary = boundary.replace(hour=0, minute=0, second=0, microsecond=0)
    if boundary <= earliest:
        boundary = earliest + timedelta(days=1)

    return HoldoutDecision(
        snapshot_id=snapshot_id,
        rule=HOLDOUT_RULE_TEXT,
        boundary=boundary,
        decided_at=decided_at or datetime.now(UTC),
        earliest_information_time=earliest,
        latest_information_time=latest,
        observed_model_outcome=False,
        note=f"fraction={fraction}, floored to the UTC day")


# ======================================================================
# Horizon inventory and effective sample, recomputed
# ======================================================================

def horizon_ids(conn: sqlite3.Connection, *,
                include_protected: bool = False) -> List[str]:
    found = sorted({
        str(row[0]).split(".")[0]
        for row in conn.execute("SELECT DISTINCT name FROM research_labels")
        if row[0] and "." in str(row[0])})
    if include_protected:
        return found
    return [h for h in found if not is_protected(h)]


def effective_sample(conn: sqlite3.Connection, horizon_id: str, *,
                     before: Optional[datetime] = None) -> Dict[str, int]:
    """
    Recomputed from the snapshot's own evidence, never read from a
    stored evaluation.

    F.6's root cause was a persisted `effective_sample_size` of 4 being
    taken for the corpus's capacity. The way to make that impossible is
    not to consult the stored value at all, and this function does not.
    """
    if is_protected(horizon_id):
        raise ValueError(f"{horizon_id} is protected and is not assessed here")

    clauses = ["l.name = ?", "o.quality_level != 'invalid'"]
    params: List[object] = [f"{horizon_id}.abnormal_return"]
    if before is not None:
        clauses.append("o.information_cutoff < ?")
        params.append(before.isoformat())

    row = conn.execute(f"""
        SELECT COUNT(DISTINCT o.observation_id),
               COUNT(DISTINCT o.instrument_id),
               COUNT(DISTINCT o.event_cluster_id),
               COUNT(DISTINCT substr(o.information_cutoff, 1, 10))
        FROM research_observations o
        JOIN research_labels l ON l.observation_id = o.observation_id
        WHERE {' AND '.join(clauses)}
    """, params).fetchone()

    rows, instruments, clusters, sessions = [int(v or 0) for v in row]
    return {
        "rows": rows,
        "instruments": instruments,
        "clusters": clusters,
        "effective_sample": clusters or rows,
        "sessions": sessions,
    }


def assess_feasibility(conn: sqlite3.Connection, *,
                       holdout: HoldoutDecision,
                       horizons: Optional[Sequence[str]] = None,
                       min_folds: int = MIN_VALID_FOLDS
                       ) -> List[FoldFeasibility]:
    """
    For each non-protected horizon, can the research region hold a legal
    walk-forward design?

    This is the acceptance test F.6 failed. It is recomputed here against
    the new snapshot, with the same purge, embargo, test and step policy.
    """
    selected = list(horizons) if horizons is not None else horizon_ids(conn)
    out: List[FoldFeasibility] = []
    for horizon in selected:
        if is_protected(horizon):
            continue
        counts = effective_sample(conn, horizon, before=holdout.boundary)
        out.append(FoldFeasibility(
            horizon_id=horizon,
            research_days=holdout.research_days,
            purge_days=PURGE_DAYS.get(horizon, max(PURGE_DAYS.values())),
            embargo_days=EMBARGO_DAYS,
            test_days=TEST_DAYS,
            step_days=STEP_DAYS,
            min_folds=min_folds,
            observation_count=counts["rows"],
            cluster_count=counts["clusters"]))
    return out


def eligible_horizons(conn: sqlite3.Connection, *,
                      holdout: HoldoutDecision,
                      feasibility: Sequence[FoldFeasibility]
                      ) -> Tuple[List[str], Dict[str, str]]:
    """
    Which horizons may be researched, and why each one may not.

    A horizon is eligible only if its research region clears the
    effective-sample floor, forms the minimum folds, and -- for intraday
    -- holds enough distinct sessions. None of those thresholds is
    relaxed here.
    """
    eligible: List[str] = []
    reasons: Dict[str, str] = {}
    by_horizon = {f.horizon_id: f for f in feasibility}

    for horizon in horizon_ids(conn, include_protected=True):
        if is_protected(horizon):
            reasons[horizon] = "PROTECTED"
            continue
        item = by_horizon.get(horizon)
        if item is None:
            reasons[horizon] = "not assessed"
            continue
        counts = effective_sample(conn, horizon, before=holdout.boundary)
        if counts["effective_sample"] < MIN_EFFECTIVE_SAMPLE:
            reasons[horizon] = (
                f"research-region effective sample {counts['effective_sample']} "
                f"below the floor of {MIN_EFFECTIVE_SAMPLE}")
            continue
        if horizon.startswith("intraday") and counts["sessions"] < MIN_INTRADAY_SESSIONS:
            reasons[horizon] = (
                f"{counts['sessions']} distinct sessions, floor "
                f"{MIN_INTRADAY_SESSIONS}")
            continue
        if not item.is_viable:
            reasons[horizon] = (
                f"{item.max_folds} fold(s) possible, contract requires "
                f"{item.min_folds}; short by {item.shortfall_days:.0f} days")
            continue
        eligible.append(horizon)
        reasons[horizon] = "ELIGIBLE"

    return eligible, reasons


# ======================================================================
# Holdout adequacy
# ======================================================================

def holdout_adequacy(conn: sqlite3.Connection, *, holdout: HoldoutDecision,
                     feasibility: Sequence[FoldFeasibility]) -> Dict[str, object]:
    """
    Is the proposed split a scientifically meaningful design?

    A zero-fold design is rejected. If even the recovered history cannot
    support one, the honest answer is DATASET STILL INSUFFICIENT, and
    manufacturing folds to avoid saying it is the one thing forbidden.
    """
    viable = [f for f in feasibility if f.is_viable]
    not_viable = [f for f in feasibility if not f.is_viable]
    region_clusters = {
        f.horizon_id: f.cluster_count for f in feasibility}

    adequate = bool(viable) and not not_viable
    verdict = "ADEQUATE" if adequate else (
        "PARTIAL" if viable else "DATASET STILL INSUFFICIENT")

    return {
        "verdict": verdict,
        "research_days": holdout.research_days,
        "holdout_days": holdout.holdout_days,
        "holdout_fraction": holdout.holdout_fraction,
        "horizons_viable": [f.horizon_id for f in viable],
        "horizons_not_viable": [f.horizon_id for f in not_viable],
        "max_folds": {f.horizon_id: f.max_folds for f in feasibility},
        "shortfall_days": {f.horizon_id: f.shortfall_days
                           for f in not_viable},
        "research_region_clusters": region_clusters,
        "effective_sample_floor": MIN_EFFECTIVE_SAMPLE,
        "min_folds": MIN_VALID_FOLDS,
        "purge_represented": True,
        "embargo_represented": True,
        "non_empty_fold_design": bool(viable),
        "note": ("every eligible horizon forms at least the minimum folds"
                 if adequate else
                 "at least one horizon cannot form the minimum folds; "
                 "no threshold was relaxed to change that"),
    }


# ======================================================================
# The handoff
# ======================================================================

def build_handoff(conn: sqlite3.Connection, *, snapshot: ResearchSnapshot,
                  holdout: HoldoutDecision, preregistration_path: str,
                  preregistration_hash: str, candidate_budget: int,
                  pre_registered_horizons: Optional[Sequence[str]] = None,
                  min_folds: int = MIN_VALID_FOLDS) -> ResearchHandoff:
    """
    Assemble what F.6B is allowed to start from.

    `pre_registered_horizons` narrows the handoff to the horizons a
    pre-registration actually commits to. Data eligibility and
    pre-registration are two different things: a horizon can clear every
    data threshold and still be deferred because no defensible purge
    policy has been written down for it. Narrowing here can only remove
    horizons, never add one that failed its thresholds.

    The handoff is a valid artifact whether or not it is research ready.
    `is_research_ready` answering False is a result, not a failure to
    produce one.
    """
    feasibility = assess_feasibility(conn, holdout=holdout, min_folds=min_folds)
    eligible, _ = eligible_horizons(conn, holdout=holdout,
                                    feasibility=feasibility)
    if pre_registered_horizons is not None:
        requested = list(pre_registered_horizons)
        unqualified = [h for h in requested if h not in eligible]
        if unqualified:
            raise ValueError(
                f"pre-registered horizons that do not clear their thresholds: "
                f"{unqualified}")
        eligible = requested
    samples = {
        horizon: effective_sample(conn, horizon,
                                  before=holdout.boundary)["effective_sample"]
        for horizon in eligible}
    purge = {horizon: PURGE_DAYS.get(horizon, max(PURGE_DAYS.values()))
             for horizon in eligible}

    return ResearchHandoff(
        snapshot=snapshot,
        holdout=holdout,
        preregistration_path=preregistration_path,
        preregistration_hash=preregistration_hash,
        eligible_horizons=tuple(eligible),
        feasibility=tuple(feasibility),
        effective_sample=samples,
        qualification_gate_reference=QUALIFICATION_GATE,
        candidate_budget=candidate_budget,
        purge_days=purge,
        embargo_days=EMBARGO_DAYS,
        d20_excluded=True,
        note="frozen before any F.6B candidate was evaluated")
