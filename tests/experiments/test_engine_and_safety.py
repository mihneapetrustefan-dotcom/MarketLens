"""
tests/experiments/test_engine_and_safety.py
-----------------------------------------------------
Running experiments, and what the engine is structurally unable to do.

Covers §82 items 13-30, all fifteen §83 adversarial cases, and the §79
and §80 safety properties.

THE TWO PROPERTIES WORTH STATING PLAINLY
--------------------------------------------
1. **The split is chronological.** A random split of financial
   observations leaks: two rows from the same day land on opposite
   sides and the held-out half already knows the answer. Every result
   this package produces would be excellent and worthless.

2. **Nothing production is touched.** The engine reads experiences and
   writes experiment tables. It cannot promote a model, change a
   threshold, alter a strategy or move capital, and the tests at the
   bottom prove it by parsing the package's own source rather than by
   trusting a convention.

The safety tests are deliberately source-parsing rather than
behavioural. A behavioural test proves the code did not promote a model
on the one path it exercised; parsing the source proves there is no
path that could.
"""

import ast
import json
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.experiment_schema import initialize_experiment_schema
from src.data_access.memory_schema import initialize_memory_schema
from src.domain.experiment_models import (
    AcceptanceCriteria, ArmSpec, DatasetSnapshot, Decision, EvaluationProtocol,
    Experiment, ExperimentStatus, ExperimentType, Hypothesis, HypothesisSource,
    ResourceLimits,
)
from src.experiments import api, engine, evaluators, templates

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
PACKAGE = os.path.join(ROOT, "src", "experiments")

WRITABLE = {"experiments", "experiment_runs", "experiment_results",
            "experiment_artifacts", "hypothesis_families"}

FORBIDDEN = {
    "trained_models", "model_evaluations", "model_promotions", "predictions",
    "signals", "signal_contributions", "research_features", "research_labels",
    "research_observations", "outcome_measurements", "outcome_aggregates",
    "error_attributions", "attribution_evidence", "trading_experiences",
    "memory_patterns", "portfolio_positions", "orders", "executions",
    "risk_limits", "paper_sessions", "recommendations",
}

BASE = datetime(2026, 6, 1, tzinfo=timezone.utc)


# ======================================================================
# Fixture: a cohort of experiences with a known shape
# ======================================================================

def build_conn(count=240, strong_is_better=True):
    """
    A record where strong signals really are more accurate -- so a
    genuine effect exists and the engine can be asked to find it, or
    (with the flag off) to correctly find nothing.
    """
    conn = sqlite3.connect(":memory:")
    initialize_memory_schema(conn)
    initialize_experiment_schema(conn)
    columns = [row[1] for row in conn.execute(
        "PRAGMA table_info(trading_experiences)")]
    for i in range(count):
        strong = (i % 2 == 0)
        if strong_is_better:
            correct = (i % 10 != 0) if strong else (i % 2 == 1 and i % 3 == 0)
        else:
            correct = (i % 3 == 0)
        record = {
            "experience_id": "exp-%04d" % i,
            "memory_version": "v1",
            "kind": "signal",
            "created_at": (BASE + timedelta(days=i)).isoformat(),
            "subject_kind": "signal",
            "subject_id": "sig-%04d" % i,
            "horizon": "5d",
            "quality": "validated",
            "experience_class": "correct_call" if correct else "wrong_call",
            "expected_direction": "long" if i % 2 == 0 else "short",
            "expected_return": 0.02,
            "actual_return": 0.03 if correct else -0.02,
            # Phase 19's vocabulary, which is what the evaluators read:
            # "hit" / "miss" / "neutral". A fixture that invents its own
            # words produces zero decided outcomes and every experiment
            # comes back INCONCLUSIVE for a reason that has nothing to
            # do with the code under test.
            "direction_result": "hit" if correct else "miss",
            "signal_strength": 0.9 if strong else 0.3,
            "signal_confidence": 0.8 if strong else 0.4,
            "event_type": "earnings" if i % 3 == 0 else "acquisition",
            "asset_class": "equity",
            "instrument_id": "INST%02d" % (i % 12),
            "available_at": (BASE + timedelta(days=i)).isoformat(),
            "information_cutoff": (BASE + timedelta(days=i)).isoformat(),
        }
        usable = {k: v for k, v in record.items() if k in columns}
        conn.execute(
            "INSERT INTO trading_experiences (%s) VALUES (%s)"
            % (", ".join(usable), ", ".join("?" * len(usable))),
            tuple(usable.values()))
    conn.commit()
    return conn


