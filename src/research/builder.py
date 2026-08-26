"""
src/research/builder.py
----------------------------
Cohort system, dataset builder, and research-run registry
(Phase 7, spec §7-§9, §24, §29, §30, §33-§40, §43).

THE LEAKAGE GATE: build_matrices() validates EVERY observation before
including it, and an observation with any violation is EXCLUDED from
the matrices — not merely flagged. A leaking row that ships with a
warning attached will end up in someone's training set anyway; a
leaking row that never reaches the matrix cannot.

TRAIN/TEST SPLITTING IS CHRONOLOGICAL, ALWAYS (spec §29): there is
deliberately no shuffle option. Random splitting of financial
time-series is the most common way a backtest reports results it
could never have achieved, and offering the option — even off by
default — is offering the mistake.
"""

import csv
import json
import uuid
import logging
import statistics
from datetime import datetime, timezone
from io import StringIO
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.domain.research_models import (
    ResearchObservation, CohortDefinition, DatasetVersion, ResearchQuality,
    ResearchRun, RunStatus, ResearchResult, ExclusionReason,
)

logger = logging.getLogger("marketlens.research.builder")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


_QUALITY_ORDER = [ResearchQuality.INVALID, ResearchQuality.LOW,
                   ResearchQuality.MEDIUM, ResearchQuality.HIGH]


def _quality_at_least(actual: ResearchQuality, minimum: ResearchQuality) -> bool:
    return _QUALITY_ORDER.index(actual) >= _QUALITY_ORDER.index(minimum)


class CohortEngine:
    """
    Builds cohort membership from explicit, versioned criteria
    (spec §7-§9).

    Membership is REGENERATED from the definition rather than frozen
    into a copied dataset — so a cohort stays a query, and re-running
    it later against the same dataset version reproduces the same
    sample.
    """

    def __init__(self):
        self.definitions: Dict[str, CohortDefinition] = {}

    def register(self, definition: CohortDefinition) -> CohortDefinition:
        """Register (or re-register) a cohort definition, keyed by id AND version so old versions survive."""
        key = f"{definition.cohort_id}:{definition.version}"
        self.definitions[key] = definition
        return definition

    def get(self, cohort_id: str, version: str = "v1") -> Optional[CohortDefinition]:
        return self.definitions.get(f"{cohort_id}:{version}")

    def matches(self, observation: ResearchObservation, definition: CohortDefinition) -> bool:
        """Whether one observation satisfies every criterion. All criteria are AND-ed; empty criteria mean 'no constraint'."""
        if definition.event_types and observation.event_type not in definition.event_types:
            return False
        if definition.entity_ids and observation.instrument_id not in definition.entity_ids:
            return False
        if definition.sector_ids and observation.sector_id not in definition.sector_ids:
            return False
        if definition.geographies and observation.geography not in definition.geographies:
            return False
        if definition.market_regimes and observation.market_regime not in definition.market_regimes:
            return False

        moment = observation.event_time or observation.information_time
        if definition.start_time and (moment is None or moment < definition.start_time):
            return False
        if definition.end_time and (moment is None or moment > definition.end_time):
            return False

        if not _quality_at_least(observation.quality.level, definition.min_quality):
            return False
        if definition.min_event_confidence is not None:
            confidence = observation.quality.event_fusion_confidence
            if confidence is None or confidence < definition.min_event_confidence:
                return False
        return True

    def build_membership(self, observations: Sequence[ResearchObservation],
                          definition: CohortDefinition) -> List[ResearchObservation]:
        """Regenerate cohort membership. Deterministic: same inputs, same definition, same members."""
        members = [o for o in observations if self.matches(o, definition)]
        logger.info("Cohort '%s' (%s): %d of %d observations matched",
                     definition.name, definition.version, len(members), len(observations))
        return members


