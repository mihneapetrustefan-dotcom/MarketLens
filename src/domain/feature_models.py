"""
src/domain/feature_models.py
---------------------------------
Feature definition, feature set, and diagnostics models (Phase 8).

WHAT THIS FILE IS FOR: a feature in this system is not a number — it
is a NUMBER PLUS ITS DEFINITION. Two researchers who both computed
"5-day momentum" have not computed the same thing unless the formula,
the lookback, the missing-data policy and the version all match. This
file makes that identity explicit so a historical result stays
reproducible.

THE VERSIONING RULE (spec §33, §34): an ACTIVE feature definition is
never edited in place. Changing a formula means creating v2 and
DEPRECATING v1 — v1 must keep producing the values it always produced,
or every research run that referenced it silently becomes wrong.

THE ONE THING DELIBERATELY ABSENT: there is no field anywhere here for
predictive power, importance, or model contribution. Per spec §41/§42,
this phase measures feature HEALTH (coverage, variance, stability) and
must not let outcome labels influence what counts as a good feature —
that is how a feature set gets fitted to its own test data.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

from src.domain.research_models import FeatureNamespace


def _require_utc(value: Optional[datetime], name: str) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (got a naive datetime)")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be in UTC (got offset {value.utcoffset()})")
    return value


class FeatureStatus(str, Enum):
    """Spec §33. An ACTIVE definition is frozen — see the module docstring."""
    DRAFT = "draft"
    ACTIVE = "active"
    DEPRECATED = "deprecated"


class MissingPolicy(str, Enum):
    """
    What a feature does when its inputs are unavailable (spec §29).

    ZERO_IS_SEMANTIC exists as its own value precisely because
    defaulting missing data to 0 is usually WRONG — "no articles today"
    genuinely is zero, but "volatility unavailable" is not zero
    volatility. Requiring the author to pick makes that distinction
    deliberate rather than accidental.
    """
    MISSING = "missing"                  # propagate None
    NOT_APPLICABLE = "not_applicable"    # feature doesn't apply to this instrument type
    INSUFFICIENT_HISTORY = "insufficient_history"
    ZERO_IS_SEMANTIC = "zero_is_semantic"


class ComputationCost(str, Enum):
    """Spec §46 — so expensive features are identifiable before a million-row backfill, not after."""
    CHEAP = "cheap"
    MODERATE = "moderate"
    EXPENSIVE = "expensive"


class TimestampSemantics(str, Enum):
    """
    What the feature's timestamp MEANS — which determines how leakage
    is checked against it (spec §31).
    """
    AS_OF_CUTOFF = "as_of_cutoff"                # value as known at the cutoff
    TRAILING_WINDOW = "trailing_window"          # aggregate over a window ENDING at the cutoff
    CONTEMPORANEOUS_EVENT = "contemporaneous_event"   # the event's own attribute; the §27 exception


@dataclass
class FeatureDefinition:
    """
    The formal identity of one feature (spec §3, §33).

    `compute` is the callable that produces the value. It receives a
    FeatureContext (see engine.py) and must return a single value or
    None — never raise, never reach outside the context, since the
    context is what enforces the information cutoff.
    """
    feature_id: str
    name: str
    namespace: FeatureNamespace
    version: str = "v1"
    description: str = ""
    formula: str = ""
    dependencies: List[str] = field(default_factory=list)   # other feature_ids
    lookback_periods: Optional[int] = None
    timestamp_semantics: TimestampSemantics = TimestampSemantics.AS_OF_CUTOFF
    missing_policy: MissingPolicy = MissingPolicy.MISSING
    output_type: str = "float"
    cost: ComputationCost = ComputationCost.CHEAP
    status: FeatureStatus = FeatureStatus.ACTIVE
    source: str = ""
    created_at: Optional[datetime] = None
    compute: Optional[Callable[[Any], Any]] = None

    def __post_init__(self):
        self.created_at = _require_utc(self.created_at, "created_at")

    @property
    def qualified_id(self) -> str:
        """Namespace-qualified, version-pinned identity — what a research run actually references."""
        return f"{self.namespace.value}.{self.name}@{self.version}"

    def lineage(self) -> Dict[str, Any]:
        """Full traceability for this feature (spec §38) — queryable, not just documented in a comment."""
        return {
            "feature_id": self.feature_id,
            "qualified_id": self.qualified_id,
            "namespace": self.namespace.value,
            "version": self.version,
            "formula": self.formula,
            "source": self.source,
            "dependencies": list(self.dependencies),
            "lookback_periods": self.lookback_periods,
            "timestamp_semantics": self.timestamp_semantics.value,
            "missing_policy": self.missing_policy.value,
            "status": self.status.value,
        }


@dataclass
class FeatureSet:
    """
    A named, versioned collection of feature definitions (spec §51, §52).

    Membership changes require a NEW version. A research run references
    a feature set version, so silently adding a feature to an existing
    set would retroactively change what past runs meant.
    """
    feature_set_id: str
    name: str
    version: str = "v1"
    description: str = ""
    feature_ids: List[str] = field(default_factory=list)
    created_at: Optional[datetime] = None

    def __post_init__(self):
        self.created_at = _require_utc(self.created_at, "created_at")

    def fingerprint(self) -> str:
        """Identity of the SET — changing membership changes this, keeping past runs traceable."""
        return f"{self.feature_set_id}@{self.version}:" + ",".join(sorted(self.feature_ids))


@dataclass
class FeatureQuality:
    """
    Health diagnostics for one feature across a sample (spec §39, §40).

    Deliberately measures STRUCTURE, not predictive power. A feature
    flagged here is broken or degenerate — constant, empty, impossible
    — not merely unhelpful for a particular model.
    """
    feature_id: str
    observation_count: int = 0
    missing_count: int = 0
    unique_values: int = 0
    variance: Optional[float] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    outlier_count: int = 0
    computation_failures: int = 0
    point_in_time_violations: int = 0

    @property
    def missingness(self) -> Optional[float]:
        if not self.observation_count:
            return None
        return round(self.missing_count / self.observation_count, 4)

    @property
    def coverage(self) -> Optional[float]:
        missing = self.missingness
        return None if missing is None else round(1.0 - missing, 4)

    @property
    def is_constant(self) -> bool:
        return self.observation_count > 1 and self.unique_values <= 1

    @property
    def is_near_constant(self) -> bool:
        return self.variance is not None and self.variance < 1e-12 and self.observation_count > 1

    def problems(self) -> List[str]:
        """
        Named problems — FLAGGED, never auto-deleted (spec §40). A
        constant feature today may be a broken pipeline, or may be a
        legitimately rare event indicator; that judgement is not this
        function's to make.
        """
        found = []
        if self.observation_count == 0:
            found.append("no observations")
            return found
        if self.is_constant:
            found.append("constant: only one distinct value")
        elif self.is_near_constant:
            found.append("near-constant: variance is effectively zero")
        if self.missingness is not None and self.missingness > 0.5:
            found.append(f"high missingness: {self.missingness:.0%}")
        if self.computation_failures:
            found.append(f"{self.computation_failures} computation failure(s)")
        if self.point_in_time_violations:
            found.append(f"{self.point_in_time_violations} POINT-IN-TIME VIOLATION(S) — leakage")
        return found


@dataclass
class FeatureStabilityWindow:
    """Distribution of one feature over one time window (spec §43)."""
    label: str
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    count: int = 0
    mean: Optional[float] = None
    median: Optional[float] = None
    variance: Optional[float] = None
    missingness: Optional[float] = None

    def __post_init__(self):
        self.start = _require_utc(self.start, "start")
        self.end = _require_utc(self.end, "end")


@dataclass
class FeatureStabilityReport:
    """
    How a feature's distribution shifts across eras (spec §43).

    Matters because a feature whose meaning drifts — a volume measure
    that changed scale after a data-provider switch, say — will train a
    model on one regime and deploy it into another.
    """
    feature_id: str
    windows: List[FeatureStabilityWindow] = field(default_factory=list)

    def mean_drift(self) -> Optional[float]:
        """Absolute change in mean between the first and last window with data."""
        usable = [w for w in self.windows if w.mean is not None]
        if len(usable) < 2:
            return None
        return round(abs(usable[-1].mean - usable[0].mean), 6)

    def has_distribution_shift(self, threshold_ratio: float = 2.0) -> bool:
        """
        Whether the distribution moved enough to be worth investigating.

        Compares the mean drift against the earliest window's own
        standard deviation — a shift is only meaningful relative to the
        feature's natural spread, not in absolute units.
        """
        usable = [w for w in self.windows if w.mean is not None and w.variance is not None]
        if len(usable) < 2:
            return False
        baseline_std = usable[0].variance ** 0.5
        if baseline_std == 0:
            return usable[-1].mean != usable[0].mean
        return abs(usable[-1].mean - usable[0].mean) > threshold_ratio * baseline_std
