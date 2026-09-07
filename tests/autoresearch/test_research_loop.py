"""
tests/autoresearch/test_research_loop.py
--------------------------------------------------
Phase 23 §73 items 1-24, 29-32 — the loop from observation to
conclusion.

THE PROPERTY THESE DEFEND
-----------------------------
A researcher must be capable of discovering that it is wrong, and must
find that outcome as reportable as any other. Most of the tests below
check that a negative result survives: that INCONCLUSIVE is reached
before REJECTED when the evidence could not decide, that a failed
experiment is stored rather than dropped, that conflicting results are
not averaged, and that nothing is deleted.

The two most important tests in this file are the ones that catch a
system flattering itself:

`test_a_run_that_never_happened_is_not_a_finding` — an experiment that
could not execute was being reported as INSUFFICIENT_DATA, which reads
as "we tested and the sample was small" rather than "we never tested".
That bug was live, and produced four such conclusions on real data.

`test_a_cohort_that_cannot_be_expressed_is_refused` — the hypothesis
generator was producing claims the evaluators cannot run, which is how
the first bug got its input.
"""

import json
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.autoresearch_schema import initialize_autoresearch_schema
from src.data_access.experiment_schema import initialize_experiment_schema
from src.data_access.memory_schema import initialize_memory_schema
from src.domain.autoresearch_models import (
    MIN_RESEARCH_SAMPLE, Actor, BudgetExceeded, CandidateStatus, CandidateType,
    ConclusionType, Evidence, FalsifiabilityCriteria, ObservationKind,
    PriorityScore, QueueState, QuestionSource, ResearchBudget,
    ResearchCandidate, ResearchConclusion, ResearchConfidence, ResearchCost,
    ResearchHypothesis, ResearchObservation, ResearchQuestion, TriageState,
    assess_confidence, overfitting_warnings, research_quality_gate,
)
from src.autoresearch import (
    api, audit, candidates as candidate_registry, cycle, governance,
    hypotheses as hypothesis_layer, observations as observation_layer,
    prioritization, questions as question_layer, queue as queue_layer,
)

BASE = datetime(2026, 6, 1, tzinfo=timezone.utc)


# ======================================================================
# Fixtures
# ======================================================================

def a_database(count=400, strong_is_better=True):
    conn = sqlite3.connect(":memory:")
    initialize_memory_schema(conn)
    initialize_experiment_schema(conn)
    initialize_autoresearch_schema(conn)
    columns = [row[1] for row in conn.execute(
        "PRAGMA table_info(trading_experiences)")]
    for i in range(count):
        strong = (i % 2 == 0)
        correct = ((i % 10 != 0) if strong else (i % 3 == 0)) \
            if strong_is_better else (i % 3 == 0)
        record = {
            "experience_id": "exp-%04d" % i, "memory_version": "v1",
            "kind": "signal", "subject_kind": "signal",
            "subject_id": "sig-%04d" % i, "horizon": "5d",
            "quality": "validated",
            "experience_class": "correct_call" if correct else "wrong_call",
            "expected_direction": "long" if i % 2 == 0 else "short",
            "expected_return": 0.02,
            "actual_return": 0.03 if correct else -0.02,
            "direction_result": "hit" if correct else "miss",
            "signal_strength": 0.9 if strong else 0.3,
            "signal_confidence": 0.8 if strong else 0.4,
            "event_type": "earnings" if i % 3 == 0 else "acquisition",
            "asset_class": "equity", "instrument_id": "INST%02d" % (i % 12),
            "created_at": (BASE + timedelta(days=i)).isoformat(),
            "available_at": (BASE + timedelta(days=i)).isoformat(),
            "information_cutoff": (BASE + timedelta(days=i)).isoformat(),
        }
        usable = {k: v for k, v in record.items() if k in columns}
        conn.execute("INSERT INTO trading_experiences (%s) VALUES (%s)"
                     % (", ".join(usable), ", ".join("?" * len(usable))),
                     tuple(usable.values()))
    conn.commit()
    return conn


def an_observation(**overrides):
    fields = dict(
        observation_id="obs-test-1",
        kind=ObservationKind.SIGNAL_WEAKNESS,
        subject="event_type=earnings",
        statement="The cohort event_type=earnings underperforms the base rate.",
        sample_size=200,
        measures={"hit_rate": 0.30, "base_rate": 0.50,
                  "conditions": {"event_type": "earnings"},
                  "instrument_count": 12, "quality": "confirmed",
                  "stability": "stable"},
        evidence=[Evidence(kind="memory_patterns", reference="pat-1",
                           detail="200 experiences")],
        source_kind=QuestionSource.MEMORY_PATTERN,
        source_reference="pat-1")
    fields.update(overrides)
    return ResearchObservation(**fields)


def a_hypothesis(conn, observation=None):
    observation = observation or an_observation()
    question = question_layer.from_observation(observation)
    question.triage = TriageState.TESTABLE
    question.triage_reason = "fixture"
    return hypothesis_layer.from_question(question, observation)


# ======================================================================
# Observations and questions
# ======================================================================

