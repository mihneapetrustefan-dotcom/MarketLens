"""
tests/experiments/test_experiment_models.py
-----------------------------------------------------
The definition layer: hypotheses, arms, criteria, fingerprints.

Covers §82 items 1-12 and the §83 adversarial cases that live in the
domain rather than the engine.

THE PROPERTY EVERYTHING ELSE DEPENDS ON
-------------------------------------------
The acceptance criteria are inside the fingerprint, and the fingerprint
survives storage. If either half fails, §46 fails silently: either the
criteria could be relaxed after the answer is visible, or every stored
experiment would be refused as "changed" and nobody could run anything.

Both halves broke during development. The criteria were in the
fingerprint from the start, but `ArmSpec.description` was in it too --
prose with no column behind it, so a saved experiment reloaded with an
empty description and recomputed to a different fingerprint. Every
proposal was unrunnable. `test_fingerprint_survives_a_save_and_load`
is the regression test for that, and it is the reason `identity()`
exists separately from `as_dict()`.
"""

import json
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.experiment_schema import initialize_experiment_schema
from src.domain.experiment_models import (
    EXPERIMENT_METHOD_VERSION, AcceptanceCriteria, ArmSpec, DatasetSnapshot,
    Decision, EvaluationProtocol, Experiment, ExperimentStatus, ExperimentType,
    Hypothesis, HypothesisSource, ResourceLimits, bootstrap_difference,
    economic_significance, multiple_testing_note,
)
from src.experiments import api, engine


def a_hypothesis(**overrides):
    fields = dict(
        statement="A 0.7 strength floor improves directional accuracy.",
        mechanism="Weak signals are dominated by noise, so removing them "
                  "should raise the hit rate of what remains.",
        expected_effect="a higher directional_accuracy than the control",
        population="all signals",
        conditions={"threshold": 0.7},
        metric="directional_accuracy",
        source=HypothesisSource.RESEARCHER)
    fields.update(overrides)
    return Hypothesis(**fields)


def an_experiment(**overrides):
    fields = dict(
        experiment_id="exp-test-0001",
        name="strength floor",
        experiment_type=ExperimentType.SIGNAL,
        hypothesis=a_hypothesis(),
        baseline=ArmSpec(name="all signals", evaluator="signal_all",
                         description="Every signal, unfiltered."),
        candidate=ArmSpec(name="strength >= 0.7",
                          evaluator="signal_strength_threshold",
                          parameters={"threshold": 0.7},
                          description="Only the strong ones.",
                          complexity=2),
        dataset=DatasetSnapshot(as_of="2026-09-01T00:00:00+00:00"),
        protocol=EvaluationProtocol(),
        criteria=AcceptanceCriteria(),
        limits=ResourceLimits())
    fields.update(overrides)
    return Experiment(**fields)


class TestHypothesis(unittest.TestCase):

    def test_a_hypothesis_requires_a_mechanism(self):
        """
        §5: a hypothesis without a proposed mechanism is a data-mining
        result wearing a hypothesis costume. The validator is the only
        thing standing between the two.
        """
        with self.assertRaises(ValueError):
            a_hypothesis(mechanism="").validate()

    def test_a_hypothesis_requires_a_statement(self):
        with self.assertRaises(ValueError):
            a_hypothesis(statement="   ").validate()

    def test_a_hypothesis_requires_a_metric(self):
        with self.assertRaises(ValueError):
            a_hypothesis(metric="").validate()

    def test_a_valid_hypothesis_passes(self):
        a_hypothesis().validate()

    def test_the_source_is_recorded_not_inferred(self):
        mined = a_hypothesis(source=HypothesisSource.MEMORY_PATTERN)
        self.assertEqual(mined.as_dict()["source"], "memory_pattern")