def an_experiment(**overrides):
    fields = dict(
        experiment_id="exp-engine-0001",
        name="strength floor",
        experiment_type=ExperimentType.SIGNAL,
        hypothesis=Hypothesis(
            statement="Strong signals are directionally more accurate.",
            mechanism="Weak signals are dominated by noise.",
            expected_effect="a higher directional_accuracy than the control",
            population="all signals",
            metric="directional_accuracy",
            source=HypothesisSource.RESEARCHER),
        baseline=evaluators.baseline("all_signals"),
        candidate=ArmSpec(name="strength >= 0.7",
                          evaluator="signal_strength_threshold",
                          parameters={"threshold": 0.7}, complexity=2),
        dataset=DatasetSnapshot(),
        protocol=EvaluationProtocol(),
        criteria=AcceptanceCriteria(),
        limits=ResourceLimits())
    fields.update(overrides)
    return Experiment(**fields)


# ======================================================================
# Splitting
# ======================================================================

class TestSplitting(unittest.TestCase):

    def setUp(self):
        self.conn = build_conn()
        self.experiment = an_experiment()
        self.rows = engine.load_cohort(self.conn, self.experiment)

    def tearDown(self):
        self.conn.close()

    def test_the_cohort_is_ordered_by_when_it_became_knowable(self):
        stamps = [r["available_at"] for r in self.rows]
        self.assertEqual(stamps, sorted(stamps))

    def test_the_split_is_chronological_not_random(self):
        """
        §38, §39: every training row must precede every test row. This
        is the property that makes an out-of-sample number mean
        anything at all.
        """
        train, test = engine.chronological_split(self.rows, 0.5)
        self.assertTrue(train and test)
        self.assertLessEqual(max(r["available_at"] for r in train),
                             min(r["available_at"] for r in test))

    def test_the_split_is_deterministic(self):
        first = engine.chronological_split(self.rows, 0.3)
        second = engine.chronological_split(self.rows, 0.3)
        self.assertEqual([r["experience_id"] for r in first[0]],
                         [r["experience_id"] for r in second[0]])

    def test_no_row_appears_on_both_sides(self):
        train, test = engine.chronological_split(self.rows, 0.5)
        self.assertFalse({r["experience_id"] for r in train}
                         & {r["experience_id"] for r in test})

    def test_an_empty_cohort_splits_into_two_empty_halves(self):
        self.assertEqual(engine.chronological_split([], 0.5), ([], []))

    def test_walk_forward_slices_never_train_on_the_future(self):
        """§37, §40: purge and embargo come from Phase 9's splitter."""
        for _label, train, test in engine.walk_forward_slices(
                self.rows, self.experiment):
            if not train or not test:
                continue
            self.assertLess(max(r["available_at"] for r in train),
                            min(r["available_at"] for r in test))

    def test_the_as_of_clause_hides_later_experience(self):
        """
        §11, §12, §72: `as_of` filters on `available_at`, Phase 21's
        point-in-time key -- when an experience became KNOWABLE, never
        when the row was written.
        """
        cutoff = (BASE + timedelta(days=60)).isoformat()
        narrow = an_experiment(dataset=DatasetSnapshot(as_of=cutoff))
        rows = engine.load_cohort(self.conn, narrow)
        self.assertTrue(rows)
        self.assertLess(len(rows), len(self.rows))
        self.assertTrue(all(r["available_at"] <= cutoff for r in rows))

    def test_a_cohort_larger_than_the_limit_is_refused_not_truncated(self):
        """
        §81: silently truncating would answer a different question than
        the one asked, and the reader could not tell.
        """
        small = an_experiment(limits=ResourceLimits(max_rows=10))
        with self.assertRaises(engine.ResourceLimitExceeded):
            engine.load_cohort(self.conn, small)