class TestObservations(unittest.TestCase):

    def test_an_observation_must_reference_evidence(self):
        """§4: an unevidenced observation is an opinion."""
        with self.assertRaises(ValueError):
            an_observation(evidence=[]).validate()

    def test_a_detector_without_inputs_raises_rather_than_returning_nothing(self):
        """
        §4: "we looked and found nothing" and "we cannot look" are
        different findings, and the second must not be reported as the
        first.
        """
        empty = sqlite3.connect(":memory:")
        with self.assertRaises(observation_layer.DetectorUnavailable):
            observation_layer.recurring_error(empty)
        empty.close()

    def test_observe_all_returns_the_blind_spots_alongside_the_findings(self):
        conn = a_database()
        found, blind = observation_layer.observe_all(conn)
        self.assertTrue(blind, "no blind spot reported at all")
        for gap in blind:
            self.assertTrue(gap["reason"].strip())
        conn.close()

    def test_a_batch_with_colliding_ids_is_refused(self):
        """
        A detector keying on something that does not distinguish its
        findings writes fewer rows than it was handed and reports the
        larger number. That happened: 26 observations became 23 rows.
        """
        conn = a_database()
        twice = [an_observation(), an_observation()]
        with self.assertRaises(ValueError):
            observation_layer.save(conn, twice)
        conn.close()


class TestQuestions(unittest.TestCase):

    def test_a_question_must_be_phrased_as_a_question(self):
        with self.assertRaises(ValueError):
            ResearchQuestion(
                question_id="q1", title="t",
                question="Momentum is bad.",
                evidence=[Evidence(kind="x", reference="y")]).validate()

    def test_a_small_cohort_is_insufficient_data_not_low_priority(self):
        conn = a_database()
        observation = an_observation(sample_size=8)
        question = question_layer.from_observation(observation)
        question, _s, _c = question_layer.triage(conn, question, observation)
        self.assertEqual(question.triage, TriageState.INSUFFICIENT_DATA)
        self.assertIn("30", question.triage_reason)
        conn.close()

    def test_a_repeated_claim_is_marked_duplicate(self):
        conn = a_database()
        observation = an_observation()
        claim = question_layer._claim_key(observation)
        question = question_layer.from_observation(observation)
        question, _s, _c = question_layer.triage(
            conn, question, observation, existing_claims=[claim])
        self.assertEqual(question.triage, TriageState.DUPLICATE)
        conn.close()

    def test_every_triage_decision_records_a_reason(self):
        """§6: the refusal without its reason is a silence."""
        conn = a_database()
        found, _blind = observation_layer.observe_all(conn)
        for question, _s, _c in question_layer.raise_questions(conn, found):
            self.assertTrue(question.triage_reason.strip(),
                            "%s has no reason" % question.triage.value)
        conn.close()


# ======================================================================
# Leakage — the adversarial case that mattered most
# ======================================================================

class TestLeakage(unittest.TestCase):
    """§74: the researcher must not be able to see the future."""

    def test_a_cohort_keyed_on_an_outcome_field_is_refused(self):
        """
        `primary_error` is Phase 20's verdict about what went wrong,
        knowable only after the outcome. A filter cannot consult it.

        This is not hypothetical: the first triage run on real data
        produced two TOP-PRIORITY questions asking whether excluding
        signals whose primary_error is prediction_error improves
        accuracy. 46 memory patterns are keyed this way.
        """
        with self.assertRaises(governance.LeakageRefused):
            governance.assert_decision_time({"primary_error": "prediction_error"})

    def test_decision_time_fields_are_allowed(self):
        governance.assert_decision_time(
            {"event_type": "earnings", "horizon": "5d"})

    def test_such_a_question_is_marked_untestable_with_a_leakage_reason(self):
        conn = a_database()
        observation = an_observation(
            measures={"hit_rate": 0.3, "base_rate": 0.5, "instrument_count": 9,
                      "conditions": {"horizon": "1d",
                                     "primary_error": "prediction_error"}})
        question = question_layer.from_observation(observation)
        question, _s, _c = question_layer.triage(conn, question, observation)
        self.assertEqual(question.triage, TriageState.UNTESTABLE)
        self.assertIn("hindsight", question.triage_reason)
        conn.close()

    def test_unknown_fields_are_reported_rather_than_guessed(self):
        """
        Guessing would make the guard unpredictable as the schema
        grows, and a false refusal trains people to disable it.
        """
        self.assertEqual(governance.leaking_fields({"brand_new_field": 1}), [])
        self.assertEqual(governance.unknown_fields({"brand_new_field": 1}),
                         ["brand_new_field"])


# ======================================================================
# Hypotheses
# ======================================================================

