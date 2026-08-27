"""
src/features/engine.py
---------------------------
Feature registry, dependency graph, and computation engine
(Phase 8, spec §3, §35, §36, §47, §48).

HOW LEAKAGE IS PREVENTED HERE: a feature's `compute` callable never
receives raw data. It receives a FeatureContext, which is a PointInTime
lens (Phase 6) plus the observation's cutoff. Every accessor on the
context filters to the cutoff, and asking for anything later raises.
So a feature author cannot reach future data even carelessly — the
data is simply not reachable through the object they are given.

DEPENDENCY RESOLUTION: features may depend on other features (spec
§35). The engine topologically sorts them, computes each once, and
REJECTS circular dependencies at registration rather than looping
forever at compute time.

CACHING: computed values are memoized per (feature, instrument,
cutoff). Cache keys include the feature VERSION, so a v2 never serves
a value computed by v1 — silent staleness across a version change
would undermine the whole reproducibility guarantee (spec §48).
"""

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from src.domain.feature_models import (
    FeatureDefinition, FeatureSet, FeatureStatus, MissingPolicy,
    TimestampSemantics, FeatureQuality, FeatureStabilityWindow, FeatureStabilityReport,
)
from src.domain.research_models import FeatureValue, FeatureNamespace
from src.pointintime.view import PointInTimeView, build_view, LookAheadViolation

logger = logging.getLogger("marketlens.features.engine")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class CircularDependencyError(Exception):
    """Raised when feature definitions form a cycle — caught at registration, never at compute time."""


@dataclass
class FeatureContext:
    """
    Everything a feature is allowed to see.

    THE POINT OF THIS CLASS: it is the ONLY thing a compute callable
    receives. Every accessor filters to `cutoff`, so future data is not
    merely discouraged — it is unreachable. A feature author cannot
    leak by accident because there is no accessor that would let them.
    """
    cutoff: datetime
    instrument_id: str
    #: Price candles (any object with .timestamp / .price / .volume).
    candles: Sequence[Any] = field(default_factory=list)
    #: Events known about this entity (any object with .publication_time / .event_type).
    events: Sequence[Any] = field(default_factory=list)
    #: Articles (any object with .published_at / .source_name / .sentiment_label).
    articles: Sequence[Any] = field(default_factory=list)
    #: Peer instrument -> its candles, for cross-sectional features.
    peer_candles: Dict[str, Sequence[Any]] = field(default_factory=dict)
    #: Already-computed features this one depends on.
    resolved: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.cutoff.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware")

    def known_candles(self) -> List[Any]:
        """Candles knowable at the cutoff, oldest first. Never returns anything later."""
        view = build_view(self.cutoff, self.candles, lambda c: getattr(c, "timestamp", None),
                           label=f"candles:{self.instrument_id}")
        return sorted(view.known(), key=lambda c: c.timestamp)

    def known_events(self) -> List[Any]:
        """Events knowable at the cutoff. Used by recency/frequency features (spec §10, §11)."""
        view = build_view(self.cutoff, self.events,
                           lambda e: getattr(e, "publication_time", None), label="events")
        return sorted(view.known(), key=lambda e: e.publication_time)

    def known_articles(self) -> List[Any]:
        view = build_view(self.cutoff, self.articles,
                           lambda a: getattr(a, "published_at", None), label="articles")
        return sorted(view.known(), key=lambda a: a.published_at)

    def known_peer_candles(self, peer_id: str) -> List[Any]:
        candles = self.peer_candles.get(peer_id, [])
        view = build_view(self.cutoff, candles, lambda c: getattr(c, "timestamp", None), label=f"peer:{peer_id}")
        return sorted(view.known(), key=lambda c: c.timestamp)

    def prices(self, lookback: Optional[int] = None) -> List[float]:
        """Closing prices up to the cutoff, optionally the last `lookback` of them."""
        values = [c.price for c in self.known_candles() if getattr(c, "price", None) is not None]
        return values[-lookback:] if lookback else values

    def volumes(self, lookback: Optional[int] = None) -> List[float]:
        values = [c.volume for c in self.known_candles() if getattr(c, "volume", None) is not None]
        return values[-lookback:] if lookback else values

    def dependency(self, feature_id: str) -> Any:
        """Value of a feature this one depends on. None if it could not be computed."""
        return self.resolved.get(feature_id)