class TestFingerprint(unittest.TestCase):

    def test_the_criteria_are_inside_the_fingerprint(self):
        """
        §46 is structural, not a convention. Relaxing `min_effect`
        after seeing the answer must produce a different experiment.
        """
        before = an_experiment().fingerprint
        relaxed = an_experiment(criteria=AcceptanceCriteria(min_effect=0.0))
        self.assertNotEqual(before, relaxed.fingerprint)

    def test_changing_a_parameter_changes_the_fingerprint(self):
        other = an_experiment()
        other.candidate = ArmSpec(name="strength >= 0.5",
                                  evaluator="signal_strength_threshold",
                                  parameters={"threshold": 0.5}, complexity=2)
        self.assertNotEqual(an_experiment().fingerprint, other.fingerprint)

    def test_changing_the_evaluator_changes_the_fingerprint(self):
        other = an_experiment()
        other.candidate = ArmSpec(name="strength >= 0.7",
                                  evaluator="signal_confidence_threshold",
                                  parameters={"threshold": 0.7}, complexity=2)
        self.assertNotEqual(an_experiment().fingerprint, other.fingerprint)

    def test_rewording_prose_does_not_change_the_fingerprint(self):
        """
        The fingerprint exists to catch a changed threshold, evaluator
        or criterion -- not a reworded comment. If prose counted, a
        typo fix would refuse to run a started experiment, and worse,
        prose has no column so a stored experiment would never match
        itself again.
        """
        other = an_experiment()
        other.baseline = ArmSpec(name="all signals", evaluator="signal_all",
                                 description="A completely different sentence.")
        self.assertEqual(an_experiment().fingerprint, other.fingerprint)

    def test_the_fingerprint_is_stable_across_identical_definitions(self):
        self.assertEqual(an_experiment().fingerprint, an_experiment().fingerprint)

    def test_identity_excludes_description_and_as_dict_keeps_it(self):
        arm = ArmSpec(name="x", evaluator="signal_all", description="prose")
        self.assertNotIn("description", arm.identity())
        self.assertEqual(arm.as_dict()["description"], "prose")


class TestChangedVariables(unittest.TestCase):

    def test_changed_variables_are_computed_not_declared(self):
        """
        §7, §8: an experiment that claims to change one thing while
        changing three is the most common way a result becomes
        uninterpretable. The list is derived from the two arms so it
        cannot disagree with them.
        """
        changed = an_experiment().changed_variables
        self.assertIn("evaluator", changed)
        self.assertIn("threshold", changed)

    def test_identical_arms_change_nothing(self):
        same = ArmSpec(name="a", evaluator="signal_all")
        exp = an_experiment(baseline=same, candidate=same)
        self.assertEqual(exp.changed_variables, [])


class TestStatistics(unittest.TestCase):

    def test_bootstrap_interval_of_identical_samples_includes_zero(self):
        values = [0.0, 1.0] * 60
        low, high, method = bootstrap_difference(values, list(values), seed=7)
        self.assertLessEqual(low, 0.0)
        self.assertGreaterEqual(high, 0.0)

    def test_bootstrap_interval_is_deterministic_for_a_seed(self):
        a = [1.0] * 40 + [0.0] * 60
        b = [1.0] * 60 + [0.0] * 40
        self.assertEqual(bootstrap_difference(a, b, seed=11),
                         bootstrap_difference(a, b, seed=11))

    def test_a_clear_difference_produces_an_interval_above_zero(self):
        a = [0.0] * 200
        b = [1.0] * 200
        low, _, method = bootstrap_difference(a, b, seed=3)
        self.assertTrue(method)
        self.assertGreater(low, 0.0)

    def test_economic_significance_rejects_a_large_adverse_effect(self):
        """
        A -2.13% effect is large, and large in the wrong direction. The
        first version of this function reported it as clearing the
        threshold, which read as a point in the candidate's favour.
        """
        ok, note = economic_significance(-0.0213, "directional_accuracy")
        self.assertFalse(ok)
        self.assertIn("wrong direction", note.lower())

    def test_economic_significance_accepts_a_large_favourable_effect(self):
        ok, _ = economic_significance(0.05, "directional_accuracy")
        self.assertTrue(ok)

    def test_economic_significance_rejects_a_trivial_effect(self):
        ok, note = economic_significance(0.0001, "directional_accuracy")
        self.assertFalse(ok)
        self.assertTrue(note)

    def test_multiple_testing_note_scales_with_the_family(self):
        """§41, §42: the twentieth test of an idea is not the first."""
        single = multiple_testing_note(1, 1)
        many = multiple_testing_note(20, 20)
        self.assertNotEqual(single, many)
        self.assertTrue(many)


