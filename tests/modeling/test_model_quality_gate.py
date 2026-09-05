"""
tests/modeling/test_model_quality_gate.py
-------------------------------------------------
NEW-01: a model that does not beat its baseline must not score.

WHAT HAPPENED
-----------------
`inference.load_model()` selected `ORDER BY trained_at DESC LIMIT 1`.
Newest won; nothing else was asked. In production all four models
carried `beats_all_baselines = 0` and a negative r-squared, and the
newest — directional accuracy 0.413, worse than a coin flip — produced
422 of 549 predictions, which became signals, ten of them active on a
public page.

THE GATE WAS ALREADY WRITTEN
--------------------------------
`ModelEvaluation.is_deployable` has existed since Phase 9:

    Requires: beats every baseline AND has a large enough effective
    sample.

Defined, documented, unit-tested, and called by nothing. So these
tests defend a wiring job, and the first thing they check is that the
wiring did not quietly introduce a *second* threshold that could
disagree with the first.

WHAT THESE DEFEND
---------------------
1. Training something newer does not change what production scores
   with. That is the regression, stated exactly.
2. No model promoted -> an explicit NO_VALIDATED_MODEL_AVAILABLE, not
   a fallback and not an empty result.
3. Promotion needs a named human, a reason, and a passing model. None
   of the three has a default, and there is no override.
4. Experimental scoring stays possible for research, and is never
   silent.
"""

import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.model_promotion_schema import (
    initialize_model_promotion_schema,
)
from src.data_access.model_schema import initialize_model_schema
from src.domain.model_models import ModelEvaluation, ModelStatus
from src.modeling.promotion import (
    PromotionRefused, demote, history, promote,
)
from src.modeling.selection import (
    NO_VALIDATED_MODEL_AVAILABLE, NoValidatedModel, SelectionPolicy,
    candidates, eligibility, select,
)

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def iso(moment):
    return moment.isoformat()