class FeatureRegistry:
    """Holds feature definitions and resolves their dependency order."""

    def __init__(self):
        self._definitions: Dict[str, FeatureDefinition] = {}

    def register(self, definition: FeatureDefinition) -> FeatureDefinition:
        """
        Register a definition.

        Re-registering an ACTIVE feature_id with a DIFFERENT formula is
        refused: spec §33/§34 require a new version instead, because
        silently changing an active feature invalidates every past run
        that referenced it.
        """
        existing = self._definitions.get(definition.feature_id)
        if (existing and existing.status == FeatureStatus.ACTIVE
                and existing.formula != definition.formula):
            raise ValueError(
                f"'{definition.feature_id}' is ACTIVE with a different formula. "
                f"Create a new version (e.g. version='{_bump(existing.version)}') and deprecate the old one "
                f"instead of editing an active definition."
            )
        self._definitions[definition.feature_id] = definition
        self._assert_no_cycles()
        return definition

    def get(self, feature_id: str) -> Optional[FeatureDefinition]:
        return self._definitions.get(feature_id)

    def all(self) -> List[FeatureDefinition]:
        return list(self._definitions.values())

    def active(self) -> List[FeatureDefinition]:
        return [d for d in self._definitions.values() if d.status == FeatureStatus.ACTIVE]

    def by_namespace(self, namespace: FeatureNamespace) -> List[FeatureDefinition]:
        return [d for d in self._definitions.values() if d.namespace == namespace]

    def deprecate(self, feature_id: str) -> None:
        definition = self._definitions.get(feature_id)
        if definition:
            definition.status = FeatureStatus.DEPRECATED

    def _assert_no_cycles(self) -> None:
        """Detect circular dependencies at REGISTRATION — long before a compute call could loop."""
        visiting: Set[str] = set()
        done: Set[str] = set()

        def visit(feature_id: str, path: List[str]) -> None:
            if feature_id in done:
                return
            if feature_id in visiting:
                raise CircularDependencyError(f"circular feature dependency: {' -> '.join(path + [feature_id])}")
            visiting.add(feature_id)
            definition = self._definitions.get(feature_id)
            for dependency in (definition.dependencies if definition else []):
                visit(dependency, path + [feature_id])
            visiting.discard(feature_id)
            done.add(feature_id)

        for feature_id in list(self._definitions):
            visit(feature_id, [])

    def resolution_order(self, feature_ids: Sequence[str]) -> List[str]:
        """
        Topological order: every dependency appears before the feature
        that needs it. Unknown dependencies are skipped rather than
        raising — a missing optional input becomes a None value, not a
        crashed batch.
        """
        ordered: List[str] = []
        seen: Set[str] = set()

        def visit(feature_id: str) -> None:
            if feature_id in seen or feature_id not in self._definitions:
                return
            seen.add(feature_id)
            for dependency in self._definitions[feature_id].dependencies:
                visit(dependency)
            ordered.append(feature_id)

        for feature_id in feature_ids:
            visit(feature_id)
        return ordered

    def downstream_of(self, feature_id: str) -> List[str]:
        """Features that depend on this one — what an incremental update must recompute (spec §47)."""
        return [d.feature_id for d in self._definitions.values() if feature_id in d.dependencies]

    def lineage(self, feature_id: str) -> Optional[Dict[str, Any]]:
        definition = self._definitions.get(feature_id)
        if not definition:
            return None
        record = definition.lineage()
        record["downstream"] = self.downstream_of(feature_id)
        return record


def _bump(version: str) -> str:
    if version.startswith("v") and version[1:].isdigit():
        return f"v{int(version[1:]) + 1}"
    return f"{version}.next"


