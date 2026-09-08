"""
tests/challengers/test_challenger_lifecycle.py
--------------------------------------------------------
Phase 24 §77 items 1-33 — candidate to verdict to review.

THE PROPERTIES THESE DEFEND
-------------------------------
1. **A comparison cannot move once it has begun.** The baseline, the
   plan and the dataset cutoff are all inside the fingerprint, and a
   started challenger refuses a changed definition. A comparison whose
   baseline can shift after the numbers exist is an anecdote.

2. **SUPERIOR is hard to reach and easy to lose.** Every dimension
   must be measured AND favourable. An unmeasured dimension blocks it
   — the first version of `decide` counted only "worse", so a
   challenger reached SUPERIOR while its stability had never been
   measured and the reasons list printed "met: stability — walk-forward
   could not be run".

3. **Contexts are preserved.** A challenger that wins in some slices
   and loses in others is CONTEXT_DEPENDENT, not a modest global win.

4. **Nothing reaches production.** The furthest state is
   PAPER_CANDIDATE, set only by a named human with a reason.
"""

import json
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.autoresearch_schema import initialize_autoresearch_schema
from src.data_access.challenger_schema import initialize_challenger_schema
from src.data_access.experiment_schema import initialize_experiment_schema
from src.data_access.memory_schema import initialize_memory_schema
from src.domain.challenger_models import (
    MIN_CHALLENGER_SAMPLE, Actor, BaselineKind, BaselineSpec, Challenger,
    ChallengerChanged, ChallengerDecision, ChallengerLimits, ChallengerResult,
    ChallengerStatus, ChangeDefinition, Dimension, EvaluationPlan,
    LimitExceeded, ReviewOutcome, RunEnvironment, Scorecard, SliceResult,
    VariantType, build_scorecard, decide, validate_candidate,
)
from src.challengers import api, evaluation, registry, workflow

BASE = datetime(2026, 6, 1, tzinfo=timezone.utc)


# ======================================================================
# Fixtures
# ======================================================================

