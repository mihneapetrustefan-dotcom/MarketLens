"""
tests/research/test_research_dataset.py
--------------------------------------------
Tests for the Phase 7 research dataset.

The leakage tests (spec §49) are ADVERSARIAL: each deliberately
introduces future information and passes only if that information is
REJECTED. A suite that only tested the happy path would prove nothing
about the property that matters most here.
"""

import sys
import os
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.research_models import (
    ResearchObservation, InformationSnapshot, OutcomeSet, FeatureValue, LabelValue,
    FeatureNamespace, SampleQuality, ResearchQuality, ExclusionReason,
    CohortDefinition, DatasetVersion, ResearchRun, RunStatus, ResearchResult,
)
from src.research.temporal import (
    latest_known_at, all_known_at, strictly_after, TemporalIndex,
    assert_point_in_time_capable, TemporalJoinError,
)
from src.research.builder import CohortEngine, DatasetBuilder, ResearchRegistry

CUTOFF = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
DATASET = DatasetVersion(version="v1", created_at=CUTOFF)


def make_observation(observation_id="o1", cutoff=CUTOFF, event_type="earnings",
                     instrument_id="nvda", sector_id="technology", geography="US",
                     regime="uptrend", label_value=0.03, quality=ResearchQuality.HIGH,
                     cluster_id=None, feature_time=None, label_time=None,
                     confidence=0.9):
    snapshot = InformationSnapshot(information_cutoff=cutoff, cutoff_basis="event publication")
    snapshot.add(FeatureValue("momentum_5d", FeatureNamespace.MARKET, 0.02,
                               as_of=feature_time or cutoff - timedelta(days=1),
                               source="market_prices", calculation="close[t]/close[t-5]-1"))
    snapshot.add(FeatureValue("event_type", FeatureNamespace.EVENT, event_type,
                               is_contemporaneous_event_attribute=True))

    outcomes = OutcomeSet(information_cutoff=cutoff)
    outcomes.add(LabelValue("abnormal_return_5d", label_value,
                             measured_at=label_time or cutoff + timedelta(days=5),
                             window_name="d5"))

    sample_quality = SampleQuality(level=quality, event_fusion_confidence=confidence)
    return ResearchObservation(
        observation_id=observation_id, event_id=f"e-{observation_id}",
        instrument_id=instrument_id, event_type=event_type,
        event_time=cutoff, information_time=cutoff, observation_created_at=cutoff,
        sector_id=sector_id, geography=geography, market_regime=regime,
        information=snapshot, outcomes=outcomes, quality=sample_quality,
        event_cluster_id=cluster_id, dataset_version="v1",
    )