class DatasetBuilder:
    """Builds feature (X) and outcome (Y) matrices from research observations."""

    def __init__(self, dataset_version: DatasetVersion):
        self.dataset_version = dataset_version

    def build_matrices(
        self,
        observations: Sequence[ResearchObservation],
        label_name: str,
        require_quality: ResearchQuality = ResearchQuality.LOW,
    ) -> Dict[str, Any]:
        """
        Build X and Y, EXCLUDING any observation that fails validation.

        Returns:
            {"X": [...], "Y": [...], "observation_ids": [...],
             "cluster_ids": [...], "feature_names": [...],
             "excluded": [{observation_id, reasons}], "included_count",
             "excluded_count"}

        X and Y are returned as separate lists, aligned by index —
        never as merged rows, so nothing downstream can accidentally
        treat a label as a feature.
        """
        feature_rows: List[Dict[str, Any]] = []
        labels: List[Any] = []
        observation_ids: List[str] = []
        cluster_ids: List[Optional[str]] = []
        excluded: List[Dict[str, Any]] = []

        for observation in observations:
            reasons: List[str] = []

            if not _quality_at_least(observation.quality.level, require_quality):
                reasons.append(f"quality {observation.quality.level.value} below required {require_quality.value}")

            violations = observation.validate()
            if violations:
                reasons.extend(violations)

            label = observation.outcomes.get(label_name) if observation.outcomes else None
            if label is None:
                reasons.append(f"label '{label_name}' missing")
            elif label.value is None:
                reasons.append(f"label '{label_name}' has no value")

            if reasons:
                excluded.append({"observation_id": observation.observation_id, "reasons": reasons})
                continue

            feature_rows.append(observation.information.to_feature_dict())
            labels.append(label.value)
            observation_ids.append(observation.observation_id)
            cluster_ids.append(observation.event_cluster_id)

        feature_names = sorted({name for row in feature_rows for name in row})
        # Align every row to the same column order; a feature absent
        # from a given observation becomes None rather than shifting
        # the columns.
        aligned = [[row.get(name) for name in feature_names] for row in feature_rows]

        logger.info("Dataset build: %d included, %d excluded, %d features",
                     len(aligned), len(excluded), len(feature_names))
        return {
            "X": aligned, "Y": labels,
            "observation_ids": observation_ids, "cluster_ids": cluster_ids,
            "feature_names": feature_names,
            "excluded": excluded,
            "included_count": len(aligned), "excluded_count": len(excluded),
            "dataset_version": self.dataset_version.fingerprint(),
            "label_name": label_name,
        }

    def chronological_split(
        self,
        observations: Sequence[ResearchObservation],
        train_end: datetime,
        validation_end: Optional[datetime] = None,
    ) -> Dict[str, List[ResearchObservation]]:
        """
        Split TRAIN / VALIDATION / TEST by time (spec §29, §30).

        There is no `shuffle` parameter, by design. Random splitting
        puts future observations in the training set, which is exactly
        the leak this whole phase exists to prevent — so the option
        does not exist to be misused.
        """
        train, validation, test = [], [], []
        for observation in observations:
            moment = observation.information_time or observation.event_time
            if moment is None:
                continue   # undated observations cannot be placed in a temporal split
            if moment <= train_end:
                train.append(observation)
            elif validation_end is None or moment <= validation_end:
                (validation if validation_end else test).append(observation)
            else:
                test.append(observation)
        return {"train": train, "validation": validation, "test": test}

    def walk_forward_windows(
        self,
        start: datetime,
        end: datetime,
        train_months: int = 48,
        test_months: int = 12,
        step_months: int = 12,
    ) -> List[Dict[str, datetime]]:
        """
        Generate rolling train/test boundaries (spec §30).

        Returns boundary DATES only — this phase deliberately does not
        run models. Each window's test period strictly follows its
        train period, so evaluation is always chronological.
        """
        def add_months(moment: datetime, months: int) -> datetime:
            year = moment.year + (moment.month - 1 + months) // 12
            month = (moment.month - 1 + months) % 12 + 1
            day = min(moment.day, 28)   # clamp so month arithmetic never overflows
            return moment.replace(year=year, month=month, day=day)

        windows = []
        train_start = start
        while True:
            train_end = add_months(train_start, train_months)
            test_end = add_months(train_end, test_months)
            if train_end >= end:
                break
            windows.append({
                "train_start": train_start, "train_end": train_end,
                "test_start": train_end, "test_end": min(test_end, end),
            })
            train_start = add_months(train_start, step_months)
        return windows

    def export_csv(self, matrices: Dict[str, Any]) -> str:
        """
        Export X and Y as CSV with metadata headers (spec §43).

        Metadata comment lines carry the dataset/label identity so an
        exported file can always be traced back to how it was built.
        Contains no secrets by construction — only feature values.
        """
        output = StringIO()
        output.write(f"# dataset_version: {matrices['dataset_version']}\n")
        output.write(f"# label: {matrices['label_name']}\n")
        output.write(f"# rows: {matrices['included_count']}\n")
        output.write(f"# exported_at: {datetime.now(timezone.utc).isoformat()}\n")

        writer = csv.writer(output)
        writer.writerow(["observation_id", "cluster_id"] + matrices["feature_names"] + ["label"])
        for i, row in enumerate(matrices["X"]):
            writer.writerow([matrices["observation_ids"][i], matrices["cluster_ids"][i]] + row + [matrices["Y"][i]])
        return output.getvalue()


