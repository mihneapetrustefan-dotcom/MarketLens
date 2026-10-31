"""
src/adaptive/sample_adequacy.py
---------------------------------------------
Whether a horizon has enough INDEPENDENT evidence to be researched
(Phase 25.9I-F.6).

THE RULE THIS MODULE EXISTS TO ENFORCE
------------------------------------------
    MORE ROWS  !=  MORE EVIDENCE.

Phase 25.9I-F.5 reported the one existing model as failing on
`effective_sample = 4`. That number is the whole reason this phase
exists, and the first job was to find out what it actually meant.

WHAT A CLUSTER IS, MEASURED
-------------------------------
`research_observations.event_cluster_id` is the clustering key, and
audited 2026-09-30 its value **is the instrument id**: 244 distinct
clusters over 244 distinct instruments, zero nulls. So a cluster is an
INSTRUMENT, and `ModelEvaluation.effective_sample_size` answers "how
many distinct companies is this evidence really about" rather than "how
many rows are there".

That is the right question. Fifteen observations of BLK across one
month are fifteen views of one company in one regime; treating them as
fifteen independent samples is how a model gets qualified on nothing.

WHY THE STORED NUMBER SAID 4
--------------------------------
It is a TEST-SPLIT count, and it is STALE. The stored evaluation was
written on 2026-08-29, when few d5 labels had matured: 5 test rows
spanning 4 instruments. Labels mature with the calendar, and re-running
the identical split today yields 27 test clusters from 32 test rows.

So `effective_sample = 4` never described the corpus. It described one
test fold of one split at one moment, and that moment has passed.

WHAT LEGITIMATELY RAISES IT, AND WHAT ONLY LOOKS LIKE IT DOES
-----------------------------------------------------------------
Raises it:
  - a new instrument entering the corpus (a genuinely new cluster)
  - labels maturing on observations that already exist
  - a longer observation window covering new regimes

Does NOT raise it, and this module refuses to count any of them:
  - duplicating rows
  - resampling or bootstrapping the same clusters
  - splitting one cluster's rows into "folds" and calling them
    independent
  - counting one instrument's fifteen observations as fifteen

`effective_sample` here is therefore always a DISTINCT-CLUSTER count,
never a row count, and `row_inflation_ratio` reports how far apart the
two are so a reader can see when rows are flattering the evidence.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from src.adaptive.horizon_inventory import is_protected
from src.domain.model_models import ModelEvaluation

#: The qualification floor, reused from the canonical gate rather than
#: restated. If that constant moves, this module moves with it.
MIN_EFFECTIVE_SAMPLE = ModelEvaluation.MIN_EFFECTIVE_SAMPLE

#: Minimum valid walk-forward folds the Phase 25.9 pre-registration
#: requires before any verdict may be issued. Reused, not re-chosen.
MIN_VALID_FOLDS = 3

#: Minimum distinct SESSIONS before intraday evidence is considered to
#: span more than one regime. Intraday bars from few sessions are many
#: views of a few days, not many independent days.
MIN_INTRADAY_SESSIONS = 30


class ResearchReadiness(str, Enum):
    """
    Whether a horizon can be fairly tested, not whether it will work.

    `RESEARCH_READY` is a statement about EVIDENCE VOLUME only. It
    promises a fair test, never a positive result.
    """
    RESEARCH_READY = "research_ready"
    DATA_ACCUMULATION_REQUIRED = "data_accumulation_required"
    INSUFFICIENT_EFFECTIVE_SAMPLE = "insufficient_effective_sample"
    UNSUPPORTED = "unsupported"
    PROTECTED = "protected"

    @property
    def is_researchable(self) -> bool:
        return self is ResearchReadiness.RESEARCH_READY


@dataclass(frozen=True)
class SampleAdequacy:
    """
    One horizon's evidence, counted honestly.

    `effective_sample` is ALWAYS the distinct-cluster count. `raw_rows`
    is reported beside it rather than instead of it, so the gap between
    "how much data looks like there is" and "how much independent
    evidence there is" is visible rather than inferred.
    """
    horizon_id: str
    raw_rows: int = 0
    matured_labels: int = 0
    unique_instruments: int = 0
    unique_clusters: int = 0
    unique_sessions: int = 0
    unique_dates: int = 0
    research_region_rows: int = 0
    research_region_clusters: int = 0
    holdout_rows: int = 0
    holdout_clusters: int = 0
    first_cutoff: Optional[datetime] = None
    last_cutoff: Optional[datetime] = None
    is_intraday: bool = False
    readiness: ResearchReadiness = ResearchReadiness.UNSUPPORTED
    reasons: Tuple[str, ...] = ()

    def __post_init__(self):
        for name in ("raw_rows", "matured_labels", "unique_instruments",
                     "unique_clusters", "unique_sessions", "unique_dates",
                     "research_region_rows", "research_region_clusters",
                     "holdout_rows", "holdout_clusters"):
            if int(getattr(self, name)) < 0:
                raise ValueError(f"{name} must be >= 0")
        if self.unique_clusters > self.raw_rows:
            # More clusters than rows is arithmetically impossible and
            # would mean something was double-counted.
            raise ValueError(
                f"{self.horizon_id}: {self.unique_clusters} clusters from "
                f"{self.raw_rows} rows is impossible")

    @property
    def effective_sample(self) -> int:
        """Distinct clusters. Never a row count, under any circumstance."""
        return self.unique_clusters

    @property
    def research_region_effective_sample(self) -> int:
        """What a model may actually train and be tested on."""
        return self.research_region_clusters

    @property
    def row_inflation_ratio(self) -> Optional[float]:
        """
        Rows per independent cluster.

        A ratio near 1 means the rows really are independent. A large
        ratio means the corpus is deep but narrow -- many views of few
        companies -- and a row count would badly overstate the evidence.
        """
        if not self.unique_clusters:
            return None
        return round(self.raw_rows / self.unique_clusters, 3)

    @property
    def meets_effective_floor(self) -> bool:
        return self.effective_sample >= MIN_EFFECTIVE_SAMPLE

    @property
    def research_region_meets_floor(self) -> bool:
        return self.research_region_effective_sample >= MIN_EFFECTIVE_SAMPLE

    @property
    def rows_sufficient_but_sample_is_not(self) -> bool:
        """
        The specific trap this module exists to name.

        Plenty of rows, not enough independent clusters. Reported
        separately because it is the state most likely to be mistaken
        for adequacy.
        """
        return self.raw_rows >= MIN_EFFECTIVE_SAMPLE and not self.meets_effective_floor

    @property
    def clusters_short_of_floor(self) -> int:
        return max(0, MIN_EFFECTIVE_SAMPLE - self.effective_sample)

    def describe(self) -> Dict[str, object]:
        return {
            "horizon_id": self.horizon_id,
            "raw_rows": self.raw_rows,
            "matured_labels": self.matured_labels,
            "unique_instruments": self.unique_instruments,
            "effective_sample_clusters": self.effective_sample,
            "unique_sessions": self.unique_sessions,
            "unique_dates": self.unique_dates,
            "row_inflation_ratio": self.row_inflation_ratio,
            "qualification_floor": MIN_EFFECTIVE_SAMPLE,
            "meets_effective_floor": self.meets_effective_floor,
            "rows_sufficient_but_sample_is_not":
                self.rows_sufficient_but_sample_is_not,
            "clusters_short_of_floor": self.clusters_short_of_floor,
            "research_region_rows": self.research_region_rows,
            "research_region_clusters": self.research_region_clusters,
            "research_region_meets_floor": self.research_region_meets_floor,
            "holdout_rows": self.holdout_rows,
            "holdout_clusters": self.holdout_clusters,
            "first_cutoff": (self.first_cutoff.isoformat()
                             if self.first_cutoff else None),
            "last_cutoff": (self.last_cutoff.isoformat()
                            if self.last_cutoff else None),
            "is_intraday": self.is_intraday,
            "readiness": self.readiness.value,
            "reasons": list(self.reasons),
        }


def _parse(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def assess_horizon(conn: sqlite3.Connection, horizon_id: str, *,
                   holdout_start: Optional[datetime] = None,
                   as_of: Optional[datetime] = None) -> SampleAdequacy:
    """
    Count one horizon's independent evidence. Reads no label VALUE.

    The label value column is never named in any query here. Adequacy
    is a question about how much evidence exists, not what it says, and
    a function that cannot read an outcome cannot leak one -- which is
    what lets this run safely over a sealed horizon too.
    """
    window = str(horizon_id).strip()
    protected = is_protected(window)

    rows = []
    if not protected:
        try:
            rows = conn.execute(
                "SELECT o.observation_id, o.instrument_id, o.event_cluster_id, "
                "o.information_cutoff, o.quality_level "
                "FROM research_labels l "
                "JOIN research_observations o "
                "  ON o.observation_id = l.observation_id "
                "WHERE l.window_name = ? AND l.name = ? "
                + ("AND (o.information_cutoff IS NULL "
                   "OR o.information_cutoff <= ?)" if as_of else ""),
                (window, f"{window}.abnormal_return")
                + ((as_of.isoformat(),) if as_of else ())).fetchall()
        except sqlite3.DatabaseError:
            rows = []
    else:
        # Readiness metadata only: how many labels exist, nothing about
        # what they say and nothing about which rows they attach to.
        try:
            matured = conn.execute(
                "SELECT COUNT(*) FROM research_labels "
                "WHERE window_name = ? AND name = ?",
                (window, f"{window}.abnormal_return")).fetchone()[0]
        except sqlite3.DatabaseError:
            matured = 0
        return SampleAdequacy(
            horizon_id=window, matured_labels=int(matured or 0),
            is_intraday=window.startswith("intraday"),
            readiness=ResearchReadiness.PROTECTED,
            reasons=("this horizon belongs to a sealed experiment and is out "
                     "of scope for research; only label counts are reported",))

    usable = [r for r in rows if str(r[4] or "").lower() != "invalid"]
    instruments = {str(r[1]) for r in usable if r[1]}
    clusters = {str(r[2] or r[0]) for r in usable}
    cutoffs = [c for c in (_parse(r[3]) for r in usable) if c is not None]
    dates = {c.date() for c in cutoffs}

    research_rows = holdout_rows = 0
    research_clusters: Set[str] = set()
    holdout_clusters: Set[str] = set()
    for row in usable:
        cutoff = _parse(row[3])
        cluster = str(row[2] or row[0])
        if holdout_start is not None and cutoff is not None and cutoff >= holdout_start:
            holdout_rows += 1
            holdout_clusters.add(cluster)
        else:
            research_rows += 1
            research_clusters.add(cluster)

    is_intraday = window.startswith("intraday")
    adequacy = SampleAdequacy(
        horizon_id=window, raw_rows=len(usable), matured_labels=len(rows),
        unique_instruments=len(instruments), unique_clusters=len(clusters),
        unique_sessions=len(dates), unique_dates=len(dates),
        research_region_rows=research_rows,
        research_region_clusters=len(research_clusters),
        holdout_rows=holdout_rows, holdout_clusters=len(holdout_clusters),
        first_cutoff=min(cutoffs) if cutoffs else None,
        last_cutoff=max(cutoffs) if cutoffs else None,
        is_intraday=is_intraday)

    readiness, reasons = _judge(adequacy)
    return SampleAdequacy(
        **{**adequacy.__dict__, "readiness": readiness,
           "reasons": tuple(reasons)})


def _judge(adequacy: SampleAdequacy) -> Tuple[ResearchReadiness, List[str]]:
    """
    Whether a fair test can be run. Never whether it will succeed.

    Ordered so the reason names the binding constraint: no data before
    too few clusters, too few clusters before too few sessions.
    """
    reasons: List[str] = []

    if adequacy.raw_rows == 0:
        return (ResearchReadiness.UNSUPPORTED,
                ["no matured labels exist for this horizon"])

    if adequacy.rows_sufficient_but_sample_is_not:
        reasons.append(
            f"{adequacy.raw_rows} rows but only {adequacy.effective_sample} "
            f"independent clusters -- rows overstate the evidence "
            f"{adequacy.row_inflation_ratio}x")

    if not adequacy.meets_effective_floor:
        reasons.append(
            f"effective sample {adequacy.effective_sample} is below the "
            f"qualification floor of {MIN_EFFECTIVE_SAMPLE}; "
            f"{adequacy.clusters_short_of_floor} more independent clusters "
            "are needed")
        return ResearchReadiness.INSUFFICIENT_EFFECTIVE_SAMPLE, reasons

    if not adequacy.research_region_meets_floor:
        reasons.append(
            f"the research region holds {adequacy.research_region_effective_sample} "
            f"clusters, below the floor of {MIN_EFFECTIVE_SAMPLE}; the holdout "
            "cannot be borrowed to make up the difference")
        return ResearchReadiness.INSUFFICIENT_EFFECTIVE_SAMPLE, reasons

    if adequacy.is_intraday and adequacy.unique_sessions < MIN_INTRADAY_SESSIONS:
        reasons.append(
            f"{adequacy.unique_sessions} distinct sessions, below "
            f"{MIN_INTRADAY_SESSIONS}: intraday bars from few sessions are "
            "many views of a few days, not many independent days")
        return ResearchReadiness.DATA_ACCUMULATION_REQUIRED, reasons

    reasons.append(
        f"{adequacy.effective_sample} independent clusters over "
        f"{adequacy.unique_dates} dates clears the floor; a fair test can be "
        "run (this says nothing about whether an edge exists)")
    return ResearchReadiness.RESEARCH_READY, reasons


def assess_all(conn: sqlite3.Connection, horizon_ids: Sequence[str], *,
               holdout_start: Optional[datetime] = None,
               as_of: Optional[datetime] = None
               ) -> Tuple[SampleAdequacy, ...]:
    """Every horizon, in the order given."""
    return tuple(assess_horizon(conn, horizon_id, holdout_start=holdout_start,
                                as_of=as_of)
                 for horizon_id in horizon_ids)


def effective_sample_of(cluster_ids: Sequence[str]) -> int:
    """
    The one correct way to count a sample in this repository.

    Exposed as a function so tests can assert directly that duplicating
    a cluster does not raise it -- which is the property everything
    else depends on.
    """
    return len({str(c) for c in cluster_ids if c is not None and str(c) != ""})