class TestHypotheses(unittest.TestCase):

    def test_a_vague_hypothesis_is_rejected(self):
        """§9's own bad example: "Maybe momentum is bad"."""
        conn = a_database()
        hypothesis = a_hypothesis(conn)
        hypothesis.statement = "Maybe momentum is bad."
        problems = hypothesis.quality_problems()
        self.assertTrue(any("vague" in p for p in problems), problems)
        conn.close()

    def test_a_hypothesis_without_a_mechanism_is_rejected(self):
        conn = a_database()
        hypothesis = a_hypothesis(conn)
        hypothesis.mechanism = "   "
        self.assertTrue(any("mechanism" in p
                            for p in hypothesis.quality_problems()))
        conn.close()

    def test_falsifiability_requires_a_positive_minimum_effect(self):
        """A minimum effect of zero makes every outcome a success."""
        with self.assertRaises(ValueError):
            FalsifiabilityCriteria(expected_result="x",
                                   minimum_effect=0.0).validate()

    def test_falsifiability_is_fixed_before_the_test(self):
        conn = a_database()
        hypothesis = a_hypothesis(conn)
        experiment = cycle.build_experiment(conn, hypothesis)
        self.assertEqual(experiment.criteria.min_effect,
                         hypothesis.falsifiability.minimum_effect)
        self.assertEqual(experiment.criteria.min_sample,
                         hypothesis.falsifiability.minimum_sample)
        conn.close()

    def test_a_cohort_that_cannot_be_expressed_is_refused(self):
        """
        The generator must not produce a claim the evaluators cannot
        run. An unrunnable experiment has no result, and interpreting
        an absent result is how "never tested" became "tested, sample
        too small".
        """
        conn = a_database()
        observation = an_observation(
            measures={"hit_rate": 0.3, "base_rate": 0.5, "instrument_count": 9,
                      "conditions": {"trained_model_id": "tm-1"}})
        question = question_layer.from_observation(observation)
        question.triage = TriageState.TESTABLE
        with self.assertRaises(hypothesis_layer.HypothesisRefused):
            hypothesis_layer.from_question(question, observation)
        conn.close()

    def test_two_hypotheses_with_the_same_claim_share_a_fingerprint(self):
        """§13: deduplication keys on the claim, never on the wording."""
        conn = a_database()
        first = a_hypothesis(conn)
        second = a_hypothesis(conn)
        second.statement = "A completely different sentence entirely."
        self.assertEqual(first.claim_fingerprint, second.claim_fingerprint)
        conn.close()

    def test_a_duplicate_is_found_against_stored_hypotheses(self):
        conn = a_database()
        hypothesis = a_hypothesis(conn)
        hypothesis_layer.save(conn, [hypothesis])
        twin = a_hypothesis(conn)
        twin.hypothesis_id = "h-different"
        self.assertIsNotNone(hypothesis_layer.find_duplicate(conn, twin))
        conn.close()

    def test_research_context_reports_what_is_already_known(self):
        """§15: a researcher that cannot recall its failures repeats them."""
        conn = a_database()
        hypothesis = a_hypothesis(conn)
        context = hypothesis_layer.research_context(conn, hypothesis)
        for key in ("duplicate", "family", "family_status", "multiple_testing"):
            self.assertIn(key, context)
        conn.close()


# ======================================================================
# Prioritization, cost, families
# ======================================================================

class TestPrioritization(unittest.TestCase):

    def test_priority_stores_every_component_not_just_the_total(self):
        """§11: a priority nobody can argue with is one nobody trusts."""
        score = PriorityScore(evidence_strength=0.8, sample_adequacy=0.9,
                              novelty=1.0, weakness_relevance=1.0,
                              confidence=0.7)
        data = score.as_dict()
        for name in PriorityScore.WEIGHTS:
            self.assertIn(name, data)
        self.assertIn("total", data)
        self.assertTrue(score.explain())

    def test_priority_carries_no_predicted_profit_component(self):
        """
        §11 forbids ranking on predicted profitability alone. The
        reliable way to obey that is not to compute it: a field that
        exists gets weighted eventually.
        """
        for name in PriorityScore.WEIGHTS:
            self.assertNotIn("profit", name)
            self.assertNotIn("return", name)

    def test_a_single_instrument_observation_carries_a_risk_penalty(self):
        """§74: one asset must not become a universal rule."""
        one = prioritization.score_observation(
            an_observation(measures={"instrument_count": 1}),
            novelty=1.0, cost=ResearchCost())
        many = prioritization.score_observation(
            an_observation(measures={"instrument_count": 12}),
            novelty=1.0, cost=ResearchCost())
        self.assertGreater(one.risk_penalty, many.risk_penalty)

    def test_a_family_with_no_support_after_enough_tries_is_depleted(self):
        """§65: stop testing an idea that keeps failing."""
        status, reason = prioritization.assess_family({
            "experiments": prioritization.DEPLETION_THRESHOLD,
            "supported": 0, "rejected": 6, "inconclusive": 0,
            "best_effect": 0.004, "median_effect": -0.002})
        self.assertEqual(status.value, "research_depleted")
        self.assertIn("median", reason)

    def test_a_family_reports_median_beside_best(self):
        """§20: do not select only the best experiment."""
        conn = a_database()
        stats = prioritization.family_statistics(conn, "fam-x")
        self.assertIn("best_effect", stats)
        self.assertIn("median_effect", stats)
        conn.close()

    def test_a_supported_family_stays_active(self):
        status, _reason = prioritization.assess_family({
            "experiments": 9, "supported": 1, "rejected": 8,
            "inconclusive": 0, "best_effect": 0.1, "median_effect": 0.0})
        self.assertEqual(status.value, "active")


