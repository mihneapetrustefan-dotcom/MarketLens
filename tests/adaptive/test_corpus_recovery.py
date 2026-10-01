"""
tests/adaptive/test_corpus_recovery.py
--------------------------------------------------
Phase 25.9I-F.7: historical coverage, lineage and recovery discipline.

The test that matters most is the third one: a historical article must
not inherit the recovery instant as its information time. Everything
else in this phase is bookkeeping; that one is the difference between a
recovered dataset and a contaminated one.
"""

import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.adaptive import corpus_funnel as funnel
from src.adaptive.corpus_versioning import (
    initialize_recovery_schema, load_recovery_run, record_corpus_version,
    record_cross_version_mapping, record_recovery_run,
)
from src.domain.corpus_recovery_models import (
    ArtifactState, CorpusVersion, CrossVersionMapping, CrossVersionRelation,
    FrozenArtifactError, FunnelStage, HistoricalFunnel, InformationTiming,
    LossAccounting, LossReason, PipelineStage, RecoveryRun, RecoveryStatus,
)

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
SOURCE_DB = os.path.join(ROOT, "data", "marketlens.db")
V2_DB = os.path.join(ROOT, "data", "marketlens_corpus_v2.db")
UTC = timezone.utc

JULY = datetime(2026, 7, 2, 7, 0, tzinfo=UTC)
RECOVERED = datetime(2026, 10, 1, 17, 0, tzinfo=UTC)


def a_run(**kw) -> RecoveryRun:
    base = dict(recovery_run_id="rec-test", pipeline_version="f7.test",
                code_commit="abc123", range_start=datetime(2026, 7, 1, tzinfo=UTC),
                range_end=datetime(2026, 8, 30, tzinfo=UTC),
                started_at=RECOVERED, status=RecoveryStatus.RUNNING)
    base.update(kw)
    return RecoveryRun(**base)


# ======================================================================
# 1. Earliest timestamps are measured from real records
# ======================================================================