# ======================================================================
# Evaluators
# ======================================================================

class TestEvaluators(unittest.TestCase):

    def test_an_evaluator_is_a_registered_name_not_a_callable(self):
        """
        §80: the configuration cannot express code, so no interface can
        be talked into executing any.
        """
        spec, function = evaluators.get("signal_all")
        self.assertEqual(spec.name, "signal_all")
        self.assertTrue(callable(function))

    def test_an_unknown_evaluator_raises_rather_than_defaulting(self):
        with self.assertRaises(evaluators.UnknownEvaluator):
            evaluators.get("os.system")

    def test_an_unavailable_evaluator_raises_and_names_what_is_missing(self):
        """
        §33: a declared-but-unrunnable experiment type must fail loudly.
        Returning an empty cohort would surface as INCONCLUSIVE, which
        reads as "we tested and could not tell" rather than "we could
        not test".
        """
        found = False
        for spec in evaluators.registered():
            if not spec.runnable:
                found = True
                _spec, function = evaluators.get(spec.name)
                with self.assertRaises(evaluators.EvaluatorError) as caught:
                    function([], {})
                self.assertTrue(str(caught.exception).strip())
        self.assertTrue(found, "no unavailable evaluator is declared")

    def test_a_baseline_is_returned_as_a_copy(self):
        """
        §83: mutating the registry would silently change the control of
        every later experiment.
        """
        first = evaluators.baseline("all_signals")
        first.parameters["injected"] = True
        second = evaluators.baseline("all_signals")
        self.assertNotIn("injected", second.parameters)

    def test_an_invented_baseline_is_refused(self):
        with self.assertRaises(evaluators.EvaluatorError):
            evaluators.baseline("whatever_makes_me_look_good")

    def test_parameters_are_validated_against_the_spec(self):
        arm = ArmSpec(name="bad", evaluator="signal_strength_threshold",
                      parameters={"threshold": "; DROP TABLE experiments"})
        with self.assertRaises(Exception):
            evaluators.evaluate(arm, [])


# ======================================================================
# Running and deciding
# ======================================================================