# ======================================================================
# Queue, budget, scheduling
# ======================================================================

class TestQueueAndBudget(unittest.TestCase):

    def setUp(self):
        self.conn = a_database()
        self.hypothesis = a_hypothesis(self.conn)
        hypothesis_layer.save(self.conn, [self.hypothesis])

    def tearDown(self):
        self.conn.close()

    def test_an_item_can_be_queued_and_moved_through_states(self):
        queue_id = queue_layer.enqueue(self.conn, self.hypothesis, priority=0.9)
        queue_layer.set_state(self.conn, queue_id, QueueState.RUNNING)
        queue_layer.set_state(self.conn, queue_id, QueueState.COMPLETED,
                              reason="done")
        self.assertEqual(queue_layer.depth(self.conn)["completed"], 1)

    def test_cancellation_keeps_the_item_and_its_reason(self):
        """§25: nothing is deleted; a cancelled item says it was."""
        queue_id = queue_layer.enqueue(self.conn, self.hypothesis, priority=0.5)
        queue_layer.cancel(self.conn, queue_id, "stopped by hand")
        rows = queue_layer.listing(self.conn, state="cancelled")
        self.assertEqual(len(rows), 1)
        self.assertIn("stopped by hand", rows[0]["reason"])

    def test_the_cycle_budget_caps_what_is_selected(self):
        """§24: a research explosion is prevented by refusing, not trimming."""
        for i in range(8):
            other = a_hypothesis(self.conn)
            other.hypothesis_id = "h-fill-%d" % i
            other.family_id = "fam-%d" % i
            hypothesis_layer.save(self.conn, [other])
            queue_layer.enqueue(self.conn, other, priority=0.5)
        budget = ResearchBudget(max_experiments_per_cycle=3)
        selected, skipped = queue_layer.next_batch(self.conn, budget=budget)
        self.assertLessEqual(len(selected), 3)
        self.assertTrue(skipped)
        for item in skipped:
            self.assertTrue(item["reason"])

    def test_the_daily_budget_refuses_rather_than_exceeds(self):
        budget = ResearchBudget(max_experiments_per_day=0)
        with self.assertRaises(BudgetExceeded):
            queue_layer.assert_daily_budget(
                self.conn, budget=budget, day="2026-09-07T00:00:00+00:00")

    def test_only_one_worker_can_claim_an_item(self):
        """
        §10: no duplicate experiment execution.

        `next_batch` only READS. Before `claim` existed, two workers
        calling it before either marked anything RUNNING both selected
        the same item -- verified directly during the Phase 23.5 audit.
        The transition is now a single conditional UPDATE, so SQLite
        picks the winner and exactly one caller sees rowcount 1.
        """
        queue_id = queue_layer.enqueue(self.conn, self.hypothesis,
                                       priority=0.9)
        first = queue_layer.claim(self.conn, queue_id)
        second = queue_layer.claim(self.conn, queue_id)
        self.assertTrue(first)
        self.assertFalse(second, "two workers claimed the same item")

    def test_a_claimed_item_leaves_the_selectable_pool(self):
        queue_id = queue_layer.enqueue(self.conn, self.hypothesis,
                                       priority=0.9)
        queue_layer.claim(self.conn, queue_id)
        selected, _skipped = queue_layer.next_batch(
            self.conn, budget=ResearchBudget())
        self.assertEqual(selected, [])

    def test_an_abandoned_running_item_is_reclaimed(self):
        """
        §59: recovery.

        A worker that dies mid-run left its item RUNNING forever --
        `next_batch` considers only queued and prioritized, so it was
        never retried and never reported. It simply stopped existing as
        far as the research programme was concerned.
        """
        queue_id = queue_layer.enqueue(self.conn, self.hypothesis,
                                       priority=0.9)
        queue_layer.claim(self.conn, queue_id)
        self.conn.execute(
            "UPDATE autoresearch_queue SET started_at = ? WHERE queue_id = ?",
            ("2020-01-01T00:00:00+00:00", queue_id))
        self.conn.commit()

        reclaimed = queue_layer.reclaim_stale(self.conn)
        self.assertEqual(len(reclaimed), 1)
        row = queue_layer.listing(self.conn)[0]
        self.assertEqual(row["state"], "queued")
        self.assertIn("reclaimed", row["reason"])

    def test_a_live_running_item_is_not_reclaimed(self):
        """A slow-but-alive run must not be taken from under itself."""
        queue_id = queue_layer.enqueue(self.conn, self.hypothesis,
                                       priority=0.9)
        queue_layer.claim(self.conn, queue_id)
        self.assertEqual(queue_layer.reclaim_stale(self.conn), [])
        self.assertEqual(queue_layer.listing(self.conn)[0]["state"], "running")

    def test_two_items_making_one_claim_do_not_both_run(self):
        twin = a_hypothesis(self.conn)
        twin.hypothesis_id = "h-twin"
        hypothesis_layer.save(self.conn, [twin])
        queue_layer.enqueue(self.conn, self.hypothesis, priority=0.9)
        queue_layer.enqueue(self.conn, twin, priority=0.8)
        selected, skipped = queue_layer.next_batch(
            self.conn, budget=ResearchBudget())
        self.assertEqual(len(selected), 1)
        self.assertTrue(any("identical claim" in s["reason"] for s in skipped))