class TestAdversarialLeakage(unittest.TestCase):
    """Spec §49 — the tests that matter most in this phase."""

    def test_future_article_rejected_as_a_feature(self):
        """Event at 10:00, article at 14:00, used as a feature -> REJECTED."""
        snapshot = InformationSnapshot(information_cutoff=CUTOFF)
        snapshot.add(FeatureValue("future_article_sentiment", FeatureNamespace.SENTIMENT, 0.9,
                                   as_of=CUTOFF + timedelta(hours=4)))
        violations = snapshot.validate()
        self.assertEqual(len(violations), 1)
        self.assertIn("AFTER the cutoff", violations[0])

    def test_closing_price_rejected_as_an_information_feature(self):
        """The canonical example: a 10:00 event may not use that day's close as a feature."""
        snapshot = InformationSnapshot(information_cutoff=CUTOFF)
        snapshot.add(FeatureValue("session_close", FeatureNamespace.MARKET, 105.0,
                                   as_of=CUTOFF.replace(hour=20)))
        self.assertTrue(snapshot.validate())

    def test_feature_without_a_timestamp_is_rejected(self):
        """An undated feature cannot be PROVEN to predate the cutoff, so it is not trusted."""
        snapshot = InformationSnapshot(information_cutoff=CUTOFF)
        snapshot.add(FeatureValue("todays_classification", FeatureNamespace.ENTITY, "Semiconductors"))
        violations = snapshot.validate()
        self.assertIn("no as_of timestamp", violations[0])

    def test_contemporaneous_event_attribute_is_the_one_permitted_exception(self):
        snapshot = InformationSnapshot(information_cutoff=CUTOFF)
        snapshot.add(FeatureValue("event_type", FeatureNamespace.EVENT, "earnings",
                                   is_contemporaneous_event_attribute=True))
        self.assertEqual(snapshot.validate(), [])

    def test_label_at_or_before_the_cutoff_is_rejected_as_an_outcome(self):
        outcomes = OutcomeSet(information_cutoff=CUTOFF)
        outcomes.add(LabelValue("return_5d", 0.03, measured_at=CUTOFF - timedelta(hours=1)))
        violations = outcomes.validate()
        self.assertIn("is a feature, not an outcome", violations[0])

    def test_label_exactly_at_the_cutoff_is_rejected(self):
        """Strictly after, not at — a value known AT the cutoff was already available."""
        outcomes = OutcomeSet(information_cutoff=CUTOFF)
        outcomes.add(LabelValue("return_0", 0.0, measured_at=CUTOFF))
        self.assertTrue(outcomes.validate())

    def test_leaking_observation_is_excluded_from_the_matrix_not_merely_flagged(self):
        clean = make_observation("clean")
        leaking = make_observation("leaking", feature_time=CUTOFF + timedelta(hours=3))

        builder = DatasetBuilder(DATASET)
        matrices = builder.build_matrices([clean, leaking], "abnormal_return_5d")

        self.assertEqual(matrices["included_count"], 1)
        self.assertEqual(matrices["excluded_count"], 1)
        self.assertNotIn("leaking", matrices["observation_ids"])

    def test_mismatched_cutoffs_between_features_and_labels_are_caught(self):
        observation = make_observation("o1")
        observation.outcomes.information_cutoff = CUTOFF + timedelta(days=1)
        self.assertIn("cutoffs disagree", " ".join(observation.validate()))

    def test_features_and_labels_have_no_merged_accessor(self):
        """
        Structural guard: there must be no method that returns X and Y
        as one flat row, because that is how leakage happens by
        accident rather than intent.
        """
        forbidden = {"to_row", "as_dict", "to_dict", "flatten", "to_record"}
        actual = set(dir(ResearchObservation))
        self.assertEqual(forbidden & actual, set())


class TestTemporalJoins(unittest.TestCase):
    """Spec §25, §27, §28 — never the current value; always the latest value known at T."""

    def setUp(self):
        self.records = [
            {"key": "gdp", "value": 100, "as_of": CUTOFF - timedelta(days=90)},
            {"key": "gdp", "value": 105, "as_of": CUTOFF - timedelta(days=30)},
            {"key": "gdp", "value": 999, "as_of": CUTOFF + timedelta(days=30)},   # a later revision
        ]
        self.timestamp = lambda r: r["as_of"]

    def test_latest_known_at_never_returns_a_future_revision(self):
        result = latest_known_at(self.records, CUTOFF, self.timestamp)
        self.assertEqual(result["value"], 105)   # NOT 999

    def test_all_known_at_excludes_the_future_and_is_ordered(self):
        results = all_known_at(self.records, CUTOFF, self.timestamp)
        self.assertEqual([r["value"] for r in results], [100, 105])

    def test_strictly_after_returns_only_the_future(self):
        results = strictly_after(self.records, CUTOFF, self.timestamp)
        self.assertEqual([r["value"] for r in results], [999])

    def test_undated_records_are_never_joined(self):
        records = self.records + [{"key": "gdp", "value": 777, "as_of": None}]
        result = latest_known_at(records, CUTOFF, self.timestamp)
        self.assertEqual(result["value"], 105)

    def test_no_data_before_cutoff_returns_none_rather_than_the_nearest_value(self):
        early = CUTOFF - timedelta(days=365)
        self.assertIsNone(latest_known_at(self.records, early, self.timestamp))

    def test_point_in_time_capability_check_rejects_a_current_values_only_source(self):
        """
        A source holding one timestamp per entity cannot answer
        historical questions — this must fail loudly rather than
        silently returning today's value.
        """
        single_revision = [{"key": "unrate", "value": 4.1, "as_of": CUTOFF}]
        with self.assertRaises(TemporalJoinError):
            assert_point_in_time_capable(single_revision, self.timestamp, "FRED unrate")

    def test_point_in_time_capability_accepts_a_revision_history(self):
        assert_point_in_time_capable(self.records, self.timestamp, "gdp")   # must not raise

    def test_temporal_index_returns_per_key_history(self):
        index = TemporalIndex(self.timestamp, lambda r: r["key"], "macro")
        index.add_all(self.records + [{"key": "cpi", "value": 3.2, "as_of": CUTOFF - timedelta(days=10)}])
        self.assertEqual(index.latest_at("gdp", CUTOFF)["value"], 105)
        self.assertEqual(index.latest_at("cpi", CUTOFF)["value"], 3.2)
        self.assertIsNone(index.latest_at("missing", CUTOFF))

    def test_temporal_index_never_indexes_undated_records(self):
        index = TemporalIndex(self.timestamp, lambda r: r["key"])
        index.add_all([{"key": "x", "value": 1, "as_of": None}])
        self.assertEqual(index.size(), 0)


