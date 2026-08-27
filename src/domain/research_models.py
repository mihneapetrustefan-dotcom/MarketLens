"""
src/domain/research_models.py
----------------------------------
Research dataset models (Phase 7).

THE ONE STRUCTURAL COMMITMENT THIS FILE MAKES (spec §6, §27):
features and labels live in SEPARATE, differently-typed containers
that cannot be merged by accident.

    InformationSnapshot   what was knowable at the cutoff  -> X
    OutcomeSet            what happened after the cutoff   -> Y

A ResearchObservation holds both, but there is deliberately NO method
that returns them combined as one flat row. Building a training matrix
requires calling the feature accessor and the label accessor
separately, so a leak has to be written on purpose rather than
happening because someone iterated a dict.

WHY THAT MATTERS MORE THAN IT SOUNDS: leakage does not usually arrive
as a deliberate mistake. It arrives as `dict(observation)` in a
notebook at 1am. The type system is the only defence that survives
that.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


def _require_utc(value: Optional[datetime], name: str) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (got a naive datetime)")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be in UTC (got offset {value.utcoffset()})")
    return value


# ============================================================
# Versioning (spec §21, §22, §23)
# ============================================================

@dataclass(frozen=True)
class DatasetVersion:
    """
    Identifies everything that determines what a dataset contains
    (spec §21). Frozen: a version that could be mutated after the fact
    would make "reproducible against version X" meaningless.
    """
    version: str
    event_taxonomy_version: str = "v1"
    feature_set_version: str = "v1"
    label_set_version: str = "v1"
    benchmark_definition_version: str = "v1"
    regime_definition_version: str = "v1"
    preprocessing_version: str = "v1"
    created_at: Optional[datetime] = None
    notes: str = ""

    def __post_init__(self):
        object.__setattr__(self, "created_at", _require_utc(self.created_at, "created_at"))

    def fingerprint(self) -> str:
        """A single comparable string — two datasets match only if every component version matches."""
        return "|".join([
            self.version, self.event_taxonomy_version, self.feature_set_version,
            self.label_set_version, self.benchmark_definition_version,
            self.regime_definition_version, self.preprocessing_version,
        ])


class FeatureNamespace(str, Enum):
    """
    Spec §41 (Phase 7) / §4 (Phase 8) — clear namespaces instead of one
    unstructured feature table.

    EXTENDED IN PHASE 8: NEWS, PEER, TECHNICAL, FUNDAMENTAL,
    CROSS_SECTIONAL and HISTORICAL were added when Phase 8's feature
    library needed them. Adding members to this enum is additive and
    safe — every existing feature keeps its namespace unchanged, and
    no stored value is reinterpreted.
    """
    MARKET = "market"
    SECTOR = "sector"
    ENTITY = "entity"
    EVENT = "event"
    MACRO = "macro"
    VOLATILITY = "volatility"
    LIQUIDITY = "liquidity"
    SENTIMENT = "sentiment"
    REGIME = "regime"
    RELATIONSHIP = "relationship"
    NEWS = "news"
    PEER = "peer"
    TECHNICAL = "technical"
    FUNDAMENTAL = "fundamental"
    CROSS_SECTIONAL = "cross_sectional"
    HISTORICAL = "historical"


@dataclass
class FeatureValue:
    """
    One feature, with the provenance needed to validate it later
    (spec §42).

    `as_of` is not decoration: it is what the leakage check compares
    against the information cutoff. A feature without an `as_of` cannot
    be proven to predate the cutoff, and is therefore rejected rather
    than trusted.
    """
    name: str
    namespace: FeatureNamespace
    value: Any
    as_of: Optional[datetime] = None
    source: str = ""
    calculation: str = ""
    feature_version: str = "v1"
    is_contemporaneous_event_attribute: bool = False   # spec §27's stated exception

    def __post_init__(self):
        self.as_of = _require_utc(self.as_of, "as_of")

    @property
    def qualified_name(self) -> str:
        return f"{self.namespace.value}.{self.name}"


@dataclass
class LabelValue:
    """
    One outcome/label, measured strictly AFTER the information cutoff.

    `measured_at` is likewise load-bearing: a label timestamped at or
    before the cutoff is not an outcome, it is a feature that wandered
    into the wrong container, and validation rejects it.
    """
    name: str
    value: Any
    measured_at: Optional[datetime] = None
    window_name: str = ""
    label_version: str = "v1"
    calculation: str = ""

    def __post_init__(self):
        self.measured_at = _require_utc(self.measured_at, "measured_at")


# ============================================================
# Quality (spec §19, §20)
# ============================================================

class ResearchQuality(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INVALID = "invalid"


class ExclusionReason(str, Enum):
    """Spec §20 — bad observations are marked and kept, never silently deleted."""
    MISSING_EVENT_TIMESTAMP = "missing_event_timestamp"
    IMPOSSIBLE_TIMESTAMP = "impossible_timestamp"
    INSUFFICIENT_PRICE_DATA = "insufficient_price_data"
    UNRESOLVED_INSTRUMENT = "unresolved_instrument"
    CORRUPTED_EVENT = "corrupted_event"
    SEVERE_DATA_GAPS = "severe_data_gaps"
    NO_BENCHMARK = "no_benchmark"
    LEAKAGE_DETECTED = "leakage_detected"
    INCOMPLETE_OUTCOME = "incomplete_outcome"


@dataclass
class SampleQuality:
    """Per-observation quality assessment (spec §19)."""
    level: ResearchQuality = ResearchQuality.HIGH
    exclusions: List[ExclusionReason] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    event_timestamp_quality: Optional[float] = None
    source_quality: Optional[float] = None
    market_data_completeness: Optional[float] = None
    entity_resolution_confidence: Optional[float] = None
    event_fusion_confidence: Optional[float] = None
    outcome_completeness: Optional[float] = None

    def exclude(self, reason: ExclusionReason, note: str = "") -> None:
        if reason not in self.exclusions:
            self.exclusions.append(reason)
        if note:
            self.notes.append(note)
        self.level = ResearchQuality.INVALID

    def downgrade(self, level: ResearchQuality, note: str = "") -> None:
        order = [ResearchQuality.HIGH, ResearchQuality.MEDIUM, ResearchQuality.LOW, ResearchQuality.INVALID]
        if order.index(level) > order.index(self.level):
            self.level = level
        if note:
            self.notes.append(note)

    @property
    def is_usable(self) -> bool:
        return self.level != ResearchQuality.INVALID


# ============================================================
# Information vs outcome (spec §3, §4, §5)
# ============================================================

@dataclass
class InformationSnapshot:
    """
    Everything knowable at `information_cutoff` — the X side.

    Contains ONLY features. There is no field here that could hold a
    future return, and `validate()` rejects any feature timestamped
    after the cutoff.
    """
    information_cutoff: datetime
    features: Dict[str, FeatureValue] = field(default_factory=dict)
    known_event_ids: List[str] = field(default_factory=list)   # related events known AT the time
    cutoff_basis: str = ""                                      # how the cutoff was derived

    def __post_init__(self):
        self.information_cutoff = _require_utc(self.information_cutoff, "information_cutoff")

    def add(self, feature: FeatureValue) -> None:
        self.features[feature.qualified_name] = feature

    def get(self, qualified_name: str) -> Optional[FeatureValue]:
        return self.features.get(qualified_name)

    def validate(self) -> List[str]:
        """
        Return a list of leakage violations — empty means clean
        (spec §27).

        Rule: FEATURE_TIMESTAMP <= INFORMATION_CUTOFF_TIME, except for
        features explicitly flagged as contemporaneous event
        attributes (the event's own type, for instance, is known
        exactly at the event and is not a leak).
        """
        violations = []
        for name, feature in self.features.items():
            if feature.is_contemporaneous_event_attribute:
                continue
            if feature.as_of is None:
                violations.append(f"{name}: no as_of timestamp — cannot prove it predates the cutoff")
            elif feature.as_of > self.information_cutoff:
                violations.append(
                    f"{name}: as_of {feature.as_of.isoformat()} is AFTER the cutoff "
                    f"{self.information_cutoff.isoformat()}")
        return violations

    def to_feature_dict(self) -> Dict[str, Any]:
        """Flatten to name -> value, for building a feature matrix. Deliberately returns FEATURES ONLY."""
        return {name: feature.value for name, feature in self.features.items()}


@dataclass
class OutcomeSet:
    """
    Everything that happened AFTER the cutoff — the Y side.

    Kept in its own type so a feature matrix builder physically cannot
    pick these up by iterating the snapshot.
    """
    information_cutoff: datetime
    labels: Dict[str, LabelValue] = field(default_factory=dict)

    def __post_init__(self):
        self.information_cutoff = _require_utc(self.information_cutoff, "information_cutoff")

    def add(self, label: LabelValue) -> None:
        self.labels[label.name] = label

    def get(self, name: str) -> Optional[LabelValue]:
        return self.labels.get(name)

    def validate(self) -> List[str]:
        """
        Return violations — empty means clean.

        Rule: OUTCOME_TIMESTAMP > INFORMATION_CUTOFF_TIME. A label at
        or before the cutoff is not an outcome; treating it as one
        would mean training a model on something it could already see.
        """
        violations = []
        for name, label in self.labels.items():
            if label.measured_at is None:
                violations.append(f"{name}: no measured_at timestamp")
            elif label.measured_at <= self.information_cutoff:
                violations.append(
                    f"{name}: measured_at {label.measured_at.isoformat()} is NOT after the cutoff "
                    f"{self.information_cutoff.isoformat()} — this is a feature, not an outcome")
        return violations

    def to_label_dict(self) -> Dict[str, Any]:
        return {name: label.value for name, label in self.labels.items()}


# ============================================================
# Research observation (spec §2)
# ============================================================

@dataclass
class ResearchObservation:
    """
    One event/instrument/time relationship, ready for analysis.

    NOTE THE ABSENCE: there is no `to_row()` or `as_dict()` that
    merges features and labels. Producing a training row requires
    calling `information.to_feature_dict()` and
    `outcomes.to_label_dict()` separately — see the module docstring
    for why that friction is the point.
    """
    observation_id: str
    event_id: str
    instrument_id: str
    benchmark_id: Optional[str] = None

    event_type: Optional[str] = None
    event_time: Optional[datetime] = None
    information_time: Optional[datetime] = None      # when the info became usable
    observation_created_at: Optional[datetime] = None

    sector_id: Optional[str] = None
    geography: Optional[str] = None
    market_regime: Optional[str] = None

    information: Optional[InformationSnapshot] = None
    outcomes: Optional[OutcomeSet] = None

    quality: SampleQuality = field(default_factory=SampleQuality)
    dataset_version: Optional[str] = None
    label_version: str = "v1"
    feature_version: str = "v1"

    # Spec §31, §32: observations from one catalyst are NOT independent
    # samples. Carrying the cluster id is what lets later research
    # cluster its standard errors instead of counting one story as
    # dozens of observations.
    event_cluster_id: Optional[str] = None

    def __post_init__(self):
        for name in ("event_time", "information_time", "observation_created_at"):
            setattr(self, name, _require_utc(getattr(self, name), name))

    @property
    def information_cutoff(self) -> Optional[datetime]:
        return self.information.information_cutoff if self.information else None

    def validate(self) -> List[str]:
        """Full leakage validation across both sides. Empty list means the observation is clean."""
        violations = []
        if self.information is None:
            violations.append("no information snapshot")
        else:
            violations.extend(self.information.validate())
        if self.outcomes is None:
            violations.append("no outcome set")
        else:
            violations.extend(self.outcomes.validate())
        if (self.information and self.outcomes
                and self.information.information_cutoff != self.outcomes.information_cutoff):
            violations.append("information and outcome cutoffs disagree")
        return violations


# ============================================================
# Cohorts (spec §7, §8, §9)
# ============================================================

@dataclass
class CohortDefinition:
    """
    Explicit, VERSIONED criteria for a group of events (spec §8).

    Versioned because a cohort whose definition can drift silently
    makes every past result built on it unreproducible — the finding
    would no longer refer to the same sample.
    """
    cohort_id: str
    name: str
    version: str = "v1"
    description: str = ""

    event_types: List[str] = field(default_factory=list)
    entity_ids: List[str] = field(default_factory=list)
    sector_ids: List[str] = field(default_factory=list)
    geographies: List[str] = field(default_factory=list)
    market_regimes: List[str] = field(default_factory=list)

    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    min_event_confidence: Optional[float] = None
    min_quality: ResearchQuality = ResearchQuality.LOW

    created_at: Optional[datetime] = None

    def __post_init__(self):
        for name in ("start_time", "end_time", "created_at"):
            setattr(self, name, _require_utc(getattr(self, name), name))

    def fingerprint(self) -> str:
        """Identity of the DEFINITION — changing any criterion changes this, so results stay traceable to their exact sample."""
        parts = [
            self.cohort_id, self.version,
            ",".join(sorted(self.event_types)), ",".join(sorted(self.entity_ids)),
            ",".join(sorted(self.sector_ids)), ",".join(sorted(self.geographies)),
            ",".join(sorted(self.market_regimes)),
            self.start_time.isoformat() if self.start_time else "",
            self.end_time.isoformat() if self.end_time else "",
            str(self.min_event_confidence), self.min_quality.value,
        ]
        return "|".join(parts)


# ============================================================
# Research runs & results (spec §34, §35, §36, §39, §40)
# ============================================================

class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ResearchRun:
    """
    A recorded research execution (spec §35).

    Every run is stored, INCLUDING the ones that found nothing (spec
    §40). That is the entire point: a research history containing only
    the successful experiments is how a project convinces itself of a
    pattern that is really just the best of two hundred tries.
    """
    run_id: str
    cohort_id: Optional[str] = None
    cohort_fingerprint: Optional[str] = None
    dataset_version: Optional[str] = None
    feature_set_version: str = "v1"
    label_set_version: str = "v1"
    parameters: Dict[str, Any] = field(default_factory=dict)
    status: RunStatus = RunStatus.RUNNING
    sample_size: int = 0
    created_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None

    def __post_init__(self):
        for name in ("created_at", "completed_at"):
            setattr(self, name, _require_utc(getattr(self, name), name))

    def reproducibility_key(self) -> str:
        """Everything needed to recreate this run. Two runs with the same key must produce the same result."""
        parameter_part = ";".join(f"{k}={v}" for k, v in sorted(self.parameters.items()))
        return "|".join([
            self.cohort_fingerprint or "", self.dataset_version or "",
            self.feature_set_version, self.label_set_version, parameter_part,
        ])


@dataclass
class ResearchResult:
    """
    Descriptive statistics from one run (spec §36).

    `small_sample` and `observation_count` vs `cluster_count` are both
    surfaced because they answer different questions: 400 observations
    drawn from 12 event clusters is not a 400-sample study, and
    presenting it as one overstates the evidence badly (spec §31, §32).
    """
    result_id: str
    run_id: str
    label_name: str
    observation_count: int = 0
    cluster_count: Optional[int] = None
    mean: Optional[float] = None
    median: Optional[float] = None
    std_dev: Optional[float] = None
    p25: Optional[float] = None
    p75: Optional[float] = None
    hit_rate: Optional[float] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    small_sample: bool = True
    methodology_note: str = ""
    created_at: Optional[datetime] = None

    MIN_MEANINGFUL_SAMPLE = 30

    def __post_init__(self):
        self.created_at = _require_utc(self.created_at, "created_at")

    @property
    def effective_sample_warning(self) -> Optional[str]:
        """
        A stated caution when observations vastly outnumber independent
        clusters — the single most common way an event study overstates
        its own evidence.
        """
        if self.cluster_count and self.observation_count >= self.cluster_count * 3:
            return (f"{self.observation_count} observations come from only {self.cluster_count} event clusters; "
                     f"treat the effective sample size as closer to {self.cluster_count}")
        return None