# ======================================================================
# Interpretation and conclusions
# ======================================================================

class TestConclusions(unittest.TestCase):

    def setUp(self):
        self.conn = a_database()
        self.hypothesis = a_hypothesis(self.conn)
        hypothesis_layer.save(self.conn, [self.hypothesis])

    def tearDown(self):
        self.conn.close()

    def _interpret(self, **result):
        base = {"effect": 0.05, "effect_in_sample": 0.05,
                "effect_low": 0.02, "effect_high": 0.08,
                "robust_slices": 3, "robust_slices_passing": 3,
                "candidate_out_of_sample": {"sample_size": 200,
                                            "instrument_count": 9}}
        base.update(result)
        return cycle.interpret(self.conn, self.hypothesis, "exp-1", base)

    def test_a_tiny_sample_is_insufficient_data_not_rejected(self):
        """§43: do not force a conclusion."""
        conclusion = self._interpret(
            candidate_out_of_sample={"sample_size": 5})
        self.assertEqual(conclusion.conclusion, ConclusionType.INSUFFICIENT_DATA)

    def test_an_interval_including_zero_is_inconclusive_not_rejected(self):
        """
        "We could not tell" and "it does not work" are different
        findings, and collapsing them loses the more useful one.
        """
        conclusion = self._interpret(effect=0.01, effect_low=-0.05,
                                     effect_high=0.07)
        self.assertEqual(conclusion.conclusion, ConclusionType.INCONCLUSIVE)
        self.assertIn("not a rejection", " ".join(conclusion.reasons))

    def test_an_effect_in_the_wrong_direction_is_rejected(self):
        conclusion = self._interpret(effect=-0.05, effect_low=-0.09,
                                     effect_high=-0.01)
        self.assertEqual(conclusion.conclusion, ConclusionType.REJECTED)

    def test_a_positive_effect_below_the_bar_is_partially_supported(self):
        conclusion = self._interpret(effect=0.005, effect_low=0.001,
                                     effect_high=0.01)
        self.assertEqual(conclusion.conclusion,
                         ConclusionType.PARTIALLY_SUPPORTED)

    def test_a_clear_effect_is_supported(self):
        self.assertEqual(self._interpret().conclusion, ConclusionType.SUPPORTED)

    def test_every_conclusion_carries_reasons(self):
        with self.assertRaises(ValueError):
            ResearchConclusion(conclusion_id="c", hypothesis_id="h",
                               question_id="q", reasons=[]).validate()

    def test_a_negative_conclusion_is_stored_like_any_other(self):
        """§42: negative research is kept."""
        conclusion = self._interpret(effect=-0.05, effect_low=-0.09,
                                     effect_high=-0.01)
        cycle.save_conclusion(self.conn, conclusion)
        rows = api.conclusions(self.conn, conclusion="rejected")
        self.assertEqual(len(rows), 1)

    def test_a_disagreeing_prior_conclusion_produces_conflicting_evidence(self):
        """§44: do not average blindly."""
        rejected = self._interpret(effect=-0.05, effect_low=-0.09,
                                   effect_high=-0.01)
        cycle.save_conclusion(self.conn, rejected)
        second = self._interpret()
        self.assertEqual(second.conclusion, ConclusionType.CONFLICTING_EVIDENCE)
        self.assertIn("NOT averaged", " ".join(second.reasons))

    def test_an_inconclusive_prior_does_not_manufacture_conflict(self):
        """
        Treating an absence of finding as a disagreement would create
        conflict out of silence.
        """
        inconclusive = self._interpret(effect=0.01, effect_low=-0.05,
                                       effect_high=0.07)
        cycle.save_conclusion(self.conn, inconclusive)
        second = self._interpret()
        self.assertEqual(second.conclusion, ConclusionType.SUPPORTED)

    def test_conclusions_are_never_deleted_by_this_package(self):
        import ast
        package = os.path.join(os.path.dirname(__file__), "..", "..",
                               "src", "autoresearch")
        for name in sorted(os.listdir(package)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(package, name), encoding="utf-8") as handle:
                source = handle.read()
            self.assertNotIn("DELETE FROM autoresearch_conclusions", source,
                             "%s deletes conclusions" % name)


# ======================================================================
# The quality gate and candidates
# ======================================================================