class TestCohorts(unittest.TestCase):
    """Spec §7, §8, §9 — explicit, versioned, regenerable."""

    def setUp(self):
        self.engine = CohortEngine()
        self.observations = [
            make_observation("o1", event_type="earnings", sector_id="technology", regime="uptrend"),
            make_observation("o2", event_type="earnings", sector_id="energy", regime="uptrend"),
            make_observation("o3", event_type="acquisition", sector_id="technology", regime="downtrend"),
            make_observation("o4", event_type="earnings", sector_id="technology", regime="uptrend",
                              quality=ResearchQuality.INVALID),
        ]

    def test_cohort_filters_by_event_type_and_sector(self):
        definition = self.engine.register(CohortDefinition(
            cohort_id="tech-earnings", name="Tech earnings",
            event_types=["earnings"], sector_ids=["technology"],
            min_quality=ResearchQuality.LOW, created_at=CUTOFF))
        members = self.engine.build_membership(self.observations, definition)
        self.assertEqual([m.observation_id for m in members], ["o1"])   # o4 excluded by quality

    def test_cohort_filters_by_regime(self):
        definition = CohortDefinition(cohort_id="c", name="Uptrend only",
                                       market_regimes=["uptrend"], created_at=CUTOFF)
        members = self.engine.build_membership(self.observations, definition)
        self.assertEqual({m.observation_id for m in members}, {"o1", "o2"})

    def test_cohort_filters_by_confidence(self):
        low_confidence = make_observation("low", confidence=0.5)
        definition = CohortDefinition(cohort_id="c", name="High confidence",
                                       min_event_confidence=0.85, created_at=CUTOFF)
        members = self.engine.build_membership([low_confidence] + self.observations, definition)
        self.assertNotIn("low", [m.observation_id for m in members])

    def test_cohort_filters_by_time_range(self):
        old = make_observation("old", cutoff=CUTOFF - timedelta(days=400))
        definition = CohortDefinition(cohort_id="c", name="Recent",
                                       start_time=CUTOFF - timedelta(days=30), created_at=CUTOFF)
        members = self.engine.build_membership([old] + self.observations, definition)
        self.assertNotIn("old", [m.observation_id for m in members])

    def test_membership_is_regenerable_and_deterministic(self):
        definition = CohortDefinition(cohort_id="c", name="Earnings",
                                       event_types=["earnings"], created_at=CUTOFF)
        first = self.engine.build_membership(self.observations, definition)
        second = self.engine.build_membership(self.observations, definition)
        self.assertEqual([m.observation_id for m in first], [m.observation_id for m in second])

    def test_cohort_versions_coexist(self):
        v1 = self.engine.register(CohortDefinition(cohort_id="c", name="X", version="v1",
                                                     event_types=["earnings"], created_at=CUTOFF))
        v2 = self.engine.register(CohortDefinition(cohort_id="c", name="X", version="v2",
                                                     event_types=["earnings", "acquisition"], created_at=CUTOFF))
        self.assertIsNotNone(self.engine.get("c", "v1"))
        self.assertIsNotNone(self.engine.get("c", "v2"))
        self.assertNotEqual(self.engine.get("c", "v1").fingerprint(),
                             self.engine.get("c", "v2").fingerprint())

    def test_changing_a_criterion_changes_the_fingerprint(self):
        a = CohortDefinition(cohort_id="c", name="X", event_types=["earnings"], created_at=CUTOFF)
        b = CohortDefinition(cohort_id="c", name="X", event_types=["earnings", "guidance"], created_at=CUTOFF)
        self.assertNotEqual(a.fingerprint(), b.fingerprint())