class TestRunning(unittest.TestCase):

    def setUp(self):
        self.conn = build_conn()

    def tearDown(self):
        self.conn.close()

    def test_a_run_produces_a_result_and_records_its_fingerprint(self):
        experiment = an_experiment()
        engine.save_experiment(self.conn, experiment)
        run_record, result = engine.run(self.conn, experiment)
        self.assertIsNotNone(result)
        self.assertEqual(run_record.fingerprint, experiment.fingerprint)

    def test_a_run_against_an_edited_definition_is_refused(self):
        """
        §32, §73: the single most important refusal in the package. An
        experiment whose criteria can move after the answer is visible
        is not an experiment.
        """
        experiment = an_experiment()
        engine.save_experiment(self.conn, experiment)
        # api.start is the entry point that marks the experiment as
        # started; engine.run alone leaves a draft a draft, and the
        # freeze deliberately begins at the start, not at the writing.
        api.start(self.conn, experiment.experiment_id)
        relaxed = an_experiment(criteria=AcceptanceCriteria(min_effect=0.0))
        with self.assertRaises(engine.DefinitionChanged):
            engine.run(self.conn, relaxed)

    def test_a_stored_draft_can_be_loaded_and_run(self):
        """
        The end-to-end path a user actually takes: propose, walk away,
        come back, run. It was broken until the fingerprint survived
        storage -- every proposal was refused as edited.
        """
        experiment = an_experiment()
        engine.save_experiment(self.conn, experiment)
        reloaded = api.load(self.conn, experiment.experiment_id)
        run_record, result = engine.run(self.conn, reloaded)
        self.assertIsNotNone(result)
        self.assertEqual(run_record.status.value, "completed")

    def test_the_out_of_sample_effect_is_the_reported_one(self):
        experiment = an_experiment()
        engine.save_experiment(self.conn, experiment)
        _run, result = engine.run(self.conn, experiment)
        self.assertIsNotNone(result.effect)
        self.assertIsNotNone(result.effect_in_sample)

    def test_a_decision_is_always_accompanied_by_reasons(self):
        """§47: a verdict with no reasoning is an assertion."""
        experiment = an_experiment()
        engine.save_experiment(self.conn, experiment)
        _run, result = engine.run(self.conn, experiment)
        self.assertTrue(result.reasons)
        self.assertIn(result.decision,
                      (Decision.PASS, Decision.FAIL, Decision.INCONCLUSIVE))

    def test_a_tiny_cohort_is_inconclusive_rather_than_failed(self):
        """
        §45: "we could not tell" and "it does not work" are different
        findings, and collapsing them loses the more useful one.
        """
        conn = build_conn(count=12)
        experiment = an_experiment()
        engine.save_experiment(conn, experiment)
        _run, result = engine.run(conn, experiment)
        self.assertEqual(result.decision, Decision.INCONCLUSIVE)
        conn.close()

    def test_no_effect_in_the_data_does_not_produce_a_pass(self):
        conn = build_conn(strong_is_better=False)
        experiment = an_experiment()
        engine.save_experiment(conn, experiment)
        _run, result = engine.run(conn, experiment)
        self.assertNotEqual(result.decision, Decision.PASS)
        conn.close()

    def test_a_run_is_reproducible_for_a_seed(self):
        """§62, §63: a number that changes when you look twice is not one."""
        experiment = an_experiment()
        engine.save_experiment(self.conn, experiment)
        _r1, first = engine.run(self.conn, experiment, seed=99, allow_cache=False)
        _r2, second = engine.run(self.conn, experiment, seed=99, allow_cache=False)
        self.assertEqual(first.effect, second.effect)
        self.assertEqual((first.effect_low, first.effect_high),
                         (second.effect_low, second.effect_high))

    def test_the_limitations_name_the_out_of_sample_boundary(self):
        experiment = an_experiment()
        engine.save_experiment(self.conn, experiment)
        _run, result = engine.run(self.conn, experiment)
        self.assertTrue(any("out-of-sample" in text.lower()
                            for text in result.limitations))


# ======================================================================
# Sensitivity and ablation
# ======================================================================

class TestSensitivity(unittest.TestCase):

    def setUp(self):
        self.conn = build_conn()
        self.experiment = an_experiment()
        engine.save_experiment(self.conn, self.experiment)

    def tearDown(self):
        self.conn.close()

    def test_a_sweep_reports_a_shape_and_a_warning(self):
        report = engine.sensitivity(self.conn, self.experiment, "threshold",
                                    [0.2, 0.4, 0.6, 0.8])
        self.assertIn("shape", report)
        self.assertEqual(len(report["surface"]), 4)
        self.assertEqual(report["values_tested"], 4)

    def test_no_value_clearing_the_bar_is_not_a_single_point_optimum(self):
        """
        Zero values working and one value working are different
        findings. "Single point" implies an optimum exists to tune
        toward; when nothing clears the bar there is nothing to tune.
        """
        conn = build_conn(strong_is_better=False)
        experiment = an_experiment()
        engine.save_experiment(conn, experiment)
        report = engine.sensitivity(conn, experiment, "threshold",
                                    [0.2, 0.4, 0.6, 0.8])
        if report["shape"] == "no_effect":
            self.assertEqual(report["values_clearing_threshold"], 0)
            self.assertIn("no parameter value", report["note"].lower())
        conn.close()

    def test_a_sweep_beyond_the_variant_limit_is_refused(self):
        experiment = an_experiment(limits=ResourceLimits(max_variants=2))
        with self.assertRaises(engine.ResourceLimitExceeded):
            engine.sensitivity(self.conn, experiment, "threshold",
                               [0.1, 0.2, 0.3, 0.4, 0.5])

    def test_ablation_measures_what_each_component_was_worth(self):
        report = engine.ablation(self.conn, self.experiment, ["threshold"])
        self.assertTrue(report)