class ResearchRegistry:
    """
    Records every research run and result (spec §34, §35, §39, §40).

    KEEPS FAILURES AND NULL RESULTS. A registry that only retained
    interesting findings would manufacture exactly the illusion spec
    §39/§40 warns about — a project that ran two hundred variations
    and remembers only the one that worked.
    """

    def __init__(self):
        self.runs: Dict[str, ResearchRun] = {}
        self.results: List[ResearchResult] = []

    def start_run(self, cohort: Optional[CohortDefinition] = None,
                   dataset_version: Optional[DatasetVersion] = None,
                   parameters: Optional[Dict[str, Any]] = None) -> ResearchRun:
        run = ResearchRun(
            run_id=f"run-{uuid.uuid4().hex[:16]}",
            cohort_id=cohort.cohort_id if cohort else None,
            cohort_fingerprint=cohort.fingerprint() if cohort else None,
            dataset_version=dataset_version.fingerprint() if dataset_version else None,
            feature_set_version=dataset_version.feature_set_version if dataset_version else "v1",
            label_set_version=dataset_version.label_set_version if dataset_version else "v1",
            parameters=dict(parameters or {}),
            status=RunStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
        )
        self.runs[run.run_id] = run
        return run

    def complete_run(self, run_id: str, sample_size: int) -> None:
        run = self.runs.get(run_id)
        if run:
            run.status = RunStatus.COMPLETED
            run.sample_size = sample_size
            run.completed_at = datetime.now(timezone.utc)

    def fail_run(self, run_id: str, error: str) -> None:
        """Failed runs are RETAINED, not discarded — see the class docstring."""
        run = self.runs.get(run_id)
        if run:
            run.status = RunStatus.FAILED
            run.error = error
            run.completed_at = datetime.now(timezone.utc)

    def compute_result(self, run_id: str, label_name: str, values: Sequence[float],
                        cluster_ids: Optional[Sequence[Optional[str]]] = None,
                        hit_threshold: float = 0.0) -> ResearchResult:
        """
        Descriptive statistics for one run (spec §36).

        Deliberately reports NO p-value and makes no significance
        claim: event-study returns are neither independent nor normal,
        and a p-value computed as if they were would lend false
        authority to the result.
        """
        clean = [v for v in values if v is not None]
        result = ResearchResult(
            result_id=f"res-{uuid.uuid4().hex[:16]}", run_id=run_id, label_name=label_name,
            observation_count=len(clean),
            cluster_count=len({c for c in (cluster_ids or []) if c}) or None,
            created_at=datetime.now(timezone.utc),
            methodology_note=("descriptive statistics only; no significance testing — event-study "
                               "observations are not independent and returns are not normally distributed"),
        )
        result.small_sample = len(clean) < ResearchResult.MIN_MEANINGFUL_SAMPLE

        if clean:
            result.mean = round(statistics.fmean(clean), 6)
            result.median = round(statistics.median(clean), 6)
            result.min_value = round(min(clean), 6)
            result.max_value = round(max(clean), 6)
            result.hit_rate = round(sum(1 for v in clean if v > hit_threshold) / len(clean), 4)
            if len(clean) >= 2:
                result.std_dev = round(statistics.stdev(clean), 6)
                ordered = sorted(clean)
                result.p25 = round(ordered[int(0.25 * (len(ordered) - 1))], 6)
                result.p75 = round(ordered[int(0.75 * (len(ordered) - 1))], 6)

        self.results.append(result)
        return result

    def results_for(self, run_id: str) -> List[ResearchResult]:
        return [r for r in self.results if r.run_id == run_id]

    def hypothesis_count(self) -> int:
        """
        How many runs have been executed (spec §39).

        Surfaced so multiple-testing effects stay visible: the more
        hypotheses tried, the more likely the best-looking result is
        noise, and this number is the input a later correction needs.
        """
        return len(self.runs)

    def multiple_testing_note(self) -> str:
        """A plain statement of how much searching produced the current findings."""
        count = self.hypothesis_count()
        if count <= 1:
            return "1 research run executed — no multiple-testing adjustment needed yet."
        return (f"{count} research runs executed against this registry. The best-looking result among "
                 f"{count} attempts is materially more likely to be noise than a single pre-registered test; "
                 f"any finding selected from these should be validated out-of-sample.")

    def compare_cohorts(self, label_name: str,
                         cohort_a: Tuple[str, Sequence[float]],
                         cohort_b: Tuple[str, Sequence[float]]) -> Dict[str, Any]:
        """
        Side-by-side comparison of two cohorts (spec §37).

        Reports the difference in means and both sample sizes, and
        explicitly declines to call the difference significant.
        """
        name_a, values_a = cohort_a
        name_b, values_b = cohort_b
        clean_a = [v for v in values_a if v is not None]
        clean_b = [v for v in values_b if v is not None]

        def summarize(values):
            if not values:
                return {"count": 0, "mean": None, "median": None}
            return {"count": len(values),
                     "mean": round(statistics.fmean(values), 6),
                     "median": round(statistics.median(values), 6)}

        summary_a, summary_b = summarize(clean_a), summarize(clean_b)
        difference = (None if summary_a["mean"] is None or summary_b["mean"] is None
                       else round(summary_a["mean"] - summary_b["mean"], 6))
        return {
            "label": label_name,
            name_a: summary_a, name_b: summary_b,
            "mean_difference": difference,
            "note": ("descriptive comparison only; no significance test applied — "
                      "differing sample sizes and non-independent observations make a naive test misleading"),
        }