class TestDatasetBuilder(unittest.TestCase):
    def setUp(self):
        self.builder = DatasetBuilder(DATASET)

    def test_x_and_y_are_returned_separately_and_aligned(self):
        observations = [make_observation(f"o{i}", label_value=0.01 * i) for i in range(5)]
        matrices = self.builder.build_matrices(observations, "abnormal_return_5d")
        self.assertEqual(len(matrices["X"]), len(matrices["Y"]))
        self.assertEqual(matrices["included_count"], 5)

    def test_missing_label_excludes_the_observation(self):
        observation = make_observation("no-label")
        observation.outcomes.labels.clear()
        matrices = self.builder.build_matrices([observation], "abnormal_return_5d")
        self.assertEqual(matrices["included_count"], 0)
        self.assertIn("missing", matrices["excluded"][0]["reasons"][0])

    def test_invalid_quality_observation_is_excluded(self):
        observation = make_observation("bad", quality=ResearchQuality.INVALID)
        matrices = self.builder.build_matrices([observation], "abnormal_return_5d")
        self.assertEqual(matrices["included_count"], 0)

    def test_feature_columns_are_consistently_aligned(self):
        rich = make_observation("rich")
        rich.information.add(FeatureValue("extra", FeatureNamespace.MACRO, 1.5,
                                           as_of=CUTOFF - timedelta(days=1)))
        plain = make_observation("plain")
        matrices = self.builder.build_matrices([rich, plain], "abnormal_return_5d")

        extra_index = matrices["feature_names"].index("macro.extra")
        rich_row = matrices["X"][matrices["observation_ids"].index("rich")]
        plain_row = matrices["X"][matrices["observation_ids"].index("plain")]
        self.assertEqual(rich_row[extra_index], 1.5)
        self.assertIsNone(plain_row[extra_index])   # absent, not shifted

    def test_cluster_ids_are_carried_through(self):
        observations = [make_observation("o1", cluster_id="cl-1"), make_observation("o2", cluster_id="cl-1")]
        matrices = self.builder.build_matrices(observations, "abnormal_return_5d")
        self.assertEqual(matrices["cluster_ids"], ["cl-1", "cl-1"])

    def test_csv_export_includes_metadata_and_rows(self):
        matrices = self.builder.build_matrices([make_observation("o1")], "abnormal_return_5d")
        csv_text = self.builder.export_csv(matrices)
        self.assertIn("# dataset_version:", csv_text)
        self.assertIn("# label: abnormal_return_5d", csv_text)
        self.assertIn("o1", csv_text)