# ======================================================================
# The read API
# ======================================================================

class TestApi(unittest.TestCase):

    def setUp(self):
        self.conn = build_conn()
        self.experiment = an_experiment()
        engine.save_experiment(self.conn, self.experiment)
        api.start(self.conn, self.experiment.experiment_id)

    def tearDown(self):
        self.conn.close()

    def test_detail_returns_the_definition_and_its_result(self):
        detail = api.detail(self.conn, self.experiment.experiment_id)
        self.assertEqual(detail["experiment_id"], self.experiment.experiment_id)
        self.assertTrue(detail["runs"])
        self.assertTrue(detail["results"])

    def test_compare_does_not_rank(self):
        """
        §43: ranking experiments by effect size IS the selection bias
        the phase exists to expose. The comparison lists; the reader
        decides.
        """
        second = an_experiment(experiment_id="exp-engine-0002",
                               candidate=ArmSpec(
                                   name="strength >= 0.5",
                                   evaluator="signal_strength_threshold",
                                   parameters={"threshold": 0.5},
                                   complexity=2))
        engine.save_experiment(self.conn, second)
        api.start(self.conn, second.experiment_id)
        report = api.compare(self.conn, [self.experiment.experiment_id,
                                         "exp-engine-0002"])
        text = json.dumps(report).lower()
        for word in ("best", "winner", "recommended", "rank"):
            self.assertNotIn('"%s"' % word, text)

    def test_integrity_check_reports_counts_not_opinions(self):
        report = api.integrity_check(self.conn)
        self.assertIsInstance(report, dict)
        for value in report.values():
            self.assertIsInstance(value, int)

    def test_available_evaluators_declare_their_availability(self):
        listed = api.available_evaluators()
        self.assertTrue(listed)
        self.assertTrue(any(not spec["runnable"] for spec in listed),
                        "the unavailable evaluators must stay visible")


# ======================================================================
# §79, §80 — what this package structurally cannot do
# ======================================================================

def package_sources():
    for name in sorted(os.listdir(PACKAGE)):
        if name.endswith(".py"):
            with open(os.path.join(PACKAGE, name), encoding="utf-8") as handle:
                yield name, handle.read()


def executed_sql(source):
    """
    SQL string literals passed to execute/executemany/executescript.

    Scoped to those calls on purpose: parsing every string constant in
    the file flags docstrings that merely mention a table name, which
    is a false positive that trained Phase 19 to ignore the detector.
    """
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", None) not in (
                "execute", "executemany", "executescript"):
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                yield argument.value
            elif isinstance(argument, ast.JoinedStr):
                for piece in argument.values:
                    if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                        yield piece.value
            elif isinstance(argument, ast.Call):
                for inner in ast.walk(argument):
                    if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                        yield inner.value