class TestQualityGate(unittest.TestCase):

    def _gate(self, **overrides):
        args = dict(conclusion=ConclusionType.SUPPORTED, effect=0.05,
                    falsifiability=FalsifiabilityCriteria(expected_result="x"),
                    sample_size=200, effect_low=0.02, effect_high=0.08,
                    slices_total=3, slices_passing=3, complexity_ratio=2.0,
                    warnings=[])
        args.update(overrides)
        return research_quality_gate(**args)

    def test_a_clean_supported_result_is_promising(self):
        promising, reasons = self._gate()
        self.assertTrue(promising)
        self.assertTrue(any("does not mean profitable" in r.lower()
                            or "not mean profitable" in r for r in reasons))

    def test_only_a_supported_conclusion_can_be_promising(self):
        for conclusion in (ConclusionType.REJECTED, ConclusionType.INCONCLUSIVE,
                           ConclusionType.PARTIALLY_SUPPORTED,
                           ConclusionType.INSUFFICIENT_DATA,
                           ConclusionType.CONFLICTING_EVIDENCE):
            promising, _r = self._gate(conclusion=conclusion)
            self.assertFalse(promising, conclusion)

    def test_test_set_reuse_blocks_promising(self):
        """§22: a result found by retuning the same window is not a find."""
        promising, reasons = self._gate(warnings=["test_set_reuse"])
        self.assertFalse(promising)
        self.assertTrue(any("test_set_reuse" in r for r in reasons))

    def test_an_interval_including_zero_blocks_promising(self):
        promising, _r = self._gate(effect_low=-0.01, effect_high=0.09)
        self.assertFalse(promising)

    def test_weak_robustness_blocks_promising(self):
        promising, _r = self._gate(slices_total=3, slices_passing=1)
        self.assertFalse(promising)

    def test_excessive_complexity_blocks_promising(self):
        """§58: complexity must be earned."""
        promising, _r = self._gate(complexity_ratio=9.0)
        self.assertFalse(promising)

    def test_the_gate_explains_itself_when_it_says_yes(self):
        _promising, reasons = self._gate()
        self.assertGreater(len(reasons), 3)


class TestCandidates(unittest.TestCase):

    def setUp(self):
        self.conn = a_database()
        self.hypothesis = a_hypothesis(self.conn)
        hypothesis_layer.save(self.conn, [self.hypothesis])

    def tearDown(self):
        self.conn.close()

    def _conclusion(self, promising=True):
        return ResearchConclusion(
            conclusion_id="con-1", hypothesis_id=self.hypothesis.hypothesis_id,
            question_id="q-1", experiment_id="exp-1",
            conclusion=ConclusionType.SUPPORTED,
            confidence=ResearchConfidence.MEDIUM,
            effect=0.05, sample_size=200, reasons=["because"],
            promising=promising)

    def test_a_promising_conclusion_becomes_a_candidate_requiring_review(self):
        candidate = candidate_registry.propose(
            self.conn, hypothesis=self.hypothesis,
            conclusion=self._conclusion())
        self.assertTrue(candidate.requires_review)
        self.assertEqual(candidate.status, CandidateStatus.READY_FOR_REVIEW)
        self.assertTrue(candidate.review_reason)

    def test_a_non_promising_conclusion_cannot_become_a_candidate(self):
        with self.assertRaises(candidate_registry.PromotionRefused):
            candidate_registry.propose(
                self.conn, hypothesis=self.hypothesis,
                conclusion=self._conclusion(promising=False))

    def test_a_candidate_must_name_what_it_changes_from(self):
        """§56: a change with no base is not reproducible."""
        with self.assertRaises(ValueError):
            ResearchCandidate(
                candidate_id="c", candidate_type=CandidateType.SIGNAL,
                name="x", hypothesis_id="h", conclusion_id="c",
                base_version="", changes={"a": 1}).validate()

    def test_a_candidate_cannot_be_created_already_promoted(self):
        with self.assertRaises(ValueError):
            ResearchCandidate(
                candidate_id="c", candidate_type=CandidateType.SIGNAL,
                name="x", hypothesis_id="h", conclusion_id="c",
                base_version="b", changes={"a": 1},
                status=CandidateStatus.PROMOTED).validate()

    def test_promotion_always_refuses(self):
        """§34: research may recommend; it may not deploy."""
        with self.assertRaises(candidate_registry.PromotionRefused):
            candidate_registry.promote(self.conn, "cand-anything")

    def test_a_rejected_candidate_is_kept(self):
        candidate = candidate_registry.propose(
            self.conn, hypothesis=self.hypothesis,
            conclusion=self._conclusion())
        candidate_registry.reject(self.conn, candidate.candidate_id, "no")
        rows = candidate_registry.listing(self.conn, status="rejected")
        self.assertEqual(len(rows), 1)


# ======================================================================
# The whole loop
# ======================================================================