class TestTemporalSplitting(unittest.TestCase):
    """Spec §29, §30 — chronological only; no shuffle option exists."""

    def setUp(self):
        self.builder = DatasetBuilder(DATASET)
        self.observations = [
            make_observation(f"o{i}", cutoff=datetime(2020 + i, 6, 1, tzinfo=timezone.utc))
            for i in range(6)
        ]

    def test_split_is_chronological(self):
        split = self.builder.chronological_split(
            self.observations,
            train_end=datetime(2022, 12, 31, tzinfo=timezone.utc),
            validation_end=datetime(2023, 12, 31, tzinfo=timezone.utc))
        train_times = [o.information_time for o in split["train"]]
        test_times = [o.information_time for o in split["test"]]
        self.assertTrue(all(t <= datetime(2022, 12, 31, tzinfo=timezone.utc) for t in train_times))
        self.assertTrue(all(t > datetime(2023, 12, 31, tzinfo=timezone.utc) for t in test_times))

    def test_no_shuffle_option_exists(self):
        """A random split would put future observations into training — the option must not exist."""
        import inspect
        signature = inspect.signature(self.builder.chronological_split)
        for forbidden in ("shuffle", "random", "random_state", "seed"):
            self.assertNotIn(forbidden, signature.parameters)

    def test_walk_forward_windows_are_chronological_and_non_overlapping(self):
        windows = self.builder.walk_forward_windows(
            datetime(2018, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertGreater(len(windows), 0)
        for window in windows:
            self.assertLess(window["train_end"], window["test_end"])
            self.assertEqual(window["test_start"], window["train_end"])

    def test_undated_observations_are_not_placed_in_any_split(self):
        undated = make_observation("undated")
        undated.information_time = None
        undated.event_time = None
        split = self.builder.chronological_split([undated], train_end=CUTOFF)
        self.assertEqual(sum(len(v) for v in split.values()), 0)


class TestResearchRunsAndReproducibility(unittest.TestCase):
    """Spec §34-§36, §39, §40."""

    def setUp(self):
        self.registry = ResearchRegistry()
        self.cohort = CohortDefinition(cohort_id="c", name="Tech earnings",
                                        event_types=["earnings"], created_at=CUTOFF)

    def test_run_records_everything_needed_to_reproduce_it(self):
        run = self.registry.start_run(self.cohort, DATASET, {"window": "d5"})
        self.assertIsNotNone(run.cohort_fingerprint)
        self.assertIsNotNone(run.dataset_version)
        self.assertIn("window=d5", run.reproducibility_key())

    def test_identical_runs_share_a_reproducibility_key(self):
        a = self.registry.start_run(self.cohort, DATASET, {"window": "d5"})
        b = self.registry.start_run(self.cohort, DATASET, {"window": "d5"})
        self.assertEqual(a.reproducibility_key(), b.reproducibility_key())

    def test_different_parameters_produce_different_keys(self):
        a = self.registry.start_run(self.cohort, DATASET, {"window": "d5"})
        b = self.registry.start_run(self.cohort, DATASET, {"window": "d20"})
        self.assertNotEqual(a.reproducibility_key(), b.reproducibility_key())

    def test_failed_runs_are_retained_not_discarded(self):
        run = self.registry.start_run(self.cohort, DATASET)
        self.registry.fail_run(run.run_id, "insufficient data")
        self.assertEqual(self.registry.runs[run.run_id].status, RunStatus.FAILED)
        self.assertEqual(self.registry.hypothesis_count(), 1)

    def test_hypothesis_count_tracks_every_run(self):
        for _ in range(5):
            self.registry.start_run(self.cohort, DATASET)
        self.assertEqual(self.registry.hypothesis_count(), 5)
        self.assertIn("5 research runs", self.registry.multiple_testing_note())

    def test_results_report_small_samples_honestly(self):
        run = self.registry.start_run(self.cohort, DATASET)
        result = self.registry.compute_result(run.run_id, "abnormal_return_5d", [0.01, 0.02, -0.01])
        self.assertTrue(result.small_sample)
        self.assertEqual(result.observation_count, 3)

    def test_results_never_claim_significance(self):
        run = self.registry.start_run(self.cohort, DATASET)
        result = self.registry.compute_result(run.run_id, "x", [0.01] * 50)
        self.assertFalse(result.small_sample)
        self.assertIn("no significance testing", result.methodology_note)
        for forbidden in ("p_value", "pvalue", "significant", "is_significant"):
            self.assertNotIn(forbidden, ResearchResult.__dataclass_fields__)

    def test_clustered_observations_trigger_an_effective_sample_warning(self):
        run = self.registry.start_run(self.cohort, DATASET)
        clusters = ["cl-1"] * 40 + ["cl-2"] * 40
        result = self.registry.compute_result(run.run_id, "x", [0.01] * 80, cluster_ids=clusters)
        self.assertEqual(result.cluster_count, 2)
        self.assertIn("effective sample size", result.effective_sample_warning)

    def test_previous_results_are_never_overwritten(self):
        run = self.registry.start_run(self.cohort, DATASET)
        self.registry.compute_result(run.run_id, "label_a", [0.01, 0.02])
        self.registry.compute_result(run.run_id, "label_b", [0.03, 0.04])
        self.assertEqual(len(self.registry.results_for(run.run_id)), 2)

    def test_cohort_comparison_reports_both_sides_without_a_verdict(self):
        comparison = self.registry.compare_cohorts(
            "abnormal_return_5d", ("beats", [0.03, 0.04, 0.02]), ("misses", [-0.02, -0.01, -0.03]))
        self.assertEqual(comparison["beats"]["count"], 3)
        self.assertIsNotNone(comparison["mean_difference"])
        self.assertIn("no significance test", comparison["note"])


class TestVersioning(unittest.TestCase):
    """Spec §21, §22, §23."""

    def test_dataset_version_fingerprint_covers_every_component(self):
        a = DatasetVersion(version="v1", feature_set_version="v1", created_at=CUTOFF)
        b = DatasetVersion(version="v1", feature_set_version="v2", created_at=CUTOFF)
        self.assertNotEqual(a.fingerprint(), b.fingerprint())

    def test_dataset_version_is_immutable(self):
        version = DatasetVersion(version="v1", created_at=CUTOFF)
        with self.assertRaises(Exception):
            version.version = "v2"

    def test_feature_carries_its_own_version_and_provenance(self):
        feature = FeatureValue("momentum_5d", FeatureNamespace.MARKET, 0.02, as_of=CUTOFF,
                                source="market_prices", calculation="close[t]/close[t-5]-1",
                                feature_version="v1")
        self.assertEqual(feature.feature_version, "v1")
        self.assertEqual(feature.source, "market_prices")
        self.assertTrue(feature.calculation)

    def test_feature_namespaces_prevent_name_collisions(self):
        snapshot = InformationSnapshot(information_cutoff=CUTOFF)
        snapshot.add(FeatureValue("volatility", FeatureNamespace.MARKET, 0.2, as_of=CUTOFF))
        snapshot.add(FeatureValue("volatility", FeatureNamespace.SECTOR, 0.3, as_of=CUTOFF))
        self.assertEqual(len(snapshot.features), 2)
        self.assertEqual(snapshot.get("market.volatility").value, 0.2)
        self.assertEqual(snapshot.get("sector.volatility").value, 0.3)


class TestInvalidObservations(unittest.TestCase):
    """Spec §20 — marked and kept, never silently deleted."""

    def test_exclusion_records_a_reason(self):
        quality = SampleQuality()
        quality.exclude(ExclusionReason.INSUFFICIENT_PRICE_DATA, "only 3 candles available")
        self.assertEqual(quality.level, ResearchQuality.INVALID)
        self.assertIn(ExclusionReason.INSUFFICIENT_PRICE_DATA, quality.exclusions)
        self.assertFalse(quality.is_usable)

    def test_excluded_observations_still_appear_in_the_build_report(self):
        builder = DatasetBuilder(DATASET)
        bad = make_observation("bad", quality=ResearchQuality.INVALID)
        matrices = builder.build_matrices([bad], "abnormal_return_5d")
        self.assertEqual(len(matrices["excluded"]), 1)
        self.assertEqual(matrices["excluded"][0]["observation_id"], "bad")

    def test_downgrade_never_upgrades_quality(self):
        quality = SampleQuality(level=ResearchQuality.LOW)
        quality.downgrade(ResearchQuality.HIGH)
        self.assertEqual(quality.level, ResearchQuality.LOW)


if __name__ == "__main__":
    unittest.main(verbosity=2)
