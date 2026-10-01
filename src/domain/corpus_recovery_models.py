"""
src/domain/corpus_recovery_models.py
--------------------------------------------------
Phase 25.9I-F.7: contracts for historical corpus recovery.

> RECOVERED HISTORY CREATES A NEW DATASET VERSION.
> IT DOES NOT REWRITE THE PAST EXPERIMENT.

> MORE HISTORICAL ROWS ARE USEFUL ONLY IF THEIR TEMPORAL AND
> PROVENANCE INTEGRITY IS VALID.

Everything here is a refusal as much as a record. The dangerous failure
in a recovery phase is not losing rows -- it is gaining rows that look
historical but carry today's knowledge. So:

  - `InformationTiming` refuses an information time at or after the
    instant the recovery ran. A July article processed in October is a
    July information event; if those two collapse, every as-of
    guarantee downstream is void.
  - `RecoveryRun.mark_complete()` refuses to report COMPLETE while any
    part of its own date range is unprocessed.
  - `CorpusVersion` and `ResearchSnapshot` refuse mutation once FROZEN.
  - `ResearchSnapshot.masquerades_as()` exists so a new snapshot cannot
    quietly inherit an old experiment's identity.

Reused, not redefined: `DatasetVersion` from `src/domain/research_models.py`
supplies the component-version fingerprint. This module adds corpus and
snapshot identity around it; it does not invent a second versioning
scheme.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from src.domain.research_models import DatasetVersion

UTC = timezone.utc


def _require_utc(value: Optional[datetime], label: str) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


# ======================================================================
# The pipeline, as it actually is
# ======================================================================

class PipelineStage(str, Enum):
    """
    The persisted stages a historical article passes through. The order
    is the real order; `index` exists so a funnel can be reported and
    reconciled stage by stage rather than as an unordered bag.
    """
    RAW_ARTICLE = "raw_article"
    NORMALIZED_ARTICLE = "normalized_article"
    ENTITY_RESOLVED = "entity_resolved"
    EVENT_REPORT = "event_report"
    FUSED = "fused"
    CANONICAL_EVENT = "canonical_event"
    RESEARCH_OBSERVATION = "research_observation"

    @property
    def index(self) -> int:
        return _STAGE_ORDER[self]


_STAGE_ORDER: Dict["PipelineStage", int] = {
    PipelineStage.RAW_ARTICLE: 0,
    PipelineStage.NORMALIZED_ARTICLE: 1,
    PipelineStage.ENTITY_RESOLVED: 2,
    PipelineStage.EVENT_REPORT: 3,
    PipelineStage.FUSED: 4,
    PipelineStage.CANONICAL_EVENT: 5,
    PipelineStage.RESEARCH_OBSERVATION: 6,
}

STAGE_SEQUENCE: Tuple[PipelineStage, ...] = tuple(
    sorted(PipelineStage, key=lambda s: _STAGE_ORDER[s]))


class LossReason(str, Enum):
    """
    Why a historical article did not become canonical-event evidence.

    `UNKNOWN` is a first-class answer. Lineage that cannot establish a
    reason must say so; inventing one would make the loss accounting
    look complete when it is not.
    """
    BECAME_CANONICAL_EVENT = "became_canonical_event"
    NOT_INGESTED = "not_ingested"
    NOT_NORMALIZED = "not_normalized"
    ENTITY_NOT_ATTEMPTED = "entity_not_attempted"
    ENTITY_UNRESOLVED = "entity_unresolved"
    ENTITY_AMBIGUOUS = "entity_ambiguous"
    EXTRACTION_NOT_ATTEMPTED = "extraction_not_attempted"
    EXTRACTION_FAILED = "extraction_failed"
    NO_EVENT_FOUND = "no_event_found"
    EVENT_REJECTED = "event_rejected"
    FUSION_NOT_RUN = "fusion_not_run"
    FUSION_REJECTED = "fusion_rejected"
    DUPLICATE_OF_EXISTING_EVENT = "duplicate_of_existing_event"
    INVALID_TIMESTAMP = "invalid_timestamp"
    UNSUPPORTED_SOURCE = "unsupported_source"
    PIPELINE_ERROR = "pipeline_error"
    UNKNOWN = "unknown"

    @property
    def is_loss(self) -> bool:
        return self is not LossReason.BECAME_CANONICAL_EVENT

    @property
    def is_legitimate(self) -> bool:
        """
        True when the article was correctly not turned into an event.

        A news article about nothing eventful is not a pipeline failure,
        and counting it as one would overstate the recoverable volume.
        """
        return self in (LossReason.BECAME_CANONICAL_EVENT,
                        LossReason.NO_EVENT_FOUND,
                        LossReason.DUPLICATE_OF_EXISTING_EVENT,
                        LossReason.UNSUPPORTED_SOURCE)

    @property
    def is_recoverable(self) -> bool:
        """
        True when re-running an existing stage over existing source
        material should fix it.

        Deliberately excludes `ENTITY_UNRESOLVED` and
        `ENTITY_AMBIGUOUS`: those were attempted and the resolver
        declined. Re-running them unchanged yields the same answer, and
        loosening the resolver to change it is forbidden -- a false
        mapping is worse than a missing one.
        """
        return self in (LossReason.ENTITY_NOT_ATTEMPTED,
                        LossReason.EXTRACTION_NOT_ATTEMPTED,
                        LossReason.FUSION_NOT_RUN)


# ======================================================================
# The funnel
# ======================================================================

@dataclass(frozen=True)
class FunnelStage:
    """
    One measured stage. `count_entering` is what the previous stage
    produced, so a drop is always attributable to this stage and not
    smeared across two.
    """
    stage: PipelineStage
    count_entering: int
    count_exiting: int
    earliest: Optional[datetime] = None
    latest: Optional[datetime] = None
    reasons: Mapping[str, int] = field(default_factory=dict)
    note: str = ""

    def __post_init__(self):
        object.__setattr__(self, "earliest", _require_utc(self.earliest, "earliest"))
        object.__setattr__(self, "latest", _require_utc(self.latest, "latest"))
        if self.count_entering < 0 or self.count_exiting < 0:
            raise ValueError("counts cannot be negative")
        if self.earliest and self.latest and self.earliest > self.latest:
            raise ValueError(f"{self.stage.value}: earliest is after latest")

    @property
    def drop(self) -> int:
        return max(self.count_entering - self.count_exiting, 0)

    @property
    def drop_pct(self) -> Optional[float]:
        if not self.count_entering:
            return None
        return round(100.0 * self.drop / self.count_entering, 2)

    @property
    def is_total_loss(self) -> bool:
        return self.count_entering > 0 and self.count_exiting == 0

    @property
    def span_days(self) -> Optional[int]:
        if not (self.earliest and self.latest):
            return None
        return (self.latest - self.earliest).days

    def describe(self) -> Dict[str, object]:
        return {
            "stage": self.stage.value,
            "entering": self.count_entering,
            "exiting": self.count_exiting,
            "drop": self.drop,
            "drop_pct": self.drop_pct,
            "earliest": self.earliest.isoformat() if self.earliest else None,
            "latest": self.latest.isoformat() if self.latest else None,
            "span_days": self.span_days,
            "reasons": dict(self.reasons),
            "note": self.note,
        }


@dataclass(frozen=True)
class HistoricalFunnel:
    """
    The whole path, measured. `first_material_break` is the point worth
    repairing; everything downstream of it is a consequence, not a
    separate problem.
    """
    stages: Tuple[FunnelStage, ...]
    period_label: str = ""
    measured_at: Optional[datetime] = None
    material_drop_pct: float = 50.0

    def __post_init__(self):
        object.__setattr__(self, "measured_at", _require_utc(self.measured_at, "measured_at"))
        seen = [s.stage.index for s in self.stages]
        if seen != sorted(seen):
            raise ValueError("funnel stages must be in pipeline order")
        if len(set(seen)) != len(seen):
            raise ValueError("a stage appears twice in the funnel")

    def stage(self, which: PipelineStage) -> Optional[FunnelStage]:
        return next((s for s in self.stages if s.stage is which), None)

    @property
    def reconciles(self) -> bool:
        """
        Every stage's entering count equals the previous stage's exiting
        count. A funnel that does not reconcile is not evidence; it is
        two measurements of different things.
        """
        for previous, current in zip(self.stages, self.stages[1:]):
            if current.count_entering != previous.count_exiting:
                return False
        return True

    @property
    def first_material_break(self) -> Optional[FunnelStage]:
        for stage in self.stages:
            if stage.is_total_loss:
                return stage
            pct = stage.drop_pct
            if pct is not None and pct >= self.material_drop_pct:
                return stage
        return None

    @property
    def total_input(self) -> int:
        return self.stages[0].count_entering if self.stages else 0

    @property
    def total_output(self) -> int:
        return self.stages[-1].count_exiting if self.stages else 0

    def describe(self) -> Dict[str, object]:
        break_at = self.first_material_break
        return {
            "period": self.period_label,
            "measured_at": self.measured_at.isoformat() if self.measured_at else None,
            "reconciles": self.reconciles,
            "total_input": self.total_input,
            "total_output": self.total_output,
            "first_material_break": break_at.stage.value if break_at else None,
            "stages": [s.describe() for s in self.stages],
        }


@dataclass(frozen=True)
class LossAccounting:
    """
    Where a period's source articles went, classified. `reconciles`
    matters more than any single number: a classification that does not
    add up to the article count is hiding something.
    """
    period_label: str
    total_source_articles: int
    by_reason: Mapping[LossReason, int]
    note: str = ""

    def __post_init__(self):
        if self.total_source_articles < 0:
            raise ValueError("total_source_articles cannot be negative")
        for reason, count in self.by_reason.items():
            if not isinstance(reason, LossReason):
                raise TypeError(f"{reason!r} is not a LossReason")
            if count < 0:
                raise ValueError(f"{reason.value} count cannot be negative")

    @property
    def classified(self) -> int:
        return sum(self.by_reason.values())

    @property
    def unclassified(self) -> int:
        return max(self.total_source_articles - self.classified, 0)

    @property
    def reconciles(self) -> bool:
        return self.classified == self.total_source_articles

    @property
    def recoverable(self) -> int:
        return sum(n for reason, n in self.by_reason.items() if reason.is_recoverable)

    @property
    def legitimately_lost(self) -> int:
        return sum(n for reason, n in self.by_reason.items()
                   if reason.is_loss and reason.is_legitimate)

    def describe(self) -> Dict[str, object]:
        return {
            "period": self.period_label,
            "total_source_articles": self.total_source_articles,
            "classified": self.classified,
            "unclassified": self.unclassified,
            "reconciles": self.reconciles,
            "recoverable": self.recoverable,
            "legitimately_lost": self.legitimately_lost,
            "by_reason": {r.value: n for r, n in sorted(
                self.by_reason.items(), key=lambda kv: -kv[1])},
            "note": self.note,
        }


# ======================================================================
# Information time is not processing time
# ======================================================================

@dataclass(frozen=True)
class InformationTiming:
    """
    The single most important contract in this phase.

        PROCESSING TIME  !=  INFORMATION TIME

    A July article replayed in October is a July information event. If
    `information_time` were allowed to pick up the recovery instant,
    every purge, embargo and as-of guarantee downstream would be
    silently void, and the resulting dataset would look richer while
    being contaminated.

    So this refuses an `information_time` at or after `recovered_at`.
    You cannot recover information that did not yet exist when you
    recovered it.
    """
    information_time: datetime
    processing_time: Optional[datetime] = None
    recovered_at: Optional[datetime] = None
    recovery_run_id: Optional[str] = None
    source: str = ""

    def __post_init__(self):
        info = _require_utc(self.information_time, "information_time")
        if info is None:
            raise ValueError("information_time is required")
        object.__setattr__(self, "information_time", info)
        object.__setattr__(self, "processing_time",
                           _require_utc(self.processing_time, "processing_time"))
        object.__setattr__(self, "recovered_at",
                           _require_utc(self.recovered_at, "recovered_at"))

        if self.recovered_at is not None and info >= self.recovered_at:
            raise ValueError(
                "information_time is at or after recovered_at -- the recovery "
                "instant has leaked into the information set")
        if self.processing_time is not None and info > self.processing_time:
            raise ValueError(
                "information_time is after processing_time -- an article cannot "
                "be processed before it was published")

    @property
    def is_recovered(self) -> bool:
        return self.recovery_run_id is not None

    @property
    def recovery_lag_days(self) -> Optional[int]:
        if self.recovered_at is None:
            return None
        return (self.recovered_at - self.information_time).days

    def describe(self) -> Dict[str, object]:
        return {
            "information_time": self.information_time.isoformat(),
            "processing_time": (self.processing_time.isoformat()
                                if self.processing_time else None),
            "recovered_at": self.recovered_at.isoformat() if self.recovered_at else None,
            "recovery_run_id": self.recovery_run_id,
            "is_recovered": self.is_recovered,
            "recovery_lag_days": self.recovery_lag_days,
            "source": self.source,
        }


# ======================================================================
# Recovery runs
# ======================================================================

class RecoveryStatus(str, Enum):
    PLANNED = "planned"
    RUNNING = "running"
    PARTIAL = "partial"
    FAILED = "failed"
    COMPLETE = "complete"

    @property
    def is_terminal(self) -> bool:
        return self in (RecoveryStatus.PARTIAL, RecoveryStatus.FAILED,
                        RecoveryStatus.COMPLETE)

    @property
    def is_trustworthy(self) -> bool:
        """Only a COMPLETE run may be used to build a research snapshot."""
        return self is RecoveryStatus.COMPLETE


@dataclass(frozen=True)
class RecoveryRun:
    """
    Durable identity for one recovery operation. Recovered history must
    never be indistinguishable from original processing, so every
    recovered record points back to one of these.
    """
    recovery_run_id: str
    pipeline_version: str
    code_commit: str
    range_start: datetime
    range_end: datetime
    started_at: datetime
    source_snapshot_id: Optional[str] = None
    completed_at: Optional[datetime] = None
    status: RecoveryStatus = RecoveryStatus.PLANNED
    processed_through: Optional[datetime] = None
    counts: Mapping[str, int] = field(default_factory=dict)
    errors: Tuple[str, ...] = ()
    note: str = ""

    def __post_init__(self):
        for name in ("range_start", "range_end", "started_at",
                     "completed_at", "processed_through"):
            object.__setattr__(self, name, _require_utc(getattr(self, name), name))
        if not self.recovery_run_id:
            raise ValueError("recovery_run_id is required")
        if not self.code_commit:
            raise ValueError("code_commit is required -- a recovery that cannot "
                             "name the code that produced it is not auditable")
        if self.range_start >= self.range_end:
            raise ValueError("range_start must precede range_end")
        if self.status is RecoveryStatus.COMPLETE and self.completed_at is None:
            raise ValueError("a COMPLETE run must record completed_at")

    @property
    def covers_full_range(self) -> bool:
        return (self.processed_through is not None
                and self.processed_through >= self.range_end)

    @property
    def range_days(self) -> int:
        return (self.range_end - self.range_start).days

    def mark_complete(self, *, completed_at: datetime,
                      processed_through: datetime,
                      counts: Optional[Mapping[str, int]] = None) -> "RecoveryRun":
        """
        Refuses to report COMPLETE when only part of the range was
        processed. A partially recovered corpus that claims completeness
        is worse than one that admits it stopped early, because nothing
        downstream would know to distrust it.
        """
        processed_through = _require_utc(processed_through, "processed_through")
        completed_at = _require_utc(completed_at, "completed_at")
        if processed_through < self.range_end:
            raise ValueError(
                f"cannot mark COMPLETE: processed through {processed_through.isoformat()} "
                f"but the range ends {self.range_end.isoformat()} -- use mark_partial()")
        if self.errors:
            raise ValueError("cannot mark COMPLETE while errors are recorded")
        return replace(self, status=RecoveryStatus.COMPLETE,
                       completed_at=completed_at,
                       processed_through=processed_through,
                       counts=dict(counts or self.counts))

    def mark_partial(self, *, completed_at: datetime,
                     processed_through: Optional[datetime],
                     reason: str,
                     counts: Optional[Mapping[str, int]] = None) -> "RecoveryRun":
        if not reason:
            raise ValueError("a PARTIAL run must say why it stopped")
        return replace(self, status=RecoveryStatus.PARTIAL,
                       completed_at=_require_utc(completed_at, "completed_at"),
                       processed_through=_require_utc(processed_through, "processed_through"),
                       counts=dict(counts or self.counts),
                       note=reason)

    def mark_failed(self, *, completed_at: datetime,
                    errors: Sequence[str]) -> "RecoveryRun":
        if not errors:
            raise ValueError("a FAILED run must record at least one error")
        return replace(self, status=RecoveryStatus.FAILED,
                       completed_at=_require_utc(completed_at, "completed_at"),
                       errors=tuple(errors))

    def describe(self) -> Dict[str, object]:
        return {
            "recovery_run_id": self.recovery_run_id,
            "pipeline_version": self.pipeline_version,
            "code_commit": self.code_commit,
            "source_snapshot_id": self.source_snapshot_id,
            "range_start": self.range_start.isoformat(),
            "range_end": self.range_end.isoformat(),
            "range_days": self.range_days,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "status": self.status.value,
            "processed_through": (self.processed_through.isoformat()
                                  if self.processed_through else None),
            "covers_full_range": self.covers_full_range,
            "counts": dict(self.counts),
            "errors": list(self.errors),
            "note": self.note,
        }


# ======================================================================
# Corpus and snapshot versions
# ======================================================================

class ArtifactState(str, Enum):
    """
    A corpus or snapshot does not become research input because files
    exist. It has to be validated, then frozen, then declared eligible.
    """
    BUILDING = "building"
    VALIDATED = "validated"
    FROZEN = "frozen"
    ELIGIBLE_FOR_RESEARCH = "eligible_for_research"
    REJECTED = "rejected"

    @property
    def is_immutable(self) -> bool:
        return self in (ArtifactState.FROZEN,
                        ArtifactState.ELIGIBLE_FOR_RESEARCH)

    @property
    def is_research_input(self) -> bool:
        return self is ArtifactState.ELIGIBLE_FOR_RESEARCH


_STATE_TRANSITIONS: Dict[ArtifactState, Tuple[ArtifactState, ...]] = {
    ArtifactState.BUILDING: (ArtifactState.VALIDATED, ArtifactState.REJECTED),
    ArtifactState.VALIDATED: (ArtifactState.FROZEN, ArtifactState.REJECTED),
    ArtifactState.FROZEN: (ArtifactState.ELIGIBLE_FOR_RESEARCH,),
    ArtifactState.ELIGIBLE_FOR_RESEARCH: (),
    ArtifactState.REJECTED: (),
}


class FrozenArtifactError(RuntimeError):
    """Raised on any attempt to change a frozen corpus or snapshot."""


def manifest_hash(payload: Mapping[str, object]) -> str:
    """
    A content hash over a canonical JSON rendering. Sorted keys and no
    whitespace variance, so the same content always hashes the same --
    which is the whole point of putting a hash on a frozen artifact.
    """
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


@dataclass(frozen=True)
class CorpusVersion:
    """
    An immutable identity for one reconstruction of the canonical event
    corpus. `v1` is the existing historical artifact; a recovery
    produces `v2` beside it, never on top of it.
    """
    version_id: str
    created_at: datetime
    code_commit: str
    schema_version: str
    state: ArtifactState = ArtifactState.BUILDING
    recovery_run_id: Optional[str] = None
    earliest_information_time: Optional[datetime] = None
    latest_information_time: Optional[datetime] = None
    counts: Mapping[str, int] = field(default_factory=dict)
    content_hash: Optional[str] = None
    frozen_at: Optional[datetime] = None
    storage_location: str = ""
    note: str = ""

    def __post_init__(self):
        for name in ("created_at", "earliest_information_time",
                     "latest_information_time", "frozen_at"):
            object.__setattr__(self, name, _require_utc(getattr(self, name), name))
        if not self.version_id:
            raise ValueError("version_id is required")
        if not self.code_commit:
            raise ValueError("code_commit is required")
        early, late = self.earliest_information_time, self.latest_information_time
        if early and late and early > late:
            raise ValueError("earliest_information_time is after latest")
        if self.state.is_immutable:
            if not self.content_hash:
                raise FrozenArtifactError(
                    f"{self.version_id} is {self.state.value} without a content hash -- "
                    "an immutable artifact that cannot be verified is not immutable")
            if self.frozen_at is None:
                raise FrozenArtifactError(
                    f"{self.version_id} is {self.state.value} without frozen_at")

    @property
    def information_span_days(self) -> Optional[int]:
        if not (self.earliest_information_time and self.latest_information_time):
            return None
        return (self.latest_information_time
                - self.earliest_information_time).days

    def transition_to(self, state: ArtifactState, *, at: datetime,
                      content_hash: Optional[str] = None) -> "CorpusVersion":
        if self.state.is_immutable and state is not ArtifactState.ELIGIBLE_FOR_RESEARCH:
            raise FrozenArtifactError(
                f"{self.version_id} is {self.state.value} and cannot change")
        if state not in _STATE_TRANSITIONS[self.state]:
            raise FrozenArtifactError(
                f"{self.version_id}: {self.state.value} -> {state.value} is not a "
                f"legal transition")
        at = _require_utc(at, "at")
        return replace(self, state=state,
                       content_hash=content_hash or self.content_hash,
                       frozen_at=at if state.is_immutable else self.frozen_at)

    def describe(self) -> Dict[str, object]:
        return {
            "version_id": self.version_id,
            "state": self.state.value,
            "created_at": self.created_at.isoformat(),
            "code_commit": self.code_commit,
            "schema_version": self.schema_version,
            "recovery_run_id": self.recovery_run_id,
            "earliest_information_time": (self.earliest_information_time.isoformat()
                                          if self.earliest_information_time else None),
            "latest_information_time": (self.latest_information_time.isoformat()
                                        if self.latest_information_time else None),
            "information_span_days": self.information_span_days,
            "counts": dict(self.counts),
            "content_hash": self.content_hash,
            "frozen_at": self.frozen_at.isoformat() if self.frozen_at else None,
            "storage_location": self.storage_location,
            "note": self.note,
        }


@dataclass(frozen=True)
class ResearchSnapshot:
    """
    A research snapshot built from exactly one corpus version.

    Identity is deliberately heavy: an experiment that cannot name the
    snapshot it used is not reproducible, and a snapshot that can be
    confused with an older one destroys the older one's reproducibility
    too.
    """
    snapshot_id: str
    corpus_version_id: str
    dataset_version: DatasetVersion
    created_at: datetime
    code_commit: str
    as_of_cutoff: datetime
    universe_version: str = "v1"
    event_version: str = "v1"
    state: ArtifactState = ArtifactState.BUILDING
    observation_count: int = 0
    instrument_count: int = 0
    cluster_count: int = 0
    earliest_information_time: Optional[datetime] = None
    latest_information_time: Optional[datetime] = None
    horizons: Tuple[str, ...] = ()
    content_hash: Optional[str] = None
    frozen_at: Optional[datetime] = None
    storage_location: str = ""
    note: str = ""

    def __post_init__(self):
        for name in ("created_at", "as_of_cutoff", "earliest_information_time",
                     "latest_information_time", "frozen_at"):
            object.__setattr__(self, name, _require_utc(getattr(self, name), name))
        if not self.snapshot_id:
            raise ValueError("snapshot_id is required")
        if not self.corpus_version_id:
            raise ValueError("a snapshot must name the corpus version it came from")
        if self.cluster_count > self.observation_count:
            raise ValueError("cluster_count cannot exceed observation_count")
        if self.state.is_immutable:
            if not self.content_hash:
                raise FrozenArtifactError(
                    f"{self.snapshot_id} is {self.state.value} without a content hash")
            if self.frozen_at is None:
                raise FrozenArtifactError(
                    f"{self.snapshot_id} is {self.state.value} without frozen_at")

    @property
    def fingerprint(self) -> str:
        """Snapshot identity plus every component version, in one string."""
        return "|".join([self.snapshot_id, self.corpus_version_id,
                         self.dataset_version.fingerprint(),
                         self.universe_version, self.event_version])

    @property
    def information_span_days(self) -> Optional[int]:
        if not (self.earliest_information_time and self.latest_information_time):
            return None
        return (self.latest_information_time
                - self.earliest_information_time).days

    def masquerades_as(self, other: "ResearchSnapshot") -> bool:
        """
        True if this snapshot would be mistaken for `other`. A new
        snapshot must fail this against every old one, or an old
        experiment's recorded result could be re-attributed to data it
        never saw.
        """
        return (self.snapshot_id == other.snapshot_id
                or self.fingerprint == other.fingerprint)

    def transition_to(self, state: ArtifactState, *, at: datetime,
                      content_hash: Optional[str] = None) -> "ResearchSnapshot":
        if self.state.is_immutable and state is not ArtifactState.ELIGIBLE_FOR_RESEARCH:
            raise FrozenArtifactError(
                f"{self.snapshot_id} is {self.state.value} and cannot change")
        if state not in _STATE_TRANSITIONS[self.state]:
            raise FrozenArtifactError(
                f"{self.snapshot_id}: {self.state.value} -> {state.value} is not a "
                f"legal transition")
        at = _require_utc(at, "at")
        return replace(self, state=state,
                       content_hash=content_hash or self.content_hash,
                       frozen_at=at if state.is_immutable else self.frozen_at)

    def describe(self) -> Dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "corpus_version_id": self.corpus_version_id,
            "dataset_version": self.dataset_version.fingerprint(),
            "universe_version": self.universe_version,
            "event_version": self.event_version,
            "fingerprint": self.fingerprint,
            "state": self.state.value,
            "created_at": self.created_at.isoformat(),
            "code_commit": self.code_commit,
            "as_of_cutoff": self.as_of_cutoff.isoformat(),
            "observation_count": self.observation_count,
            "instrument_count": self.instrument_count,
            "cluster_count": self.cluster_count,
            "earliest_information_time": (self.earliest_information_time.isoformat()
                                          if self.earliest_information_time else None),
            "latest_information_time": (self.latest_information_time.isoformat()
                                        if self.latest_information_time else None),
            "information_span_days": self.information_span_days,
            "horizons": list(self.horizons),
            "content_hash": self.content_hash,
            "frozen_at": self.frozen_at.isoformat() if self.frozen_at else None,
            "storage_location": self.storage_location,
            "note": self.note,
        }


# ======================================================================
# Cross-version lineage
# ======================================================================

class CrossVersionRelation(str, Enum):
    """
    How an event in the new corpus relates to the old one. An improved
    pipeline is allowed to cluster differently; what is not allowed is
    pretending the clustering is identical.
    """
    SAME = "same"
    MERGED = "merged"
    SPLIT = "split"
    NEW = "new"
    REMOVED = "removed"
    CHANGED = "changed"

    @property
    def exists_in_old(self) -> bool:
        return self is not CrossVersionRelation.NEW

    @property
    def exists_in_new(self) -> bool:
        return self is not CrossVersionRelation.REMOVED


@dataclass(frozen=True)
class CrossVersionMapping:
    """One old-to-new correspondence, or the explicit absence of one."""
    relation: CrossVersionRelation
    old_corpus_version: str
    new_corpus_version: str
    old_event_ids: Tuple[str, ...] = ()
    new_event_ids: Tuple[str, ...] = ()
    fingerprint: Optional[str] = None
    note: str = ""

    def __post_init__(self):
        if self.old_corpus_version == self.new_corpus_version:
            raise ValueError("a cross-version mapping needs two different versions")
        if self.relation.exists_in_old and not self.old_event_ids:
            raise ValueError(f"{self.relation.value} requires at least one old event id")
        if self.relation.exists_in_new and not self.new_event_ids:
            raise ValueError(f"{self.relation.value} requires at least one new event id")
        if self.relation is CrossVersionRelation.MERGED and len(self.old_event_ids) < 2:
            raise ValueError("MERGED means two or more old events became one")
        if self.relation is CrossVersionRelation.SPLIT and len(self.new_event_ids) < 2:
            raise ValueError("SPLIT means one old event became two or more")

    def describe(self) -> Dict[str, object]:
        return {
            "relation": self.relation.value,
            "old_corpus_version": self.old_corpus_version,
            "new_corpus_version": self.new_corpus_version,
            "old_event_ids": list(self.old_event_ids),
            "new_event_ids": list(self.new_event_ids),
            "fingerprint": self.fingerprint,
            "note": self.note,
        }


# ======================================================================
# Holdout selection, and the rule that makes it legitimate
# ======================================================================

@dataclass(frozen=True)
class FoldFeasibility:
    """
    Whether a research region can hold a legal walk-forward design.
    This is the acceptance test F.6 failed, recomputed for a new
    snapshot.
    """
    horizon_id: str
    research_days: int
    purge_days: float
    embargo_days: float
    test_days: int
    step_days: int
    min_folds: int
    observation_count: int = 0
    cluster_count: int = 0

    def __post_init__(self):
        if self.research_days < 0:
            raise ValueError("research_days cannot be negative")
        if self.min_folds < 1:
            raise ValueError("min_folds must be at least 1")

    @property
    def days_for_first_fold(self) -> float:
        return self.purge_days + self.embargo_days + self.test_days

    @property
    def days_for_min_folds(self) -> float:
        return self.days_for_first_fold + (self.min_folds - 1) * self.step_days

    @property
    def max_folds(self) -> int:
        spare = self.research_days - self.days_for_first_fold
        if spare < 0:
            return 0
        return int(1 + spare // self.step_days)

    @property
    def shortfall_days(self) -> float:
        return max(self.days_for_min_folds - self.research_days, 0.0)

    @property
    def is_viable(self) -> bool:
        return self.max_folds >= self.min_folds

    def describe(self) -> Dict[str, object]:
        return {
            "horizon_id": self.horizon_id,
            "research_days": self.research_days,
            "purge_days": self.purge_days,
            "embargo_days": self.embargo_days,
            "test_days": self.test_days,
            "step_days": self.step_days,
            "min_folds": self.min_folds,
            "days_for_first_fold": self.days_for_first_fold,
            "days_for_min_folds": self.days_for_min_folds,
            "max_folds": self.max_folds,
            "shortfall_days": self.shortfall_days,
            "is_viable": self.is_viable,
            "observation_count": self.observation_count,
            "cluster_count": self.cluster_count,
        }


@dataclass(frozen=True)
class HoldoutDecision:
    """
    A holdout boundary together with the deterministic rule that chose
    it and proof of when it was chosen.

    `observed_model_outcome` must be False. A boundary selected after
    seeing how many folds -- or how good a score -- it produces is not a
    holdout; it is a result. The flag exists so that failure is
    recordable rather than deniable.
    """
    snapshot_id: str
    rule: str
    boundary: datetime
    decided_at: datetime
    earliest_information_time: datetime
    latest_information_time: datetime
    observed_model_outcome: bool = False
    inherited_from: Optional[str] = None
    note: str = ""

    def __post_init__(self):
        for name in ("boundary", "decided_at", "earliest_information_time",
                     "latest_information_time"):
            object.__setattr__(self, name, _require_utc(getattr(self, name), name))
        if not self.rule.strip():
            raise ValueError("a holdout boundary without a stated rule is not a rule")
        if self.observed_model_outcome:
            raise ValueError(
                "this boundary was chosen after a model outcome was observed -- "
                "that is holdout shopping and cannot be frozen as a holdout")
        if not (self.earliest_information_time < self.boundary
                <= self.latest_information_time):
            raise ValueError("boundary must fall inside the information range")

    @property
    def research_days(self) -> int:
        return (self.boundary - self.earliest_information_time).days

    @property
    def holdout_days(self) -> int:
        return (self.latest_information_time - self.boundary).days

    @property
    def holdout_fraction(self) -> Optional[float]:
        total = (self.latest_information_time
                 - self.earliest_information_time).total_seconds()
        if total <= 0:
            return None
        held = (self.latest_information_time - self.boundary).total_seconds()
        return round(held / total, 4)

    def describe(self) -> Dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "rule": self.rule,
            "boundary": self.boundary.isoformat(),
            "decided_at": self.decided_at.isoformat(),
            "earliest_information_time": self.earliest_information_time.isoformat(),
            "latest_information_time": self.latest_information_time.isoformat(),
            "research_days": self.research_days,
            "holdout_days": self.holdout_days,
            "holdout_fraction": self.holdout_fraction,
            "observed_model_outcome": self.observed_model_outcome,
            "inherited_from": self.inherited_from,
            "note": self.note,
        }


# ======================================================================
# The handoff F.6B must consume
# ======================================================================

@dataclass(frozen=True)
class ResearchHandoff:
    """
    What F.6B is allowed to start from, frozen before it runs. F.6B may
    read this; it may not rediscover any of it after seeing a model
    outcome.
    """
    snapshot: ResearchSnapshot
    holdout: HoldoutDecision
    preregistration_path: str
    preregistration_hash: str
    eligible_horizons: Tuple[str, ...]
    feasibility: Tuple[FoldFeasibility, ...]
    effective_sample: Mapping[str, int]
    qualification_gate_reference: str
    candidate_budget: int
    purge_days: Mapping[str, float]
    embargo_days: float
    d20_excluded: bool = True
    note: str = ""

    def __post_init__(self):
        if not self.d20_excluded:
            raise ValueError("the protected horizon cannot be included in a handoff")
        if self.holdout.snapshot_id != self.snapshot.snapshot_id:
            raise ValueError("the holdout decision belongs to a different snapshot")
        if not self.preregistration_hash:
            raise ValueError("a handoff must carry the pre-registration hash")
        if self.candidate_budget < 1:
            raise ValueError("candidate_budget must be at least 1")
        for horizon in self.eligible_horizons:
            if horizon not in self.purge_days:
                raise ValueError(f"no purge horizon declared for {horizon}")

    @property
    def is_research_ready(self) -> bool:
        """
        Every eligible horizon must be able to form its minimum folds,
        and the snapshot must actually be eligible. A handoff that is
        not research ready is still a valid artifact -- it just says NO.
        """
        if not self.snapshot.state.is_research_input:
            return False
        if not self.eligible_horizons:
            return False
        viable = {f.horizon_id for f in self.feasibility if f.is_viable}
        return all(h in viable for h in self.eligible_horizons)

    def describe(self) -> Dict[str, object]:
        return {
            "snapshot": self.snapshot.describe(),
            "holdout": self.holdout.describe(),
            "preregistration_path": self.preregistration_path,
            "preregistration_hash": self.preregistration_hash,
            "eligible_horizons": list(self.eligible_horizons),
            "feasibility": [f.describe() for f in self.feasibility],
            "effective_sample": dict(self.effective_sample),
            "qualification_gate_reference": self.qualification_gate_reference,
            "candidate_budget": self.candidate_budget,
            "purge_days": dict(self.purge_days),
            "embargo_days": self.embargo_days,
            "d20_excluded": self.d20_excluded,
            "is_research_ready": self.is_research_ready,
            "note": self.note,
        }