class TestPersistence(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        initialize_experiment_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_fingerprint_survives_a_save_and_load(self):
        """
        The regression test for the bug that made every proposed
        experiment unrunnable: a field inside the fingerprint had no
        column, so the reloaded definition hashed differently and
        `run()` refused it as edited.
        """
        original = an_experiment()
        engine.save_experiment(self.conn, original)
        restored = api.load(self.conn, original.experiment_id)
        self.assertIsNotNone(restored)
        self.assertEqual(original.fingerprint, restored.fingerprint)

    def test_every_fingerprinted_field_round_trips(self):
        original = an_experiment()
        engine.save_experiment(self.conn, original)
        restored = api.load(self.conn, original.experiment_id)
        for name in ("baseline", "candidate"):
            self.assertEqual(getattr(original, name).identity(),
                             getattr(restored, name).identity(), name)
        self.assertEqual(original.criteria.as_dict(), restored.criteria.as_dict())
        self.assertEqual(original.protocol.as_dict(), restored.protocol.as_dict())
        self.assertEqual(original.dataset.as_dict(), restored.dataset.as_dict())
        self.assertEqual(original.hypothesis.as_dict(), restored.hypothesis.as_dict())

    def test_arm_descriptions_are_stored_rather_than_dropped(self):
        """
        Excluded from the fingerprint, but still written: prose that
        explains why an arm is the honest control is worth keeping.
        """
        original = an_experiment()
        engine.save_experiment(self.conn, original)
        restored = api.load(self.conn, original.experiment_id)
        self.assertEqual(restored.baseline.description,
                         original.baseline.description)
        self.assertEqual(restored.candidate.description,
                         original.candidate.description)

    def test_a_started_experiment_refuses_an_edited_definition(self):
        """§32, §73."""
        original = an_experiment()
        engine.save_experiment(self.conn, original)
        engine.set_status(self.conn, original.experiment_id,
                          ExperimentStatus.RUNNING)
        edited = an_experiment(criteria=AcceptanceCriteria(min_effect=0.0))
        with self.assertRaises(engine.DefinitionChanged):
            engine.save_experiment(self.conn, edited)

    def test_a_draft_may_still_be_edited(self):
        """
        The freeze begins when the experiment starts, not when it is
        written down. A draft nobody has run carries no result that
        editing it could flatter.
        """
        original = an_experiment()
        engine.save_experiment(self.conn, original)
        edited = an_experiment(criteria=AcceptanceCriteria(min_effect=0.05))
        engine.save_experiment(self.conn, edited)
        restored = api.load(self.conn, original.experiment_id)
        self.assertEqual(restored.criteria.min_effect, 0.05)

    def test_the_schema_migrates_a_table_missing_the_newer_columns(self):
        """
        CREATE TABLE IF NOT EXISTS does nothing to an existing table,
        so a database written before the description columns existed
        would fail every insert. The migration adds them in place.
        """
        old = sqlite3.connect(":memory:")
        initialize_experiment_schema(old)
        old.execute("ALTER TABLE experiments DROP COLUMN baseline_description")
        old.execute("ALTER TABLE experiments DROP COLUMN candidate_description")
        initialize_experiment_schema(old)
        columns = {row[1] for row in old.execute("PRAGMA table_info(experiments)")}
        self.assertIn("baseline_description", columns)
        self.assertIn("candidate_description", columns)
        engine.save_experiment(old, an_experiment())
        old.close()

    def test_the_method_version_is_recorded_on_every_experiment(self):
        """§44: methodology versions coexist rather than overwrite."""
        engine.save_experiment(self.conn, an_experiment())
        stored = self.conn.execute(
            "SELECT method_version FROM experiments").fetchone()[0]
        self.assertEqual(stored, EXPERIMENT_METHOD_VERSION)


class TestStatusVocabulary(unittest.TestCase):

    def test_a_verdict_is_a_terminal_status(self):
        for status in (ExperimentStatus.PASSED, ExperimentStatus.REJECTED,
                       ExperimentStatus.INCONCLUSIVE):
            self.assertTrue(status.is_terminal, status)

    def test_a_draft_is_not_started(self):
        self.assertFalse(ExperimentStatus.DRAFT.is_started)
        self.assertFalse(ExperimentStatus.PLANNED.is_started)
        self.assertFalse(ExperimentStatus.QUEUED.is_started)

    def test_anything_past_queued_is_started(self):
        for status in (ExperimentStatus.RUNNING, ExperimentStatus.COMPLETED,
                       ExperimentStatus.PASSED, ExperimentStatus.REJECTED,
                       ExperimentStatus.INCONCLUSIVE, ExperimentStatus.FAILED,
                       ExperimentStatus.CANCELLED):
            self.assertTrue(status.is_started, status)

    def test_pass_is_a_decision_not_a_recommendation(self):
        """
        §4: nothing in the vocabulary says "deploy". PASS means the
        predefined criteria were met.
        """
        self.assertEqual({d.value for d in Decision},
                         {"pass", "fail", "inconclusive"})


if __name__ == "__main__":
    unittest.main()