def a_database(count=400):
    conn = sqlite3.connect(":memory:")
    initialize_memory_schema(conn)
    initialize_experiment_schema(conn)
    initialize_autoresearch_schema(conn)
    initialize_challenger_schema(conn)
    columns = [row[1] for row in conn.execute(
        "PRAGMA table_info(trading_experiences)")]
    for i in range(count):
        favoured = (i % 3 == 0)          # the cohort the challenger keeps
        correct = (i % 4 != 0) if favoured else (i % 2 == 0)
        record = {
            "experience_id": "exp-%04d" % i, "memory_version": "v1",
            "kind": "signal", "subject_kind": "signal",
            "subject_id": "sig-%04d" % i, "horizon": "3d" if favoured else "5d",
            "quality": "validated",
            "experience_class": "correct_call" if correct else "wrong_call",
            "expected_direction": "long", "expected_return": 0.02,
            "actual_return": 0.03 if correct else -0.02,
            "direction_result": "hit" if correct else "miss",
            "signal_strength": 0.8, "signal_confidence": 0.7,
            "event_type": "acquisition" if favoured else "earnings",
            "asset_class": "equity", "instrument_id": "INST%02d" % (i % 10),
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


def a_candidate(conn, candidate_id="cand-test-1", *, effect=0.12,
                base_version="baseline:all_signals", evaluator="signal_composite",
                hypothesis_id="h-test", conclusion_id="con-test",
                experiment_id="exp-test"):
    """A Phase 23 candidate row, with the lineage a challenger requires."""
    conn.execute("""
        INSERT OR REPLACE INTO autoresearch_hypotheses (
            hypothesis_id, method_version, statement, mechanism,
            claim_fingerprint, family_id, family_name, created_at
        ) VALUES (?,?,?,?,?,?,?,?)
    """, (hypothesis_id, "v1", "a claim", "a mechanism", "fp-1",
          "fam-test", "test family", "2026-09-07"))
    conn.execute("""
        INSERT OR REPLACE INTO autoresearch_conclusions (
            conclusion_id, method_version, hypothesis_id, conclusion,
            confidence, effect, sample_size, reasons_json, promising,
            concluded_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (conclusion_id, "v1", hypothesis_id, "supported", "medium", effect,
          200, '["because"]', 1, "2026-09-07"))
    conn.execute("""
        INSERT OR REPLACE INTO autoresearch_candidates (
            candidate_id, method_version, candidate_type, name,
            hypothesis_id, conclusion_id, experiment_id, status,
            base_version, changes_json, code_version, requires_review,
            review_reason, effect, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (candidate_id, "v1", "signal", "restrict to acquisition/3d",
          hypothesis_id, conclusion_id, experiment_id, "ready_for_review",
          base_version,
          json.dumps({"evaluator": evaluator,
                      "parameters": {"event_type": "acquisition",
                                     "horizon": "3d"},
                      "condition": {"event_type": "acquisition",
                                    "horizon": "3d"}}),
          "abc1234", 1, "a review reason", effect, "2026-09-07"))
    conn.commit()
    return candidate_id


def a_challenger(conn, **overrides):
    candidate_id = a_candidate(conn)
    challenger = registry.from_candidate(conn, candidate_id)
    for key, value in overrides.items():
        setattr(challenger, key, value)
    registry.save(conn, challenger)
    return challenger


def a_result(**overrides):
    fields = dict(
        run_id="run-1", challenger_id="chl-1", challenger_version=1,
        baseline_out_of_sample={"sample_size": 300, "instrument_count": 9},
        challenger_out_of_sample={"sample_size": 200, "instrument_count": 9},
        effect=0.08, effect_in_sample=0.09, effect_low=0.02, effect_high=0.14,
        walk_forward_folds=3, walk_forward_folds_favourable=3,
        robust_slices=3, robust_slices_favourable=3,
        complexity_ratio=2.0, economically_significant=True,
        reasons=["seeded"])
    fields.update(overrides)
    return ChallengerResult(**fields)


# ======================================================================
# Creation and candidate linkage
# ======================================================================

class TestCreation(unittest.TestCase):

    def setUp(self):
        self.conn = a_database()

    def tearDown(self):
        self.conn.close()

    def test_a_challenger_is_created_from_a_candidate(self):
        challenger = a_challenger(self.conn)
        self.assertTrue(challenger.challenger_id.startswith("chl-"))
        self.assertEqual(challenger.version, 1)

    def test_it_references_its_whole_lineage(self):
        """§7: an orphan challenger cannot be traced to its evidence."""
        challenger = a_challenger(self.conn)
        for field in ("candidate_id", "hypothesis_id", "experiment_id",
                      "conclusion_id"):
            self.assertTrue(getattr(challenger, field), field)

    def test_a_challenger_without_lineage_is_refused(self):
        challenger = a_challenger(self.conn)
        challenger.conclusion_id = ""
        with self.assertRaises(ValueError):
            challenger.validate()

    def test_a_candidate_missing_evidence_does_not_qualify(self):
        """§3: not every candidate deserves a challenger."""
        self.assertTrue(validate_candidate({}))
        self.assertTrue(validate_candidate(
            {"hypothesis_id": "h", "conclusion_id": "c",
             "experiment_id": "e", "base_version": "",
             "changes": {"evaluator": "x"}, "effect": 0.1}))

    def test_a_disqualified_candidate_is_refused_by_default(self):
        a_candidate(self.conn, "cand-bad", base_version="")
        with self.assertRaises(registry.ChallengerRefused):
            registry.from_candidate(self.conn, "cand-bad")

    def test_force_records_the_objections_rather_than_hiding_them(self):
        a_candidate(self.conn, "cand-thin", effect=None)
        challenger = registry.from_candidate(self.conn, "cand-thin",
                                             force=True)
        self.assertTrue(any("deliberately" in note
                            for note in challenger.notes))


# ======================================================================
# Baseline
# ======================================================================

class TestBaseline(unittest.TestCase):

    def setUp(self):
        self.conn = a_database()

    def tearDown(self):
        self.conn.close()

    def test_the_baseline_is_versioned(self):
        """§6: an unversioned baseline can move under a comparison."""
        challenger = a_challenger(self.conn)
        self.assertTrue(challenger.baseline.version)

    def test_an_unversioned_baseline_is_refused(self):
        with self.assertRaises(ValueError):
            BaselineSpec(kind=BaselineKind.CURRENT_SIGNAL_RULE,
                         name="x", version="").validate()

    def test_the_baseline_version_is_inside_the_fingerprint(self):
        challenger = a_challenger(self.conn)
        before = challenger.fingerprint
        challenger.baseline.version = "signal-rule:all_signals@different"
        self.assertNotEqual(before, challenger.fingerprint)

    def test_no_promoted_model_means_experimental_basis(self):
        """
        §75: a challenger built on an unpromoted model is research on
        research and must say so. Measured through Phase 18's own
        selection rather than assumed.
        """
        self.assertIsNone(registry.active_model_baseline(self.conn))
        challenger = a_challenger(self.conn)
        self.assertTrue(challenger.experimental_basis)
        self.assertTrue(any("EXPERIMENTAL BASIS" in note
                            for note in challenger.notes))


# ======================================================================
# Versioning and immutability
# ======================================================================

class TestVersioningAndImmutability(unittest.TestCase):

    def setUp(self):
        self.conn = a_database()
        self.challenger = a_challenger(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_proposed_challenger_may_still_be_edited(self):
        self.challenger.plan.min_effect = 0.05
        registry.save(self.conn, self.challenger)
        reloaded = registry.load(self.conn, self.challenger.challenger_id)
        self.assertEqual(reloaded.plan.min_effect, 0.05)

    def test_a_started_challenger_refuses_a_changed_definition(self):
        """§9, §10: the most important refusal in this phase."""
        registry.set_status(self.conn, self.challenger.challenger_id,
                            self.challenger.version,
                            ChallengerStatus.VALIDATING)
        # A VALID relaxation: 0.005 passes `plan.validate()`, so the
        # only thing that can refuse it is the immutability guard. Using
        # 0.0 would be caught earlier by the validator and the test
        # would pass without exercising what it claims to.
        self.challenger.plan.min_effect = 0.005
        with self.assertRaises(ChallengerChanged):
            registry.save(self.conn, self.challenger)

    def test_a_changed_baseline_is_refused_once_started(self):
        registry.set_status(self.conn, self.challenger.challenger_id,
                            self.challenger.version,
                            ChallengerStatus.VALIDATING)
        self.challenger.baseline.version = "moved"
        with self.assertRaises(ChallengerChanged):
            registry.save(self.conn, self.challenger)

    def test_a_new_version_is_the_supported_path(self):
        registry.set_status(self.conn, self.challenger.challenger_id,
                            self.challenger.version,
                            ChallengerStatus.VALIDATING)
        successor = registry.new_version(
            self.conn, self.challenger,
            reason="relaxing the bar after seeing nothing clear it")
        self.assertEqual(successor.version, 2)
        self.assertEqual(successor.status, ChallengerStatus.PROPOSED)
        self.assertTrue(any("supersedes" in note for note in successor.notes))

    def test_the_previous_version_survives(self):
        registry.new_version(self.conn, self.challenger, reason="because")
        versions = api.versions(self.conn, self.challenger.challenger_id)
        self.assertEqual([v["version"] for v in versions], [1, 2])

    def test_a_new_version_must_say_why(self):
        with self.assertRaises(ValueError):
            registry.new_version(self.conn, self.challenger, reason="  ")

    def test_the_fingerprint_survives_a_save_and_load(self):
        reloaded = registry.load(self.conn, self.challenger.challenger_id)
        self.assertEqual(reloaded.fingerprint, self.challenger.fingerprint)


# ======================================================================
# Dataset identity and cache
# ======================================================================

class TestDatasetIdentity(unittest.TestCase):
    """§56: preserve the Phase 23.5 stale-cache fix."""

    def test_the_cutoff_is_recorded_and_inside_the_fingerprint(self):
        conn = a_database()
        challenger = a_challenger(conn)
        self.assertTrue(challenger.dataset_cutoff)
        before = challenger.fingerprint
        challenger.dataset_cutoff = "2030-01-01T00:00:00+00:00"
        self.assertNotEqual(before, challenger.fingerprint)
        conn.close()

    def test_an_identical_definition_reuses_a_result_and_says_so(self):
        conn = a_database()
        challenger = a_challenger(conn)
        _run_one, first = evaluation.evaluate(conn, challenger)
        run_two, second = evaluation.evaluate(conn, challenger)
        self.assertTrue(run_two["cache_hit"])
        self.assertTrue(any("CACHED RESULT" in text
                            for text in second.limitations))
        conn.close()

    def test_a_grown_record_is_a_different_challenger(self):
        """
        The Phase 23.5 defect, closed by construction here: the cutoff
        is inside the fingerprint, so a longer record cannot reach the
        cache at all.
        """
        conn = a_database(count=300)
        first = a_challenger(conn)
        columns = [row[1] for row in conn.execute(
            "PRAGMA table_info(trading_experiences)")]
        for i in range(300, 360):
            record = {"experience_id": "exp-%04d" % i, "memory_version": "v1",
                      "kind": "signal", "subject_kind": "signal",
                      "subject_id": "s%d" % i, "horizon": "3d",
                      "quality": "validated",
                      "experience_class": "correct_call",
                      "expected_direction": "long", "direction_result": "hit",
                      "actual_return": 0.03, "signal_strength": 0.8,
                      "signal_confidence": 0.7, "event_type": "acquisition",
                      "asset_class": "equity", "instrument_id": "INST01",
                      "created_at": (BASE + timedelta(days=i)).isoformat(),
                      "available_at": (BASE + timedelta(days=i)).isoformat()}
            usable = {k: v for k, v in record.items() if k in columns}
            conn.execute("INSERT INTO trading_experiences (%s) VALUES (%s)"
                         % (", ".join(usable), ", ".join("?" * len(usable))),
                         tuple(usable.values()))
        conn.commit()
        second = registry.from_candidate(conn, "cand-test-1")
        self.assertNotEqual(second.fingerprint, first.fingerprint)
        conn.close()


# ======================================================================
# Evaluation
# ======================================================================

class TestEvaluation(unittest.TestCase):

    def setUp(self):
        self.conn = a_database()
        self.challenger = a_challenger(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_an_evaluation_produces_a_result_with_reasons(self):
        run, result = evaluation.evaluate(self.conn, self.challenger)
        self.assertEqual(run["status"], "completed")
        self.assertTrue(result.reasons)

    def test_the_run_records_the_fingerprint_it_executed(self):
        run, _result = evaluation.evaluate(self.conn, self.challenger)
        self.assertEqual(run["fingerprint"], self.challenger.fingerprint)

    def test_an_evaluation_of_a_moved_definition_is_refused(self):
        self.challenger.plan.min_effect = 0.99
        with self.assertRaises(evaluation.EvaluationRefused):
            evaluation.evaluate(self.conn, self.challenger)

    def test_contexts_are_measured_and_kept(self):
        """§39: slices are preserved, never averaged."""
        _run, result = evaluation.evaluate(self.conn, self.challenger)
        self.assertTrue(result.slices)
        for piece in result.slices:
            self.assertTrue(piece.kind)

    def test_out_of_sample_is_separate_from_in_sample(self):
        _run, result = evaluation.evaluate(self.conn, self.challenger)
        self.assertIsNotNone(result.effect)
        self.assertIsNotNone(result.effect_in_sample)

    def test_a_categorical_change_reports_sensitivity_as_not_applicable(self):
        """
        A cohort keyed on `event_type` has no neighbouring values. A
        fabricated sweep would produce a plateau that means nothing.
        """
        _run, result = evaluation.evaluate(self.conn, self.challenger)
        self.assertEqual(result.sensitivity.get("shape"), "not_applicable")

    def test_the_evaluation_records_its_window_in_the_snooping_ledger(self):
        from src.autoresearch import governance
        evaluation.evaluate(self.conn, self.challenger)
        self.assertTrue(governance.snooping_report(self.conn))

    def test_a_protected_window_blocks_the_evaluation(self):
        """§14, §21."""
        from src.autoresearch import governance
        governance.declare_window(self.conn, label="final",
                                  starts_at="2020-01-01", ends_at="2099-01-01")
        run, result = evaluation.evaluate(self.conn, self.challenger,
                                          allow_cache=False)
        self.assertIsNone(result)
        self.assertIn("protected", run["error"].lower())


# ======================================================================
# The decision
# ======================================================================

class TestDecision(unittest.TestCase):

    def setUp(self):
        self.plan = EvaluationPlan()

    def _decide(self, **overrides):
        result = a_result(**overrides)
        result.scorecard = build_scorecard(result, self.plan)
        return decide(result, self.plan)

    def test_a_clean_win_on_every_dimension_is_superior(self):
        verdict, reasons = self._decide()
        self.assertEqual(verdict, ChallengerDecision.SUPERIOR)
        self.assertTrue(any("not an approval to deploy" in r for r in reasons))

    def test_a_small_sample_is_inconclusive_not_inferior(self):
        verdict, _reasons = self._decide(
            challenger_out_of_sample={"sample_size": 5})
        self.assertEqual(verdict, ChallengerDecision.INCONCLUSIVE)

    def test_a_negative_effect_with_a_clear_interval_is_inferior(self):
        verdict, _r = self._decide(effect=-0.05, effect_low=-0.09,
                                   effect_high=-0.01)
        self.assertEqual(verdict, ChallengerDecision.INFERIOR)

    def test_a_negative_effect_whose_interval_spans_zero_is_inconclusive(self):
        verdict, _r = self._decide(effect=-0.01, effect_low=-0.09,
                                   effect_high=0.05)
        self.assertEqual(verdict, ChallengerDecision.INCONCLUSIVE)

    def test_an_interval_spanning_zero_blocks_superior(self):
        verdict, _r = self._decide(effect=0.08, effect_low=-0.01,
                                   effect_high=0.16)
        self.assertEqual(verdict, ChallengerDecision.INCONCLUSIVE)

    def test_a_positive_effect_below_the_bar_is_inconclusive(self):
        verdict, _r = self._decide(effect=0.005, effect_low=0.001,
                                   effect_high=0.01)
        self.assertEqual(verdict, ChallengerDecision.INCONCLUSIVE)

    def test_disagreeing_contexts_produce_context_dependent(self):
        """§39: the finding survives instead of being averaged away."""
        slices = [SliceResult("period", "a", 0.5, 0.7, 100),
                  SliceResult("period", "b", 0.5, 0.3, 100),
                  SliceResult("period", "c", 0.5, 0.7, 100),
                  SliceResult("period", "d", 0.5, 0.3, 100)]
        verdict, reasons = self._decide(slices=slices)
        self.assertEqual(verdict, ChallengerDecision.CONTEXT_DEPENDENT)
        self.assertTrue(any("context-dependent" in r for r in reasons))

    def test_an_unmeasured_dimension_blocks_superior(self):
        """
        THE REGRESSION TEST FOR A REAL BUG IN THIS PHASE.

        The first `decide` counted only dimensions reading "worse", so
        a challenger reached SUPERIOR while its stability had never
        been measured — and the reasons printed "met: stability —
        walk-forward could not be run on this record".
        """
        verdict, reasons = self._decide(walk_forward_folds=0,
                                        walk_forward_folds_favourable=0)
        self.assertEqual(verdict, ChallengerDecision.REQUIRES_REVIEW)
        self.assertTrue(any("could not be measured" in r for r in reasons))

    def test_excessive_complexity_forces_review(self):
        """§19: a tiny gain from huge complexity may be rejected."""
        verdict, _r = self._decide(complexity_ratio=9.0)
        self.assertEqual(verdict, ChallengerDecision.REQUIRES_REVIEW)

    def test_an_economically_meaningless_win_forces_review(self):
        verdict, _r = self._decide(economically_significant=False,
                                   economic_note="too small to matter")
        self.assertEqual(verdict, ChallengerDecision.REQUIRES_REVIEW)

    def test_every_decision_carries_reasons(self):
        for overrides in ({}, {"effect": -0.05, "effect_low": -0.09,
                               "effect_high": -0.01},
                          {"challenger_out_of_sample": {"sample_size": 5}}):
            _verdict, reasons = self._decide(**overrides)
            self.assertTrue(reasons)

    def test_a_result_without_reasons_is_refused(self):
        with self.assertRaises(ValueError):
            ChallengerResult(run_id="r", challenger_id="c",
                             challenger_version=1, reasons=[]).validate()


class TestScorecard(unittest.TestCase):

    def test_the_scorecard_has_no_total_and_cannot_be_sorted(self):
        """
        §37: a sortable score gets sorted, and the top of a list of a
        hundred challengers is where the noise collects.
        """
        self.assertFalse(hasattr(Scorecard, "overall"))
        self.assertFalse(hasattr(Scorecard, "total"))
        card = build_scorecard(a_result(), EvaluationPlan())
        with self.assertRaises(TypeError):
            sorted([card, card])

    def test_it_reports_six_named_dimensions(self):
        card = build_scorecard(a_result(), EvaluationPlan())
        self.assertEqual([d.name for d in card.dimensions()],
                         ["performance", "risk", "robustness", "stability",
                          "complexity", "evidence"])

    def test_unmeasured_dimensions_are_reported_separately(self):
        card = build_scorecard(a_result(walk_forward_folds=0), EvaluationPlan())
        self.assertIn("stability", card.unknown())
        self.assertNotIn("stability", card.failing())

    def test_evidence_accounts_for_the_family_count(self):
        card = build_scorecard(a_result(family_challenger_count=9),
                               EvaluationPlan())
        self.assertNotEqual(card.evidence.verdict, "better")


# ======================================================================
# Queue, concurrency, recovery, cancellation
# ======================================================================

class TestQueue(unittest.TestCase):

    def setUp(self):
        self.conn = a_database()
        self.challenger = a_challenger(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_only_one_worker_can_claim_an_item(self):
        """§53: no duplicate challenger runs."""
        queue_id = workflow.enqueue(self.conn, self.challenger.challenger_id,
                                    self.challenger.version)
        self.assertTrue(workflow.claim(self.conn, queue_id))
        self.assertFalse(workflow.claim(self.conn, queue_id))

    def test_a_claimed_item_leaves_the_selectable_pool(self):
        queue_id = workflow.enqueue(self.conn, self.challenger.challenger_id,
                                    self.challenger.version)
        workflow.claim(self.conn, queue_id)
        self.assertEqual(workflow.next_batch(self.conn), [])

    def test_an_abandoned_item_is_reclaimed_with_a_reason(self):
        """§55: recovery after a worker crash."""
        queue_id = workflow.enqueue(self.conn, self.challenger.challenger_id,
                                    self.challenger.version)
        workflow.claim(self.conn, queue_id)
        self.conn.execute(
            "UPDATE challenger_queue SET started_at = ? WHERE queue_id = ?",
            ("2020-01-01T00:00:00+00:00", queue_id))
        self.conn.commit()
        reclaimed = workflow.reclaim_stale(self.conn)
        self.assertEqual(len(reclaimed), 1)
        row = workflow.queue_listing(self.conn)[0]
        self.assertEqual(row["state"], "queued")
        self.assertIn("reclaimed", row["reason"])

    def test_a_live_item_is_not_reclaimed(self):
        queue_id = workflow.enqueue(self.conn, self.challenger.challenger_id,
                                    self.challenger.version)
        workflow.claim(self.conn, queue_id)
        self.assertEqual(workflow.reclaim_stale(self.conn), [])

    def test_a_cancelled_run_is_never_completed(self):
        """§54."""
        queue_id = workflow.enqueue(self.conn, self.challenger.challenger_id,
                                    self.challenger.version)
        workflow.cancel(self.conn, queue_id, "stopped by hand")
        depth = workflow.depth(self.conn)
        self.assertEqual(depth["cancelled"], 1)
        self.assertEqual(depth["completed"], 0)

    def test_the_run_limit_refuses_rather_than_repeats(self):
        """§51: re-running one challenger is how a winner is manufactured."""
        workflow.enqueue(self.conn, self.challenger.challenger_id,
                         self.challenger.version)
        report = workflow.run_queued(
            self.conn, limits=ChallengerLimits(max_runs_per_challenger=0))
        self.assertEqual(report["evaluated"], [])

    def test_the_family_budget_refuses_challenger_explosion(self):
        """§50."""
        with self.assertRaises(LimitExceeded):
            registry.assert_family_budget(
                self.conn, self.challenger.family_id,
                limits=ChallengerLimits(max_challengers_per_family=1))

    def test_a_worker_pass_records_why_it_stopped(self):
        report = workflow.run_queued(self.conn)
        self.assertTrue(report["termination_reason"])


# ======================================================================
# Review — the only exit
# ======================================================================

class TestReview(unittest.TestCase):

    def setUp(self):
        self.conn = a_database()
        self.challenger = a_challenger(self.conn)
        workflow.enqueue(self.conn, self.challenger.challenger_id,
                         self.challenger.version)
        workflow.run_queued(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_review_requires_a_named_reviewer_and_a_reason(self):
        """An approval nobody signed is not an approval."""
        for reviewer, reason in (("", "because"), ("someone", "  ")):
            with self.assertRaises(workflow.ReviewRefused):
                workflow.review(self.conn, self.challenger.challenger_id,
                                self.challenger.version,
                                outcome=ReviewOutcome.APPROVED_FOR_PAPER,
                                reviewer=reviewer, reason=reason)

    def test_approval_reaches_paper_candidate_and_no_further(self):
        result = workflow.review(
            self.conn, self.challenger.challenger_id, self.challenger.version,
            outcome=ReviewOutcome.APPROVED_FOR_PAPER,
            reviewer="a.person", reason="worth paper trading")
        self.assertEqual(result["status"], "paper_candidate")
        self.assertIn("executed", result["note"])

    def test_an_unevaluated_challenger_cannot_be_approved(self):
        other = registry.from_candidate(
            self.conn, a_candidate(self.conn, "cand-2"))
        registry.save(self.conn, other)
        with self.assertRaises(workflow.ReviewRefused):
            workflow.review(self.conn, other.challenger_id, other.version,
                            outcome=ReviewOutcome.APPROVED_FOR_PAPER,
                            reviewer="a.person", reason="looks good")

    def test_reviews_are_append_only(self):
        for outcome in (ReviewOutcome.DEFERRED, ReviewOutcome.REJECTED):
            workflow.review(self.conn, self.challenger.challenger_id,
                            self.challenger.version, outcome=outcome,
                            reviewer="a.person", reason="changing my mind")
        self.assertEqual(
            len(workflow.reviews(self.conn, self.challenger.challenger_id)), 2)

    def test_a_rejected_challenger_stays_listed(self):
        """§42: negative knowledge is preserved."""
        workflow.review(self.conn, self.challenger.challenger_id,
                        self.challenger.version,
                        outcome=ReviewOutcome.REJECTED,
                        reviewer="a.person", reason="not convinced")
        rows = api.challengers(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