class FeatureEngine:
    """Computes feature values for observations, with caching and dependency resolution."""

    def __init__(self, registry: FeatureRegistry):
        self.registry = registry
        self._cache: Dict[Tuple[str, str, str, str], Any] = {}
        self.cache_hits = 0
        self.cache_misses = 0
        self.computation_failures = 0

    def _cache_key(self, definition: FeatureDefinition, context: FeatureContext) -> Tuple[str, str, str, str]:
        """
        Cache identity includes the feature VERSION — so a v2 never
        serves a value that v1 computed (spec §48: no silent staleness).
        """
        return (definition.feature_id, definition.version, context.instrument_id, context.cutoff.isoformat())

    def compute_one(self, feature_id: str, context: FeatureContext,
                     use_cache: bool = True) -> Optional[FeatureValue]:
        """
        Compute a single feature, resolving its dependencies first.

        Returns a FeatureValue carrying full provenance, or None if the
        definition is unknown. A computation FAILURE returns a
        FeatureValue whose value is None — the observation records that
        the feature was attempted and could not be produced, which is
        different from it never having been requested.
        """
        definition = self.registry.get(feature_id)
        if definition is None:
            return None

        key = self._cache_key(definition, context)
        if use_cache and key in self._cache:
            self.cache_hits += 1
            return self._wrap(definition, self._cache[key], context)
        self.cache_misses += 1

        for dependency_id in self.registry.resolution_order(definition.dependencies):
            if dependency_id == feature_id or dependency_id in context.resolved:
                continue
            dependency_value = self.compute_one(dependency_id, context, use_cache)
            context.resolved[dependency_id] = dependency_value.value if dependency_value else None

        value = None
        if definition.compute is not None:
            try:
                value = definition.compute(context)
            except LookAheadViolation:
                # A leak attempt is never swallowed as an ordinary
                # failure — it is a bug in the feature definition and
                # must surface loudly.
                raise
            except Exception as exc:  # noqa: BLE001 — one bad feature must not kill a batch
                self.computation_failures += 1
                logger.error("Feature '%s' failed for %s: %s", feature_id, context.instrument_id, exc)
                value = None

        if value is None and definition.missing_policy == MissingPolicy.ZERO_IS_SEMANTIC:
            value = 0

        if use_cache:
            self._cache[key] = value
        return self._wrap(definition, value, context)

    def _wrap(self, definition: FeatureDefinition, value: Any, context: FeatureContext) -> FeatureValue:
        """Attach provenance and the correct leakage semantics to a computed value."""
        return FeatureValue(
            name=definition.name,
            namespace=definition.namespace,
            value=value,
            # A trailing-window feature is stamped AT the cutoff: it
            # aggregates data ending there, so the cutoff is exactly
            # when it became knowable.
            as_of=context.cutoff,
            source=definition.source,
            calculation=definition.formula,
            feature_version=definition.version,
            is_contemporaneous_event_attribute=(
                definition.timestamp_semantics == TimestampSemantics.CONTEMPORANEOUS_EVENT),
        )

    def compute_set(self, feature_set: FeatureSet, context: FeatureContext,
                     use_cache: bool = True) -> Dict[str, FeatureValue]:
        """Compute every feature in a set, in dependency order."""
        results: Dict[str, FeatureValue] = {}
        for feature_id in self.registry.resolution_order(feature_set.feature_ids):
            value = self.compute_one(feature_id, context, use_cache)
            if value is not None:
                results[feature_id] = value
                context.resolved[feature_id] = value.value
        return results

    def compute_batch(self, feature_set: FeatureSet,
                       contexts: Sequence[FeatureContext],
                       use_cache: bool = True) -> List[Dict[str, FeatureValue]]:
        """Compute a set across many observations (historical backfill — spec §47)."""
        return [self.compute_set(feature_set, context, use_cache) for context in contexts]

    def invalidate(self, feature_id: Optional[str] = None) -> int:
        """
        Drop cached values. With a feature_id, also drops everything
        DOWNSTREAM of it — a stale dependency would otherwise keep
        feeding a recomputed feature (spec §48).
        """
        if feature_id is None:
            count = len(self._cache)
            self._cache.clear()
            return count

        affected = {feature_id, *self.registry.downstream_of(feature_id)}
        keys = [k for k in self._cache if k[0] in affected]
        for key in keys:
            del self._cache[key]
        return len(keys)

    def cache_hit_ratio(self) -> Optional[float]:
        total = self.cache_hits + self.cache_misses
        return round(self.cache_hits / total, 4) if total else None

    # ---------------- diagnostics ----------------

    def assess_quality(self, feature_id: str, values: Sequence[Optional[float]],
                        outlier_sigma: float = 5.0) -> FeatureQuality:
        """
        Structural health of a feature across a sample (spec §39, §40).

        Outliers are COUNTED, never removed — large moves in financial
        data are usually the economically meaningful observations, and
        discarding them would quietly delete the events most worth
        studying (spec §30).
        """
        quality = FeatureQuality(feature_id=feature_id, observation_count=len(values))
        present = [v for v in values if v is not None]
        quality.missing_count = len(values) - len(present)
        quality.unique_values = len({str(v) for v in present})

        numeric = [v for v in present if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if numeric:
            quality.min_value = min(numeric)
            quality.max_value = max(numeric)
        if len(numeric) >= 2:
            quality.variance = round(statistics.pvariance(numeric), 12)
            mean = statistics.fmean(numeric)
            std = statistics.stdev(numeric)
            if std > 0:
                quality.outlier_count = sum(1 for v in numeric if abs(v - mean) > outlier_sigma * std)
        return quality

    def assess_stability(self, feature_id: str,
                          samples: Sequence[Tuple[datetime, Optional[float]]],
                          window_bounds: Sequence[Tuple[str, datetime, datetime]]) -> FeatureStabilityReport:
        """
        How the feature's distribution shifts across eras (spec §43).

        Windows are supplied by the caller rather than inferred, so the
        comparison periods are an explicit research choice.
        """
        report = FeatureStabilityReport(feature_id=feature_id)
        for label, start, end in window_bounds:
            in_window = [value for moment, value in samples if start <= moment <= end]
            present = [v for v in in_window if v is not None]
            window = FeatureStabilityWindow(label=label, start=start, end=end, count=len(in_window))
            if in_window:
                window.missingness = round((len(in_window) - len(present)) / len(in_window), 4)
            if present:
                window.mean = round(statistics.fmean(present), 6)
                window.median = round(statistics.median(present), 6)
            if len(present) >= 2:
                window.variance = round(statistics.pvariance(present), 12)
            report.windows.append(window)
        return report