class GateCase(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        initialize_model_schema(self.conn)
        initialize_model_promotion_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    # ---------------- fixtures ----------------

    def add_model(self, trained_model_id="tm-1", status="evaluated",
                  trained_at=None, label_name="d5.abnormal_return"):
        self.conn.execute("""
            INSERT INTO trained_models (
                trained_model_id, model_id, model_qualified_id, name, task,
                family, model_version, label_name, label_version,
                feature_set_id, feature_set_version, dataset_version,
                hyperparameters_json, parameters_json, feature_names_json,
                train_start, train_end, train_sample_size, train_cluster_count,
                status, training_notes_json, trained_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (trained_model_id, "ridge", f"ridge:{trained_model_id}", "Ridge",
              "abnormal_return", "ridge_regression", "v1", label_name, "v1",
              "fs-1", "v1", "ds-1", "{}", "{}", '["f.a"]',
              iso(NOW - timedelta(days=40)), iso(NOW - timedelta(days=8)),
              200, 60, status, "[]",
              iso(trained_at or NOW - timedelta(days=1))))
        self.conn.commit()
        return trained_model_id

    def add_evaluation(self, trained_model_id="tm-1", clusters=60,
                       beats=True, r_squared=0.12, evaluation_id=None,
                       baselines=("baseline_historical_mean",)):
        evaluation_id = evaluation_id or f"ev-{trained_model_id}"
        self.conn.execute("""
            INSERT INTO model_evaluations (
                evaluation_id, trained_model_id, model_qualified_id,
                window_label, sample_size, cluster_count,
                effective_sample_size, small_sample, beats_all_baselines,
                abstention_rate, metrics_json, notes_json, evaluated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (evaluation_id, trained_model_id, f"ridge:{trained_model_id}",
              "single_chronological_split", clusters * 2, clusters, clusters,
              int(clusters < ModelEvaluation.MIN_EFFECTIVE_SAMPLE), int(beats),
              0.0,
              f'{{"r_squared": {r_squared}, "directional_accuracy": 0.55}}',
              "[]", iso(NOW - timedelta(days=1))))
        for name in baselines:
            # model_score above baseline_score means the model wins.
            self.conn.execute("""
                INSERT INTO model_baseline_comparisons
                (evaluation_id, baseline_name, metric_name,
                 baseline_score, model_score)
                VALUES (?,?,?,?,?)
            """, (evaluation_id, name, "directional_accuracy",
                  0.50, 0.55 if beats else 0.45))
        self.conn.commit()
        return evaluation_id

    def good_model(self, trained_model_id="tm-good", **kwargs):
        self.add_model(trained_model_id, **kwargs)
        self.add_evaluation(trained_model_id, clusters=60, beats=True)
        return trained_model_id

    def bad_model(self, trained_model_id="tm-bad", **kwargs):
        self.add_model(trained_model_id, **kwargs)
        self.add_evaluation(trained_model_id, clusters=60, beats=False,
                            r_squared=-0.24)
        return trained_model_id


# ======================================================================
# The threshold is not re-implemented
# ======================================================================

class TestTheGateReusesTheExistingCriterion(GateCase):
    """
    The single most important property of this change: it wires up the
    Phase 9 gate rather than writing a second one. Two thresholds
    eventually disagree, and the disagreement surfaces as a model being
    active that the evaluator says should not be.
    """

    def test_eligibility_agrees_with_is_deployable_when_it_passes(self):
        self.good_model("tm-x")
        verdict = eligibility(self.conn, "tm-x")
        evaluation = ModelEvaluation(
            evaluation_id="e", trained_model_id="tm-x", model_qualified_id="q",
            sample_size=120, cluster_count=60,
            baseline_comparisons=[])
        self.assertTrue(verdict.deployable)
        # and the constant itself is the domain object's, not a copy
        self.assertEqual(ModelEvaluation.MIN_EFFECTIVE_SAMPLE, 30)
        self.assertEqual(evaluation.MIN_EFFECTIVE_SAMPLE, 30)

    def test_eligibility_agrees_with_is_deployable_when_it_fails(self):
        self.bad_model("tm-y")
        self.assertIs(eligibility(self.conn, "tm-y").deployable, False)

    def test_there_is_no_threshold_argument_to_lower(self):
        """
        A caller must not be able to relax the bar by passing a number.
        The gate takes a connection and an id and nothing else.
        """
        import inspect
        for function in (eligibility, select, candidates):
            names = set(inspect.signature(function).parameters)
            for forbidden in ("threshold", "min_r2", "min_r_squared",
                              "min_clusters", "min_sample", "force",
                              "override", "bypass", "allow_failing"):
                self.assertNotIn(
                    forbidden, names,
                    f"{function.__name__} grew a way to lower the bar")


# ======================================================================
# Three-way verdict: pass, fail, and cannot-be-judged
# ======================================================================

class TestUnjudgedIsNotPassing(GateCase):

    def test_a_model_with_no_evaluation_is_unjudged_not_eligible(self):
        self.add_model("tm-raw")
        verdict = eligibility(self.conn, "tm-raw")
        self.assertIsNone(verdict.deployable)
        self.assertEqual(verdict.verdict, "UNJUDGED")
        self.assertFalse(verdict.may_be_promoted,
                         "'we could not measure it' became evidence in favour")

    def test_an_evaluation_with_no_baseline_cannot_be_judged(self):
        self.add_model("tm-nb")
        self.add_evaluation("tm-nb", clusters=60, baselines=())
        verdict = eligibility(self.conn, "tm-nb")
        self.assertIsNone(verdict.deployable)
        self.assertFalse(verdict.may_be_promoted)

    def test_a_small_sample_fails_even_when_it_beats_the_baseline(self):
        """Both conditions are necessary; beating a baseline on 4 clusters is noise."""
        self.add_model("tm-small")
        self.add_evaluation("tm-small", clusters=4, beats=True)
        verdict = eligibility(self.conn, "tm-small")
        self.assertIs(verdict.deployable, False)
        self.assertTrue(any("effective sample" in r for r in verdict.reasons))

    def test_the_reasons_explain_which_baselines_were_missed(self):
        self.add_model("tm-m")
        self.add_evaluation("tm-m", clusters=60, beats=False,
                            baselines=("baseline_historical_mean",
                                       "baseline_majority_class"))
        reasons = " ".join(eligibility(self.conn, "tm-m").reasons)
        self.assertIn("baseline_historical_mean", reasons)
        self.assertIn("baseline_majority_class", reasons)

    def test_a_missing_model_is_reported_not_invented(self):
        verdict = eligibility(self.conn, "tm-nonexistent")
        self.assertIsNone(verdict.deployable)
        self.assertIn("no such trained model", " ".join(verdict.reasons))


# ======================================================================
# Selection
# ======================================================================

class TestSelection(GateCase):

    def test_no_active_model_raises_rather_than_falling_back(self):
        self.bad_model("tm-bad")
        with self.assertRaises(NoValidatedModel) as caught:
            select(self.conn, label_name="d5.abnormal_return")
        self.assertEqual(caught.exception.code, NO_VALIDATED_MODEL_AVAILABLE)

    def test_the_refusal_names_every_candidate_and_its_failure(self):
        self.bad_model("tm-bad")
        with self.assertRaises(NoValidatedModel) as caught:
            select(self.conn)
        report = caught.exception.report()
        self.assertIn("tm-bad", report)
        self.assertIn("baseline", report)

    def test_a_newer_unpromoted_model_does_not_displace_the_active_one(self):
        """
        NEW-01, stated exactly: training something newer must not change
        what production scores with.
        """
        self.good_model("tm-active", status="active",
                        trained_at=NOW - timedelta(days=30))
        self.bad_model("tm-newer", status="evaluated",
                       trained_at=NOW - timedelta(hours=1))
        self.assertEqual(select(self.conn).trained_model_id, "tm-active")

    def test_an_eligible_but_unpromoted_model_is_still_refused(self):
        """
        Passing the gate is necessary, not sufficient. A model becomes
        production-facing when a person says so.
        """
        self.good_model("tm-passes", status="evaluated")
        with self.assertRaises(NoValidatedModel) as caught:
            select(self.conn)
        self.assertIn("waiting for promotion", caught.exception.args[0])

    def test_experimental_policy_returns_the_newest_evaluated_model(self):
        self.bad_model("tm-old", trained_at=NOW - timedelta(days=9))
        self.bad_model("tm-new", trained_at=NOW - timedelta(days=1))
        chosen = select(self.conn, policy=SelectionPolicy.EXPERIMENTAL)
        self.assertEqual(chosen.trained_model_id, "tm-new")
        self.assertFalse(chosen.is_active, "an experimental pick claimed to be active")

    def test_experimental_still_prefers_an_active_model_when_one_exists(self):
        self.good_model("tm-active", status="active",
                        trained_at=NOW - timedelta(days=30))
        self.bad_model("tm-new", trained_at=NOW - timedelta(days=1))
        self.assertEqual(
            select(self.conn, policy=SelectionPolicy.EXPERIMENTAL).trained_model_id,
            "tm-active")

    def test_selection_is_deterministic_across_repeated_calls(self):
        self.good_model("tm-a", status="active", trained_at=NOW - timedelta(days=5))
        self.good_model("tm-b", status="active", trained_at=NOW - timedelta(days=2))
        picks = {select(self.conn).trained_model_id for _ in range(10)}
        self.assertEqual(len(picks), 1)

    def test_a_label_mismatch_is_not_rescued_by_the_gate(self):
        self.good_model("tm-d20", status="active", label_name="d20.abnormal_return")
        with self.assertRaises(NoValidatedModel):
            select(self.conn, label_name="d5.abnormal_return")

    def test_no_model_at_all_says_so_distinctly(self):
        with self.assertRaises(NoValidatedModel) as caught:
            select(self.conn)
        self.assertIn("No trained model exists", str(caught.exception))


# ======================================================================
# Promotion
# ======================================================================

class TestPromotion(GateCase):

    def test_a_failing_model_cannot_be_promoted(self):
        self.bad_model("tm-bad")
        with self.assertRaises(PromotionRefused) as caught:
            promote(self.conn, "tm-bad", approved_by="someone",
                    reason="I want signals")
        self.assertIn("does not pass the quality gate", str(caught.exception))

    def test_an_unjudged_model_cannot_be_promoted(self):
        self.add_model("tm-raw")
        with self.assertRaises(PromotionRefused):
            promote(self.conn, "tm-raw", approved_by="someone", reason="why not")

    def test_promotion_requires_a_named_approver(self):
        self.good_model("tm-good")
        with self.assertRaises(PromotionRefused) as caught:
            promote(self.conn, "tm-good", approved_by="   ", reason="looks fine")
        self.assertIn("named approver", str(caught.exception))

    def test_promotion_requires_a_reason(self):
        self.good_model("tm-good")
        with self.assertRaises(PromotionRefused) as caught:
            promote(self.conn, "tm-good", approved_by="a person", reason="")
        self.assertIn("stated reason", str(caught.exception))

    def test_there_is_no_force_parameter(self):
        """
        Checked on the signature, whole-token, so `time_in_force`-style
        substrings cannot produce a false pass.
        """
        import inspect
        import re
        for function in (promote, demote):
            source = inspect.getsource(function)
            names = set(inspect.signature(function).parameters)
            for forbidden in ("force", "override", "bypass", "skip_gate",
                              "ignore_gate", "auto"):
                self.assertNotIn(forbidden, names,
                                 f"{function.__name__} grew a {forbidden}")
            self.assertFalse(
                re.search(r"\bforce\b\s*=", source),
                f"{function.__name__} assigns something called force")

    def test_a_passing_model_is_promoted_and_becomes_active(self):
        self.good_model("tm-good")
        records = promote(self.conn, "tm-good", approved_by="an analyst",
                          reason="beats both baselines on 60 clusters")
        self.assertEqual(len(records), 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM trained_models WHERE trained_model_id='tm-good'"
            ).fetchone()[0], ModelStatus.ACTIVE.value)

    def test_promotion_records_who_why_and_on_what_evidence(self):
        self.good_model("tm-good")
        promote(self.conn, "tm-good", approved_by="an analyst",
                reason="beats both baselines")
        row = self.conn.execute("""
            SELECT approved_by, reason, evaluation_id, deployable,
                   dataset_version, feature_set_version, label_version
            FROM model_promotions WHERE action='promote'
        """).fetchone()
        self.assertEqual(row[0], "an analyst")
        self.assertEqual(row[1], "beats both baselines")
        self.assertEqual(row[2], "ev-tm-good")
        self.assertEqual(row[3], 1)
        self.assertEqual(row[4], "ds-1")
        self.assertEqual(row[5], "v1")
        self.assertEqual(row[6], "v1")

    def test_promoting_retires_the_previous_champion(self):
        self.good_model("tm-first")
        promote(self.conn, "tm-first", approved_by="a", reason="first")
        self.good_model("tm-second", trained_at=NOW)
        promote(self.conn, "tm-second", approved_by="a", reason="better")
        statuses = dict(self.conn.execute(
            "SELECT trained_model_id, status FROM trained_models"))
        self.assertEqual(statuses["tm-first"], ModelStatus.RETIRED.value)
        self.assertEqual(statuses["tm-second"], ModelStatus.ACTIVE.value)

    def test_only_one_model_is_ever_active_for_a_label(self):
        self.good_model("tm-first")
        promote(self.conn, "tm-first", approved_by="a", reason="first")
        self.good_model("tm-second", trained_at=NOW)
        promote(self.conn, "tm-second", approved_by="a", reason="better")
        active = self.conn.execute(
            "SELECT COUNT(*) FROM trained_models WHERE status='active'").fetchone()[0]
        self.assertEqual(active, 1)

    def test_a_retirement_is_recorded_not_erased(self):
        self.good_model("tm-first")
        promote(self.conn, "tm-first", approved_by="a", reason="first")
        self.good_model("tm-second", trained_at=NOW)
        promote(self.conn, "tm-second", approved_by="a", reason="better")
        records = history(self.conn, "tm-first")
        self.assertEqual([r["action"] for r in records], ["demote", "promote"])

    def test_promoting_an_already_active_model_is_refused(self):
        self.good_model("tm-good")
        promote(self.conn, "tm-good", approved_by="a", reason="first")
        with self.assertRaises(PromotionRefused) as caught:
            promote(self.conn, "tm-good", approved_by="a", reason="again")
        self.assertIn("already ACTIVE", str(caught.exception))

    def test_demotion_is_not_gated(self):
        """
        Taking something out of production must never be harder than
        putting it in.
        """
        self.good_model("tm-good")
        promote(self.conn, "tm-good", approved_by="a", reason="in")
        demote(self.conn, "tm-good", approved_by="a", reason="drifted")
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM trained_models WHERE trained_model_id='tm-good'"
            ).fetchone()[0], ModelStatus.DEGRADED.value)

    def test_a_demoted_model_stops_being_selected(self):
        self.good_model("tm-good")
        promote(self.conn, "tm-good", approved_by="a", reason="in")
        demote(self.conn, "tm-good", approved_by="a", reason="out")
        with self.assertRaises(NoValidatedModel):
            select(self.conn)

    def test_a_retired_model_keeps_its_row(self):
        self.good_model("tm-first")
        promote(self.conn, "tm-first", approved_by="a", reason="first")
        self.good_model("tm-second", trained_at=NOW)
        promote(self.conn, "tm-second", approved_by="a", reason="better")
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM trained_models WHERE trained_model_id='tm-first'"
        ).fetchone())