class TestTheCycle(unittest.TestCase):

    def test_a_dry_run_writes_nothing(self):
        conn = a_database()
        report = cycle.run_cycle(conn, apply=False)
        self.assertIn("dry run", report["termination_reason"])
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM autoresearch_hypotheses"
                         ).fetchone()[0], 0)
        conn.close()

    def test_every_cycle_records_why_it_stopped(self):
        """§52: a loop that ends without saying why looks like a crash."""
        conn = a_database()
        report = cycle.run_cycle(conn, apply=True,
                                 budget=ResearchBudget(max_experiments_per_cycle=1))
        self.assertTrue(report["termination_reason"].strip())
        stored = conn.execute(
            "SELECT termination_reason FROM autoresearch_cycles").fetchall()
        self.assertTrue(all(row[0].strip() for row in stored))
        conn.close()

    def test_a_run_that_never_happened_is_not_a_finding(self):
        """
        THE REGRESSION TEST FOR THE WORST BUG IN THIS PHASE.

        An experiment Phase 22 refuses to run produces no result. The
        first version interpreted that absence as INSUFFICIENT_DATA --
        "we tested and the sample was too small" -- for something that
        was never tested at all. Four such conclusions were written on
        real data before it was caught.

        This is behavioural, not a string match: it seeds a hypothesis
        whose parameters Phase 22's evaluator will reject, runs a
        cycle, and asserts that the failure is recorded as a refusal
        rather than as a finding.
        """
        conn = a_database()
        broken = a_hypothesis(conn)
        broken.hypothesis_id = "h-unrunnable"
        broken.evaluator = "signal_composite"
        # `signal_composite` refuses unknown parameters, exactly so a
        # silently-ignored one cannot make an experiment record a
        # change it did not make.
        broken.parameters = {"a_parameter_that_does_not_exist": 1}
        hypothesis_layer.save(conn, [broken])
        queue_layer.enqueue(conn, broken, priority=1.0)

        report = cycle.run_cycle(
            conn, apply=True,
            budget=ResearchBudget(max_experiments_per_cycle=1))

        concluded = [row["hypothesis_id"] for row in api.conclusions(conn)]
        self.assertNotIn("h-unrunnable", concluded,
                         "a run that produced no result became a conclusion")

        queued = {row["hypothesis_id"]: row
                  for row in queue_layer.listing(conn, limit=50)}
        self.assertIn("h-unrunnable", queued)
        self.assertIn(queued["h-unrunnable"]["state"],
                      ("rejected", "queued"),
                      "the failed item was not recorded as refused")
        conn.close()

    def test_the_cycle_terminates_within_its_timeout(self):
        """§52: bounded, always."""
        conn = a_database()
        report = cycle.run_cycle(conn, apply=True, max_seconds=0.001)
        self.assertTrue(report["termination_reason"])
        conn.close()

    def test_the_loop_reaches_memory_and_stops_there(self):
        """
        §39, §85: the conclusion IS the memory update, read back
        through `memory_feedback`. Phase 21's tables are never written.
        """
        conn = a_database()
        cycle.run_cycle(conn, apply=True,
                        budget=ResearchBudget(max_experiments_per_cycle=2))
        before = conn.execute(
            "SELECT COUNT(*) FROM trading_experiences").fetchone()[0]
        self.assertEqual(before, 400, "Phase 21's record was modified")
        feedback = cycle.memory_feedback(conn, "pat-1")
        self.assertIsInstance(feedback, list)
        conn.close()

    def test_the_same_cycle_twice_does_not_double_the_record(self):
        """§72: reproducible, and idempotent on identity."""
        conn = a_database()
        cycle.run_cycle(conn, apply=True,
                        budget=ResearchBudget(max_experiments_per_cycle=2))
        first = conn.execute(
            "SELECT COUNT(*) FROM autoresearch_hypotheses").fetchone()[0]
        cycle.run_cycle(conn, apply=True,
                        budget=ResearchBudget(max_experiments_per_cycle=2))
        second = conn.execute(
            "SELECT COUNT(*) FROM autoresearch_hypotheses").fetchone()[0]
        self.assertEqual(first, second,
                         "re-running the cycle duplicated hypotheses")
        conn.close()