@unittest.skipUnless(os.path.exists(SOURCE_DB), "source corpus absent")
class TestMeasuredNotAssumed(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.conn = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_the_entity_cutoff_is_a_real_articles_publication_instant(self):
        cutoff = funnel.entity_cutoff(self.conn)
        self.assertIsNotNone(cutoff)
        match = self.conn.execute("""
            SELECT COUNT(*) FROM articles a
            JOIN article_entities e ON e.article_id = a.article_id
            WHERE a.published_at = ?""", (cutoff.isoformat(),)).fetchone()[0]
        self.assertGreater(match, 0, "the cutoff is not a real record")

    def test_nothing_entity_linked_predates_the_measured_cutoff(self):
        cutoff = funnel.entity_cutoff(self.conn)
        earlier = self.conn.execute("""
            SELECT COUNT(*) FROM articles a
            JOIN article_entities e ON e.article_id = a.article_id
            WHERE a.published_at < ?""", (cutoff.isoformat(),)).fetchone()[0]
        self.assertEqual(earlier, 0)

    def test_articles_exist_well_before_the_cutoff(self):
        """The gap is the finding: source material predates the cutoff."""
        cutoff = funnel.entity_cutoff(self.conn)
        before = self.conn.execute(
            "SELECT COUNT(*) FROM articles WHERE published_at < ?",
            (cutoff.isoformat(),)).fetchone()[0]
        self.assertGreater(before, 10000)

    def test_no_date_constant_is_hard_coded_in_the_funnel_module(self):
        path = os.path.join(ROOT, "src", "adaptive", "corpus_funnel.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("2026-07-30", source)
        self.assertNotIn("2026-08-15", source)


# ======================================================================
# 2. Funnel counts reconcile
# ======================================================================

class TestFunnelReconciliation(unittest.TestCase):

    def test_a_funnel_whose_stages_do_not_chain_does_not_reconcile(self):
        broken = HistoricalFunnel(stages=(
            FunnelStage(stage=PipelineStage.RAW_ARTICLE,
                        count_entering=100, count_exiting=100),
            FunnelStage(stage=PipelineStage.NORMALIZED_ARTICLE,
                        count_entering=90, count_exiting=90)))
        self.assertFalse(broken.reconciles)

    def test_a_chained_funnel_reconciles(self):
        good = HistoricalFunnel(stages=(
            FunnelStage(stage=PipelineStage.RAW_ARTICLE,
                        count_entering=100, count_exiting=100),
            FunnelStage(stage=PipelineStage.NORMALIZED_ARTICLE,
                        count_entering=100, count_exiting=80)))
        self.assertTrue(good.reconciles)

    def test_stages_out_of_pipeline_order_are_refused(self):
        with self.assertRaises(ValueError):
            HistoricalFunnel(stages=(
                FunnelStage(stage=PipelineStage.CANONICAL_EVENT,
                            count_entering=1, count_exiting=1),
                FunnelStage(stage=PipelineStage.RAW_ARTICLE,
                            count_entering=1, count_exiting=1)))

    def test_the_first_material_break_is_the_first_one_not_the_biggest(self):
        shaped = HistoricalFunnel(stages=(
            FunnelStage(stage=PipelineStage.RAW_ARTICLE,
                        count_entering=1000, count_exiting=1000),
            FunnelStage(stage=PipelineStage.NORMALIZED_ARTICLE,
                        count_entering=1000, count_exiting=400),
            FunnelStage(stage=PipelineStage.ENTITY_RESOLVED,
                        count_entering=400, count_exiting=10)))
        self.assertIs(shaped.first_material_break.stage,
                      PipelineStage.NORMALIZED_ARTICLE)

    def test_total_loss_at_a_stage_is_a_break_whatever_the_threshold(self):
        wiped = HistoricalFunnel(
            stages=(FunnelStage(stage=PipelineStage.RAW_ARTICLE,
                                count_entering=10, count_exiting=10),
                    FunnelStage(stage=PipelineStage.NORMALIZED_ARTICLE,
                                count_entering=10, count_exiting=0)),
            material_drop_pct=99.0)
        self.assertIsNotNone(wiped.first_material_break)

    @unittest.skipUnless(os.path.exists(SOURCE_DB), "source corpus absent")
    def test_the_real_funnel_reconciles_and_breaks_at_entity_resolution(self):
        conn = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
        try:
            measured = funnel.measure_funnel(conn)
        finally:
            conn.close()
        self.assertTrue(measured.reconciles)
        self.assertIs(measured.first_material_break.stage,
                      PipelineStage.ENTITY_RESOLVED)

    @unittest.skipUnless(os.path.exists(SOURCE_DB), "source corpus absent")
    def test_loss_accounting_adds_up_to_the_article_count(self):
        conn = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
        try:
            losses = funnel.classify_losses(conn)
        finally:
            conn.close()
        self.assertTrue(losses.reconciles, losses.describe())
        self.assertEqual(losses.unclassified, 0)

    def test_an_accounting_that_does_not_add_up_says_so(self):
        partial = LossAccounting(
            period_label="x", total_source_articles=100,
            by_reason={LossReason.NO_EVENT_FOUND: 40})
        self.assertFalse(partial.reconciles)
        self.assertEqual(partial.unclassified, 60)


# ======================================================================
# 3. A historical article does not inherit recovery processing time
# ======================================================================

class TestInformationTimeIsNotProcessingTime(unittest.TestCase):
    """The contract the whole phase rests on."""

    def test_a_july_article_recovered_in_october_keeps_july(self):
        timing = InformationTiming(information_time=JULY,
                                   processing_time=datetime(2026, 8, 23, tzinfo=UTC),
                                   recovered_at=RECOVERED,
                                   recovery_run_id="rec-1")
        self.assertEqual(timing.information_time, JULY)
        self.assertEqual(timing.recovery_lag_days, 91)
        self.assertTrue(timing.is_recovered)

    def test_an_information_time_at_the_recovery_instant_is_refused(self):
        with self.assertRaises(ValueError):
            InformationTiming(information_time=RECOVERED, recovered_at=RECOVERED)

    def test_an_information_time_after_the_recovery_instant_is_refused(self):
        with self.assertRaises(ValueError):
            InformationTiming(information_time=RECOVERED + timedelta(days=1),
                              recovered_at=RECOVERED)

    def test_being_processed_before_publication_is_refused(self):
        with self.assertRaises(ValueError):
            InformationTiming(information_time=datetime(2026, 8, 1, tzinfo=UTC),
                              processing_time=datetime(2026, 7, 1, tzinfo=UTC))

    def test_a_naive_timestamp_is_refused(self):
        with self.assertRaises(ValueError):
            InformationTiming(information_time=datetime(2026, 7, 2, 7, 0))

    def test_an_original_record_needs_no_recovery_run(self):
        timing = InformationTiming(information_time=JULY)
        self.assertFalse(timing.is_recovered)
        self.assertIsNone(timing.recovery_lag_days)


# ======================================================================
# 4-7. Replay, and failures that stay explicit
# ======================================================================

@unittest.skipUnless(os.path.exists(SOURCE_DB), "source corpus absent")
class TestOldSourceCanBeReplayed(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.conn = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_the_skipped_region_carries_resolvable_mention_data(self):
        """
        Replay is only possible because the source rows were never the
        problem. If the mention columns were empty, no amount of
        re-running would recover anything.
        """
        detail = funnel.recoverable_detail(self.conn)
        self.assertGreater(detail["skipped_with_mentions"], 10000)
        self.assertGreater(detail["recoverable_share"], 0.5)

    def test_both_sides_of_the_cutoff_have_comparable_mention_density(self):
        """Refutes 'the old articles were different'."""
        audit = funnel.entity_resolution_audit(self.conn)
        self.assertTrue(audit["mention_density_comparable"], audit)

    def test_entity_coverage_differs_enormously_across_the_cutoff(self):
        audit = funnel.entity_resolution_audit(self.conn)
        self.assertEqual(audit["pre_cutoff"]["entity_pct"], 0.0)
        self.assertGreater(audit["post_cutoff"]["entity_pct"], 50.0)

    def test_entity_failure_is_explicit_not_silent(self):
        losses = funnel.classify_losses(self.conn)
        self.assertIn(LossReason.ENTITY_NOT_ATTEMPTED, losses.by_reason)
        self.assertGreater(losses.by_reason[LossReason.ENTITY_NOT_ATTEMPTED], 0)

    def test_not_attempted_is_recoverable_and_unresolved_is_not(self):
        self.assertTrue(LossReason.ENTITY_NOT_ATTEMPTED.is_recoverable)
        self.assertFalse(LossReason.ENTITY_UNRESOLVED.is_recoverable)
        self.assertFalse(LossReason.ENTITY_AMBIGUOUS.is_recoverable)

    def test_extraction_failure_is_explicit(self):
        audit = funnel.extraction_audit(self.conn)
        self.assertIn("skipped_articles", audit)
        self.assertIn("no_event_articles", audit)
        self.assertNotEqual(audit["eligible_articles"], 0)

    def test_extraction_distinguishes_never_run_from_found_nothing(self):
        audit = funnel.extraction_audit(self.conn)
        self.assertGreater(audit["no_event_articles"], 0,
                           "ran and found nothing")
        self.assertIsNotNone(audit["extraction_cutoff"])

    def test_fusion_failure_is_explicit(self):
        audit = funnel.fusion_audit(self.conn)
        self.assertEqual(audit["reports_not_fused"], 0,
                         "fusion was not the break")
        self.assertGreater(audit["reports_fused"], 0)

    def test_price_history_can_support_the_recovery_period(self):
        """
        Recovering events whose returns cannot be measured would add rows
        and no evidence.
        """
        prices = funnel.price_coverage_audit(self.conn)
        earliest = prices["earliest"]
        self.assertIsNotNone(earliest)
        self.assertLess(earliest[:7], "2026-07")

    def test_the_time_distribution_shows_a_step_not_a_ramp(self):
        rows = funnel.coverage_by_period(self.conn, grain="day")
        step = funnel.first_hard_cutoff(rows, field="entity_resolved")
        self.assertIsNotNone(step, "no hard cutoff found")
        zero_days = [r for r in rows if r["period"] < step
                     and r["articles"] > 100 and r["entity_resolved"] == 0]
        self.assertGreater(len(zero_days), 20,
                           "a ramp would not leave 20+ busy days at exactly zero")


# ======================================================================
# 8-9. Duplicate safety and idempotency
# ======================================================================

@unittest.skipUnless(os.path.exists(V2_DB), "recovered corpus absent")
class TestRecoveredCorpusIntegrity(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.conn = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_one_article_does_not_produce_two_canonical_events_of_a_type(self):
        """
        Report identity is (article_id, event_type), so the same article
        replayed cannot mint a second report for the same claim.
        """
        duplicates = self.conn.execute("""
            SELECT COUNT(*) FROM (
              SELECT v.article_id, e.event_type, COUNT(DISTINCT e.event_id) n
              FROM event_evidence v JOIN events e ON e.event_id = v.event_id
              GROUP BY 1, 2 HAVING n > 1)""").fetchone()[0]
        self.assertEqual(duplicates, 0)

    def test_every_canonical_event_has_at_least_one_report(self):
        orphans = self.conn.execute("""
            SELECT COUNT(*) FROM canonical_events c
            WHERE NOT EXISTS (SELECT 1 FROM canonical_event_reports r
                              WHERE r.canonical_event_id = c.canonical_event_id)
        """).fetchone()[0]
        self.assertEqual(orphans, 0)

    def test_every_report_traces_back_to_a_source_article(self):
        anonymous = self.conn.execute("""
            SELECT COUNT(*) FROM events e
            WHERE NOT EXISTS (SELECT 1 FROM event_evidence v
                              WHERE v.event_id = e.event_id)
        """).fetchone()[0]
        self.assertEqual(anonymous, 0)

    def test_every_observation_traces_back_to_a_canonical_event(self):
        orphans = self.conn.execute("""
            SELECT COUNT(*) FROM research_observations o
            WHERE NOT EXISTS (SELECT 1 FROM canonical_events c
                              WHERE c.canonical_event_id = o.event_id)
        """).fetchone()[0]
        self.assertEqual(orphans, 0)

    def test_event_evidence_articles_all_exist(self):
        dangling = self.conn.execute("""
            SELECT COUNT(*) FROM event_evidence v
            WHERE NOT EXISTS (SELECT 1 FROM articles a
                              WHERE a.article_id = v.article_id)
        """).fetchone()[0]
        self.assertEqual(dangling, 0)

    def test_no_recovered_record_carries_the_recovery_instant_as_its_own_time(self):
        rows = self.conn.execute("""
            SELECT COUNT(*) FROM recovery_provenance
            WHERE information_time >= recovered_at""").fetchone()[0]
        self.assertEqual(rows, 0)

    def test_provenance_distinguishes_recovered_records(self):
        """Recovered history must not be indistinguishable from original."""
        kinds = dict(self.conn.execute(
            "SELECT record_type, COUNT(*) FROM recovery_provenance GROUP BY 1"))
        self.assertIn("canonical_event", kinds)
        self.assertIn("research_observation", kinds)
        self.assertGreater(kinds["canonical_event"], 0)

    def test_every_provenance_row_names_a_recovery_run(self):
        unnamed = self.conn.execute(
            "SELECT COUNT(*) FROM recovery_provenance "
            "WHERE recovery_run_id IS NULL OR recovery_run_id = ''").fetchone()[0]
        self.assertEqual(unnamed, 0)

    def test_the_recovered_corpus_starts_earlier_than_the_old_one(self):
        new_start = funnel.canonical_cutoff(self.conn)
        old = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
        try:
            old_start = funnel.canonical_cutoff(old)
        finally:
            old.close()
        self.assertIsNotNone(new_start)
        self.assertLess(new_start, old_start)

    def test_the_recovered_funnel_still_reconciles(self):
        self.assertTrue(funnel.measure_funnel(self.conn).reconciles)

    def test_entity_resolution_no_longer_has_a_publication_floor(self):
        audit = funnel.entity_resolution_audit(self.conn)
        earliest_linked = funnel.entity_cutoff(self.conn)
        earliest_article = self.conn.execute(
            "SELECT MIN(published_at) FROM articles").fetchone()[0]
        self.assertLess((earliest_linked - datetime.fromisoformat(
            earliest_article)).days, 3,
            "entity links should now begin with the corpus, not a month later")


# ======================================================================
# 10-11. Partial recovery cannot report COMPLETE
# ======================================================================

class TestRecoveryRunStatus(unittest.TestCase):

    def test_a_run_that_stopped_early_cannot_be_marked_complete(self):
        run = a_run()
        with self.assertRaises(ValueError):
            run.mark_complete(completed_at=RECOVERED,
                              processed_through=datetime(2026, 8, 1, tzinfo=UTC))

    def test_a_run_covering_its_range_can_be_marked_complete(self):
        run = a_run().mark_complete(
            completed_at=RECOVERED,
            processed_through=datetime(2026, 8, 30, tzinfo=UTC),
            counts={"canonical_events": 1000})
        self.assertIs(run.status, RecoveryStatus.COMPLETE)
        self.assertTrue(run.covers_full_range)
        self.assertTrue(run.status.is_trustworthy)

    def test_a_run_with_errors_cannot_be_marked_complete(self):
        run = a_run(errors=("stage fusion exploded",))
        with self.assertRaises(ValueError):
            run.mark_complete(completed_at=RECOVERED,
                              processed_through=datetime(2026, 8, 30, tzinfo=UTC))

    def test_a_partial_run_must_say_why(self):
        with self.assertRaises(ValueError):
            a_run().mark_partial(completed_at=RECOVERED,
                                 processed_through=None, reason="")

    def test_a_partial_run_is_not_trustworthy_for_research(self):
        run = a_run().mark_partial(completed_at=RECOVERED,
                                   processed_through=datetime(2026, 8, 1, tzinfo=UTC),
                                   reason="validation slice")
        self.assertIs(run.status, RecoveryStatus.PARTIAL)
        self.assertFalse(run.status.is_trustworthy)
        self.assertFalse(run.covers_full_range)

    def test_a_failed_run_must_record_an_error(self):
        with self.assertRaises(ValueError):
            a_run().mark_failed(completed_at=RECOVERED, errors=())

    def test_a_run_without_a_code_commit_is_refused(self):
        with self.assertRaises(ValueError):
            a_run(code_commit="")

    def test_a_complete_status_without_a_completion_time_is_refused(self):
        with self.assertRaises(ValueError):
            a_run(status=RecoveryStatus.COMPLETE, completed_at=None)

    def test_recovery_run_metadata_survives_a_round_trip(self):
        conn = sqlite3.connect(":memory:")
        try:
            initialize_recovery_schema(conn)
            run = a_run().mark_complete(
                completed_at=RECOVERED,
                processed_through=datetime(2026, 8, 30, tzinfo=UTC),
                counts={"canonical_events": 7})
            record_recovery_run(conn, run)
            again = load_recovery_run(conn, "rec-test")
            self.assertEqual(again.describe(), run.describe())
        finally:
            conn.close()


# ======================================================================
# 12-16. Immutability, and old identity
# ======================================================================

class TestVersionImmutability(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        initialize_recovery_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def _corpus(self, version_id="canonical_corpus_v2",
                state=ArtifactState.BUILDING, **kw) -> CorpusVersion:
        base = dict(version_id=version_id, created_at=RECOVERED,
                    code_commit="abc", schema_version="f7.v1", state=state)
        base.update(kw)
        return CorpusVersion(**base)

    def test_a_frozen_corpus_cannot_be_declared_without_a_hash(self):
        with self.assertRaises(FrozenArtifactError):
            self._corpus(state=ArtifactState.FROZEN, frozen_at=RECOVERED)

    def test_a_frozen_corpus_cannot_be_declared_without_a_freeze_time(self):
        with self.assertRaises(FrozenArtifactError):
            self._corpus(state=ArtifactState.FROZEN, content_hash="deadbeef")

    def test_building_cannot_jump_straight_to_frozen(self):
        with self.assertRaises(FrozenArtifactError):
            self._corpus().transition_to(ArtifactState.FROZEN, at=RECOVERED,
                                         content_hash="deadbeef")

    def test_a_frozen_corpus_cannot_go_back_to_building(self):
        frozen = (self._corpus().transition_to(ArtifactState.VALIDATED, at=RECOVERED)
                  .transition_to(ArtifactState.FROZEN, at=RECOVERED,
                                 content_hash="deadbeef"))
        with self.assertRaises(FrozenArtifactError):
            frozen.transition_to(ArtifactState.BUILDING, at=RECOVERED)

    def test_a_frozen_corpus_record_cannot_be_rewritten(self):
        frozen = (self._corpus().transition_to(ArtifactState.VALIDATED, at=RECOVERED)
                  .transition_to(ArtifactState.FROZEN, at=RECOVERED,
                                 content_hash="aaaa"))
        record_corpus_version(self.conn, frozen)
        from dataclasses import replace
        tampered = replace(frozen, content_hash="bbbb")
        with self.assertRaises(FrozenArtifactError):
            record_corpus_version(self.conn, tampered)

    def test_rewriting_a_frozen_record_with_identical_content_is_a_no_op(self):
        frozen = (self._corpus().transition_to(ArtifactState.VALIDATED, at=RECOVERED)
                  .transition_to(ArtifactState.FROZEN, at=RECOVERED,
                                 content_hash="aaaa"))
        record_corpus_version(self.conn, frozen)
        record_corpus_version(self.conn, frozen)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM corpus_versions").fetchone()[0], 1)

    def test_only_a_frozen_corpus_may_become_research_input(self):
        validated = self._corpus().transition_to(ArtifactState.VALIDATED,
                                                 at=RECOVERED)
        with self.assertRaises(FrozenArtifactError):
            validated.transition_to(ArtifactState.ELIGIBLE_FOR_RESEARCH,
                                    at=RECOVERED)

    def test_files_existing_is_not_eligibility(self):
        building = self._corpus()
        self.assertFalse(building.state.is_research_input)
        self.assertFalse(building.state.is_immutable)


# ======================================================================
# 17. Cross-version lineage
# ======================================================================

class TestCrossVersionLineage(unittest.TestCase):

    def test_a_merge_needs_two_old_events(self):
        with self.assertRaises(ValueError):
            CrossVersionMapping(relation=CrossVersionRelation.MERGED,
                                old_corpus_version="v1", new_corpus_version="v2",
                                old_event_ids=("a",), new_event_ids=("b",))

    def test_a_split_needs_two_new_events(self):
        with self.assertRaises(ValueError):
            CrossVersionMapping(relation=CrossVersionRelation.SPLIT,
                                old_corpus_version="v1", new_corpus_version="v2",
                                old_event_ids=("a",), new_event_ids=("b",))

    def test_a_new_event_has_no_old_id_and_that_is_allowed(self):
        mapping = CrossVersionMapping(relation=CrossVersionRelation.NEW,
                                      old_corpus_version="v1",
                                      new_corpus_version="v2",
                                      new_event_ids=("b",))
        self.assertFalse(mapping.relation.exists_in_old)
        self.assertTrue(mapping.relation.exists_in_new)

    def test_a_removed_event_must_still_name_what_was_removed(self):
        with self.assertRaises(ValueError):
            CrossVersionMapping(relation=CrossVersionRelation.REMOVED,
                                old_corpus_version="v1", new_corpus_version="v2")

    def test_mapping_two_identical_versions_is_refused(self):
        with self.assertRaises(ValueError):
            CrossVersionMapping(relation=CrossVersionRelation.SAME,
                                old_corpus_version="v1", new_corpus_version="v1",
                                old_event_ids=("a",), new_event_ids=("a",))

    def test_a_mapping_round_trips_through_the_registry(self):
        conn = sqlite3.connect(":memory:")
        try:
            initialize_recovery_schema(conn)
            record_cross_version_mapping(conn, CrossVersionMapping(
                relation=CrossVersionRelation.NEW, old_corpus_version="v1",
                new_corpus_version="v2", new_event_ids=("ce-x",)))
            row = conn.execute(
                "SELECT relation, new_event_ids_json FROM cross_version_event_map"
            ).fetchone()
            self.assertEqual(row[0], "new")
            self.assertIn("ce-x", row[1])
        finally:
            conn.close()

    @unittest.skipUnless(os.path.exists(V2_DB), "recovered corpus absent")
    def test_the_real_recovery_recorded_cross_version_lineage(self):
        conn = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)
        try:
            tally = dict(conn.execute(
                "SELECT relation, COUNT(*) FROM cross_version_event_map "
                "GROUP BY 1").fetchall())
        finally:
            conn.close()
        self.assertGreater(sum(tally.values()), 0)
        self.assertGreater(tally.get("new", 0), 0,
                           "recovery that added history must record NEW events")


# ======================================================================
# 18-20. As-of preservation, no future leakage
# ======================================================================

@unittest.skipUnless(os.path.exists(V2_DB), "recovered corpus absent")
class TestAsOfIntegrity(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.conn = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_no_feature_is_known_after_its_observations_cutoff(self):
        leaked = self.conn.execute("""
            SELECT COUNT(*) FROM research_features f
            JOIN research_observations o ON o.observation_id = f.observation_id
            WHERE f.as_of IS NOT NULL AND f.as_of > o.information_cutoff
        """).fetchone()[0]
        self.assertEqual(leaked, 0)

    def test_every_label_matures_strictly_after_its_cutoff(self):
        early = self.conn.execute("""
            SELECT COUNT(*) FROM research_labels l
            JOIN research_observations o ON o.observation_id = l.observation_id
            WHERE l.measured_at IS NOT NULL
              AND l.measured_at <= o.information_cutoff
        """).fetchone()[0]
        self.assertEqual(early, 0)

    def test_a_future_article_cannot_be_evidence_for_an_earlier_event(self):
        """
        Corroboration must not grow backwards: an article published after
        an event's first report may extend it, but no evidence row may
        predate the article it comes from.
        """
        impossible = self.conn.execute("""
            SELECT COUNT(*) FROM event_evidence v
            JOIN articles a ON a.article_id = v.article_id
            WHERE v.published_at IS NOT NULL AND a.published_at IS NOT NULL
              AND v.published_at != a.published_at
        """).fetchone()[0]
        self.assertEqual(impossible, 0)

    def test_an_events_publication_time_matches_its_source_article(self):
        mismatched = self.conn.execute("""
            SELECT COUNT(*) FROM events e
            JOIN event_evidence v ON v.event_id = e.event_id
            JOIN articles a ON a.article_id = v.article_id
            WHERE e.publication_time < a.published_at
        """).fetchone()[0]
        self.assertEqual(mismatched, 0)

    def test_processing_time_is_later_than_information_time_everywhere(self):
        backwards = self.conn.execute("""
            SELECT COUNT(*) FROM research_observations
            WHERE observation_created_at <= information_cutoff
        """).fetchone()[0]
        self.assertEqual(backwards, 0)

    def test_first_reported_at_is_the_earliest_of_its_evidence(self):
        """
        `first_reported_at` means the earliest report, so later
        corroborating articles publishing after it is correct -- 411 of
        1,541 evidence links do, and should.

        What must never happen is evidence *predating* the first report,
        because that would mean the event's information time was set
        later than the moment it became knowable.
        """
        impossible = self.conn.execute("""
            SELECT COUNT(*) FROM canonical_events c
            WHERE c.first_reported_at > (
                SELECT MIN(a.published_at)
                FROM canonical_event_reports r
                JOIN event_evidence v ON v.event_id = r.report_id
                JOIN articles a ON a.article_id = v.article_id
                WHERE r.canonical_event_id = c.canonical_event_id)
        """).fetchone()[0]
        self.assertEqual(impossible, 0)

    def test_first_reported_at_matches_its_earliest_evidence_exactly(self):
        mismatched = self.conn.execute("""
            SELECT COUNT(*) FROM canonical_events c
            WHERE c.first_reported_at != (
                SELECT MIN(a.published_at)
                FROM canonical_event_reports r
                JOIN event_evidence v ON v.event_id = r.report_id
                JOIN articles a ON a.article_id = v.article_id
                WHERE r.canonical_event_id = c.canonical_event_id)
        """).fetchone()[0]
        self.assertEqual(mismatched, 0)

    def test_the_recovered_corpus_contains_no_record_from_the_future(self):
        latest_article = self.conn.execute(
            "SELECT MAX(published_at) FROM articles").fetchone()[0]
        beyond = self.conn.execute(
            "SELECT COUNT(*) FROM canonical_events WHERE first_reported_at > ?",
            (latest_article,)).fetchone()[0]
        self.assertEqual(beyond, 0)


if __name__ == "__main__":
    unittest.main()