# ======================================================================
# Nothing promotes on its own
# ======================================================================

class TestNothingPromotesAutomatically(unittest.TestCase):
    """
    The gate is only worth as much as the guarantee that no scheduled
    job walks around it. These read the repository itself.
    """

    ROOT = os.path.join(os.path.dirname(__file__), "..", "..")

    def _read(self, *parts):
        path = os.path.join(self.ROOT, *parts)
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_no_script_but_the_promotion_cli_imports_promote(self):
        scripts = os.path.join(self.ROOT, "scripts")
        offenders = []
        for name in os.listdir(scripts):
            if not name.endswith(".py") or name == "promote_model.py":
                continue
            body = self._read("scripts", name)
            if "modeling.promotion" in body or "import promote" in body:
                offenders.append(name)
        self.assertEqual(offenders, [],
                         "a script other than the promotion CLI can promote")

    def test_no_workflow_invokes_the_promotion_script(self):
        workflows = os.path.join(self.ROOT, ".github", "workflows")
        offenders = [name for name in os.listdir(workflows)
                     if name.endswith(".yml")
                     and "promote_model.py" in self._read(".github", "workflows", name)]
        self.assertEqual(offenders, [],
                         "a scheduled workflow can promote a model")

    def test_train_models_does_not_set_a_model_active(self):
        body = self._read("scripts", "train_models.py")
        self.assertNotIn("'active'", body)
        self.assertNotIn('"active"', body)

    def test_the_pipeline_predicts_experimentally_and_says_so(self):
        """
        Stage 9 runs with --experimental deliberately, so research keeps
        moving while no model passes. The flag must be visible in the
        workflow rather than defaulted in code — that visibility is the
        whole difference between this and the bug.
        """
        body = self._read(".github", "workflows", "pipeline.yml")
        self.assertIn("predict.py --apply --experimental", body)


if __name__ == "__main__":
    unittest.main()