class TestDatasetIdentityAndCache(unittest.TestCase):
    """
    §18, §55 — an experiment must not silently observe a changed
    dataset, and a cached result must not be served as current
    research.
    """

    def _grow(self, conn, start, count):
        columns = [row[1] for row in conn.execute(
            "PRAGMA table_info(trading_experiences)")]
        for i in range(start, start + count):
            record = {
                "experience_id": "exp-%04d" % i, "memory_version": "v1",
                "kind": "signal", "subject_kind": "signal",
                "subject_id": "s%d" % i, "horizon": "5d",
                "quality": "validated", "experience_class": "correct_call",
                "expected_direction": "long", "direction_result": "hit",
                "actual_return": 0.03, "signal_strength": 0.9,
                "signal_confidence": 0.8, "event_type": "earnings",
                "asset_class": "equity", "instrument_id": "INST01",
                "created_at": (BASE + timedelta(days=i)).isoformat(),
                "available_at": (BASE + timedelta(days=i)).isoformat(),
            }
            usable = {k: v for k, v in record.items() if k in columns}
            conn.execute(
                "INSERT INTO trading_experiences (%s) VALUES (%s)"
                % (", ".join(usable), ", ".join("?" * len(usable))),
                tuple(usable.values()))
        conn.commit()

    def test_a_grown_record_is_a_different_experiment(self):
        """
        THE REGRESSION TEST FOR THE STALE-CACHE DEFECT.

        Reproduced during the Phase 23.5 audit: 300 experiences gave an
        effect of +0.3333; 150 more arrived; the re-run reported
        +0.3333 as current research on 450 rows, flagged only as a
        cache hit of the earlier run.

        The dataset identity was purely definitional -- `as_of`,
        filters, versions -- so a record that GREW produced an
        identical fingerprint, and the run cache keys on that. It was
        the worst possible failure for this project, whose stated
        limitation is that the record is short and more data would
        change the answer.
        """
        from src.experiments import api as experiment_api, engine
        conn = a_database(count=300)
        hypothesis = a_hypothesis(conn)
        hypothesis_layer.save(conn, [hypothesis])

        first = cycle.build_experiment(conn, hypothesis)
        engine.save_experiment(conn, first)
        run_one = experiment_api.start(conn, first.experiment_id)

        self._grow(conn, 300, 150)

        second = cycle.build_experiment(conn, hypothesis)
        self.assertNotEqual(second.experiment_id, first.experiment_id,
                            "a grown record produced the same experiment")
        engine.save_experiment(conn, second)
        run_two = experiment_api.start(conn, second.experiment_id)

        self.assertFalse(run_two["run"]["cache_hit"],
                         "a stale result was served as current research")
        self.assertNotEqual(run_two["result"]["effect"],
                            run_one["result"]["effect"])
        conn.close()

    def test_an_unchanged_record_keeps_one_experiment(self):
        """
        The fix must not make every run a new experiment. Identical
        data must still produce an identical identity, or the cache
        never hits and re-running a hypothesis silently multiplies the
        multiple-testing count.
        """
        conn = a_database(count=300)
        hypothesis = a_hypothesis(conn)
        first = cycle.build_experiment(conn, hypothesis)
        second = cycle.build_experiment(conn, hypothesis)
        self.assertEqual(first.experiment_id, second.experiment_id)
        self.assertEqual(first.fingerprint, second.fingerprint)
        conn.close()

    def test_the_dataset_records_how_far_the_record_went(self):
        conn = a_database(count=300)
        experiment = cycle.build_experiment(conn, a_hypothesis(conn))
        self.assertTrue(experiment.dataset.data_cutoff,
                        "the experiment does not record its data cutoff")
        self.assertTrue(experiment.dataset.snapshot_id)
        conn.close()


class TestConfidence(unittest.TestCase):

    def test_a_small_sample_gives_insufficient_confidence(self):
        self.assertEqual(
            assess_confidence(sample_size=5, effect_low=0.1, effect_high=0.2,
                              slices_total=3, slices_passing=3, warnings=[]),
            ResearchConfidence.INSUFFICIENT)

    def test_an_interval_spanning_zero_gives_low_confidence(self):
        self.assertEqual(
            assess_confidence(sample_size=500, effect_low=-0.1,
                              effect_high=0.2, slices_total=3,
                              slices_passing=3, warnings=[]),
            ResearchConfidence.LOW)

    def test_a_robust_large_clean_result_gives_high_confidence(self):
        self.assertEqual(
            assess_confidence(sample_size=500, effect_low=0.05,
                              effect_high=0.2, slices_total=3,
                              slices_passing=3, warnings=[]),
            ResearchConfidence.HIGH)

    def test_a_serious_warning_lowers_confidence(self):
        self.assertNotEqual(
            assess_confidence(sample_size=500, effect_low=0.05,
                              effect_high=0.2, slices_total=3,
                              slices_passing=3,
                              warnings=["test_set_reuse",
                                        "large_train_test_gap"]),
            ResearchConfidence.HIGH)


class TestOverfittingWarnings(unittest.TestCase):
    """§19: the shapes, reported as facts rather than as a score."""

    def test_a_sign_flip_between_halves_is_flagged(self):
        self.assertIn("unstable_sign",
                      overfitting_warnings(effect=-0.02, effect_in_sample=0.1))

    def test_a_large_gap_is_flagged(self):
        self.assertIn("large_train_test_gap",
                      overfitting_warnings(effect=0.01, effect_in_sample=0.10))

    def test_a_single_point_optimum_is_flagged(self):
        self.assertIn("single_parameter_peak",
                      overfitting_warnings(effect=0.05, effect_in_sample=0.05,
                                           sensitivity_shape="single_point"))

    def test_a_short_record_is_flagged(self):
        self.assertIn("narrow_time_period",
                      overfitting_warnings(effect=0.05, effect_in_sample=0.05,
                                           span_days=28))

    def test_repeated_use_of_one_window_is_flagged(self):
        self.assertIn("test_set_reuse",
                      overfitting_warnings(effect=0.05, effect_in_sample=0.05,
                                           window_reuse_count=4))

    def test_one_instrument_is_flagged(self):
        self.assertIn("single_instrument",
                      overfitting_warnings(effect=0.05, effect_in_sample=0.05,
                                           instrument_count=1))

    def test_many_variants_is_flagged(self):
        self.assertIn("many_variants",
                      overfitting_warnings(effect=0.05, effect_in_sample=0.05,
                                           variants=14))

    def test_a_clean_result_is_flagged_with_nothing(self):
        self.assertEqual(
            overfitting_warnings(effect=0.05, effect_in_sample=0.055,
                                 instrument_count=12, regime_count=3,
                                 variants=2, window_reuse_count=1,
                                 span_days=400, slices_total=3,
                                 slices_passing=3),
            [])


if __name__ == "__main__":
    unittest.main()