class TestExperimentsChangeNothing(unittest.TestCase):
    """§79: an experiment is research, not a deployment."""

    def test_no_module_writes_a_production_table(self):
        offenders = []
        for name, source in package_sources():
            for text in executed_sql(source):
                upper = " ".join(text.upper().split())
                if not any(verb in upper for verb in
                           ("INSERT", "UPDATE", "DELETE", "REPLACE", "DROP", "ALTER")):
                    continue
                for table in FORBIDDEN:
                    if table.upper() in upper:
                        offenders.append("%s: writes %s" % (name, table))
        self.assertEqual(offenders, [],
                         "an experiment can modify the record it studies")

    def test_every_write_targets_an_experiment_table(self):
        found = set()
        for _name, source in package_sources():
            for text in executed_sql(source):
                upper = " ".join(text.upper().split())
                for verb in ("INSERT OR REPLACE INTO", "INSERT OR IGNORE INTO",
                             "INSERT INTO", "UPDATE", "DELETE FROM"):
                    if verb in upper:
                        tail = upper.split(verb, 1)[1].strip()
                        found.add(tail.split()[0].strip("( ").lower())
                        break
        self.assertTrue(found, "no writes found - the parser is broken")
        self.assertTrue(found <= WRITABLE, "writes %s" % (found - WRITABLE,))

    def test_nothing_promotes_a_model_or_moves_capital(self):
        for name, source in package_sources():
            lowered = source.lower()
            for word in ("def promote", "promote(", "place_order", "submit_order",
                         "set_capital", "update risk_limits", ".fit("):
                self.assertNotIn(word, lowered, "%s contains %r" % (name, word))

    def test_no_module_imports_a_decision_making_engine(self):
        forbidden = ("src.modeling.promotion", "src.execution", "src.risk",
                     "src.portfolio.rebalance")
        for name, source in package_sources():
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith(("import ", "from ")):
                    for module in forbidden:
                        self.assertNotIn(module, stripped, "%s: %s" % (name, stripped))

    def test_no_broker_other_than_ibkr_is_referenced(self):
        """Interactive Brokers remains the only broker, in every phase."""
        for name, source in package_sources():
            lowered = source.lower()
            for word in ("metatrader", "mt5", "alpaca", "oanda", "binance"):
                self.assertNotIn(word, lowered, "%s contains %r" % (name, word))

    def test_no_arbitrary_code_execution_is_reachable(self):
        """
        §80: an evaluator is a registered name. Nothing in the package
        may compile, exec, eval or import a string that came from a
        definition.
        """
        for name, source in package_sources():
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    called = getattr(node.func, "id", None)
                    self.assertNotIn(
                        called, ("eval", "exec", "compile", "__import__"),
                        "%s calls %s" % (name, called))
                    attribute = getattr(node.func, "attr", None)
                    self.assertNotEqual(
                        attribute, "import_module", "%s imports dynamically" % name)
                if isinstance(node, ast.Attribute) and node.attr == "system":
                    self.fail("%s reaches for os.system" % name)

    def test_no_learning_loop_updates_itself(self):
        """
        No autonomous learning and no reinforcement learning: an
        experiment reports, a human decides.
        """
        for name, source in package_sources():
            lowered = source.lower()
            for word in ("reinforcement", "q_learning", "auto_promote",
                         "self_update", "auto_apply"):
                self.assertNotIn(word, lowered, "%s contains %r" % (name, word))

    def test_no_llm_is_used_anywhere_in_the_package(self):
        for name, source in package_sources():
            lowered = source.lower()
            for word in ("openai", "anthropic", "chat.completions", "llm("):
                self.assertNotIn(word, lowered, "%s contains %r" % (name, word))

    def test_no_credentials_are_read_or_emitted(self):
        """§60, §80: nothing here touches account or broker secrets."""
        for name, source in package_sources():
            lowered = source.lower()
            for word in ("api_key", "api_secret", "password", "ibkr_account",
                         "account_number", "getenv(\"ib"):
                self.assertNotIn(word, lowered, "%s contains %r" % (name, word))


class TestProposalsAreHonest(unittest.TestCase):
    """§30, §31: a mined hypothesis must say it was mined."""

    def setUp(self):
        self.conn = build_conn()

    def tearDown(self):
        self.conn.close()

    def test_every_proposal_is_a_draft(self):
        """Generating is not executing."""
        for proposal in templates.propose_all(self.conn):
            self.assertEqual(proposal.status, ExperimentStatus.DRAFT)

    def test_a_mined_hypothesis_carries_the_caveat_in_its_mechanism(self):
        for proposal in templates.propose_all(self.conn):
            if proposal.hypothesis.source == HypothesisSource.MEMORY_PATTERN:
                self.assertIn("derived from the same historical record",
                              proposal.hypothesis.mechanism)

    def test_every_proposal_names_where_it_came_from(self):
        for proposal in templates.propose_all(self.conn):
            self.assertNotEqual(proposal.hypothesis.source,
                                HypothesisSource.RESEARCHER)
            self.assertTrue(proposal.hypothesis.source_reference)

    def test_proposals_are_not_written_unless_asked(self):
        templates.propose_all(self.conn)
        stored = self.conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
        self.assertEqual(stored, 0, "proposing must not write by itself")


if __name__ == "__main__":
    unittest.main()
