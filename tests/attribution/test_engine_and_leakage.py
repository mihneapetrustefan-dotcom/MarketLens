"""
tests/attribution/test_engine_and_leakage.py
--------------------------------------------------------
Ranking, versioning, idempotency, review — and the adversarial cases.

Covers §63 items 13-17 and 20-24, and all eleven of §64.

THE FAILURE MODE THIS DEFENDS AGAINST
-----------------------------------------
A diagnostic layer that explains everything is worse than none: it
produces confident prose about noise and the prose is indistinguishable
from insight. §64's list is essentially a catalogue of ways to do that,
and every entry has a test here.
"""

import ast
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.attribution.engine import _CAUSAL_DEPTH, attribute
from src.attribution.pipeline import (
    compare_versions, existing_identities, queue_for_review, run, save,
)
from src.data_access.attribution_schema import initialize_attribution_schema
from src.data_access.outcome_schema import initialize_outcome_schema
from src.domain.attribution_models import (
    ATTRIBUTION_METHOD_VERSION, MIN_COHORT_SAMPLE, AttributionConfidence,
    AttributionRole, AttributionStatus, ErrorAttribution, ErrorType, Evidence,
    Observability, Severity,
)

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
PACKAGE = os.path.join(ROOT, "src", "attribution")

#: The only tables this layer may write.
WRITABLE = {"error_attributions", "attribution_evidence",
            "attribution_review_queue"}

#: Writing any of these would let a diagnosis change the thing it
#: diagnoses, or let the future reach the past.
FORBIDDEN = {
    "research_features", "research_labels", "research_observations",
    "predictions", "signals", "signal_contributions", "trained_models",
    "model_evaluations", "model_promotions", "outcome_measurements",
    "outcome_aggregates", "price_candle_cache", "events", "canonical_events",
    "signal_strategies", "recommendations",
}


def outcome(**overrides):
    base = {
        "subject_kind": "signal", "subject_id": "sig-1", "horizon": "5d",
        "method_version": "v1", "status": "available",
        "direction_result": "hit", "expected_direction": "long",
        "expected_return": 0.02, "simple_return": 0.022,
        "realized_direction": "long", "mfe": 0.03, "mae": -0.01,
        "time_to_mfe_seconds": 86400.0, "reference_price": 100.0,
        "instrument_id": "i-1", "trained_model_id": "tm-1",
        "model_status": "evaluated", "market_regime": None,
        "horizon_sort": 5 * 6.5 * 3600.0,
    }
    base.update(overrides)
    return base


def cohort(sample_size=200, low=-0.05, high=0.05):
    return {"sample_size": sample_size, "p10_return": low,
            "p90_return": high, "directional_accuracy": 0.5}


# ======================================================================
# §63.13, §63.14, §18, §26 — the non-error verdicts
# ======================================================================

class TestTheSystemMaySayNothingWentWrong(unittest.TestCase):

    def test_a_clean_outcome_is_no_error(self):
        found, _, _ = attribute(outcome(), cohort=cohort())
        self.assertEqual(found[0].error_type, ErrorType.NO_ERROR)

    def test_an_ordinary_loss_is_an_expected_loss_not_a_mistake(self):
        """
        §18. Some losses are statistically normal, and forcing them into
        an error class makes the layer a machine for rationalising noise.
        """
        found, _, _ = attribute(
            outcome(direction_result="neutral", simple_return=-0.0005,
                    expected_return=0.0002, mfe=0.001),
            cohort=cohort())
        self.assertEqual(found[0].error_type, ErrorType.EXPECTED_LOSS)

    def test_an_expected_loss_cites_the_cohort_band(self):
        found, _, _ = attribute(
            outcome(direction_result="neutral", simple_return=-0.0005,
                    expected_return=0.0002, mfe=0.001),
            cohort=cohort())
        self.assertTrue(any(item.kind == "expectedness"
                            for item in found[0].evidence))

    def test_a_small_cohort_cannot_declare_anything_unusual(self):
        """
        §64: a small sample must not become a confident attribution.
        Below the threshold there is no expectedness judgement at all.
        """
        found, _, _ = attribute(
            outcome(direction_result="neutral", simple_return=-0.0005,
                    expected_return=0.0002, mfe=0.001),
            cohort=cohort(sample_size=MIN_COHORT_SAMPLE - 1))
        self.assertEqual(found[0].error_type, ErrorType.NO_ERROR)

    def test_an_unmeasured_outcome_is_never_declared_clean(self):
        """
        The bug this caught in development: peripheral detectors can
        still answer on a PENDING outcome, and the engine reached
        NO_ERROR for 2,199 measurements whose result is not yet known.
        You cannot call a decision sound when you do not know what
        happened.
        """
        found, _, _ = attribute(outcome(status="pending"), cohort=cohort())
        self.assertEqual(found[0].error_type, ErrorType.UNKNOWN)
        self.assertEqual(found[0].status,
                         AttributionStatus.INSUFFICIENT_EVIDENCE)

    def test_an_unusual_result_with_no_cause_goes_to_review_not_to_a_guess(self):
        """§27, §47: a surprising result is not automatically an error."""
        found, _, reason = attribute(
            outcome(direction_result="neutral", simple_return=-0.30,
                    expected_return=0.0002, mfe=0.001),
            cohort=cohort())
        self.assertEqual(found[0].error_type, ErrorType.UNKNOWN)
        self.assertEqual(found[0].status, AttributionStatus.REQUIRES_REVIEW)
        self.assertIsNotNone(reason)


# ======================================================================
# §63.16, §63.17, §19, §20 — several causes, ranked
# ======================================================================

class TestMultipleAndRankedAttributions(unittest.TestCase):

    def test_one_outcome_may_carry_several_attributions(self):
        found, _, _ = attribute(
            outcome(direction_result="miss", simple_return=-0.03, mfe=0.08),
            cohort=cohort())
        types = {a.error_type for a in found}
        self.assertGreaterEqual(len(types), 2)

    def test_exactly_one_attribution_is_primary(self):
        found, _, _ = attribute(
            outcome(direction_result="miss", simple_return=-0.03, mfe=0.08),
            cohort=cohort())
        primaries = [a for a in found if a.role == AttributionRole.PRIMARY]
        self.assertEqual(len(primaries), 1)

    def test_the_earlier_cause_wins_over_the_later_one(self):
        """
        §20. If the direction was wrong, the timing of a move that was
        never going to happen is a consequence, not a cause.
        """
        found, _, _ = attribute(
            outcome(direction_result="miss", simple_return=-0.03, mfe=0.08),
            cohort=cohort())
        primary = next(a for a in found if a.role == AttributionRole.PRIMARY)
        self.assertEqual(primary.error_type, ErrorType.PREDICTION_ERROR)

    def test_data_error_outranks_prediction_error(self):
        """A decision made on invalid data explains the wrong direction."""
        self.assertLess(_CAUSAL_DEPTH[ErrorType.DATA_ERROR],
                        _CAUSAL_DEPTH[ErrorType.PREDICTION_ERROR])

    def test_execution_and_portfolio_sit_last_in_the_chain(self):
        for late in (ErrorType.EXECUTION_ERROR, ErrorType.PORTFOLIO_ERROR):
            self.assertGreater(_CAUSAL_DEPTH[late],
                               _CAUSAL_DEPTH[ErrorType.PREDICTION_ERROR])

    def test_the_primary_records_why_it_outranked_the_others(self):
        found, _, _ = attribute(
            outcome(direction_result="miss", simple_return=-0.03, mfe=0.08),
            cohort=cohort())
        primary = next(a for a in found if a.role == AttributionRole.PRIMARY)
        self.assertTrue(any(item.kind == "ranking" for item in primary.evidence))

    def test_unassessable_layers_are_recorded_on_the_primary(self):
        """
        A cause may lie in a layer nobody could look at, and the
        conclusion has to say so rather than imply completeness.
        """
        found, _, _ = attribute(
            outcome(direction_result="miss", simple_return=-0.03),
            cohort=cohort())
        primary = next(a for a in found if a.role == AttributionRole.PRIMARY)
        self.assertTrue(any(item.kind == "coverage" for item in primary.evidence))

    def test_a_partly_assessable_case_is_marked_partially_attributed(self):
        found, _, _ = attribute(
            outcome(direction_result="miss", simple_return=-0.03),
            cohort=cohort())
        self.assertEqual(found[0].status,
                         AttributionStatus.PARTIALLY_ATTRIBUTED)


# ======================================================================
# §63.19, §22 — evidence
# ======================================================================

class TestEvidenceIsMandatory(unittest.TestCase):

    def test_an_attribution_without_evidence_refuses_to_be_written(self):
        empty = ErrorAttribution(
            subject_kind="signal", subject_id="s", horizon="5d",
            error_type=ErrorType.PREDICTION_ERROR)
        with self.assertRaises(ValueError) as caught:
            empty.require_evidence()
        self.assertIn("opinion stored as a fact", str(caught.exception))

    def test_every_produced_attribution_carries_evidence(self):
        for sample in (outcome(),
                       outcome(direction_result="miss", simple_return=-0.03),
                       outcome(status="pending")):
            found, _, _ = attribute(sample, cohort=cohort())
            for attribution in found:
                attribution.require_evidence()

    def test_evidence_names_the_table_it_came_from(self):
        found, _, _ = attribute(
            outcome(direction_result="miss", simple_return=-0.03),
            cohort=cohort())
        primary = next(a for a in found if a.role == AttributionRole.PRIMARY)
        self.assertTrue(any(item.source for item in primary.evidence))


# ======================================================================
# §63.18, §21 — confidence semantics
# ======================================================================

class TestConfidenceIsOrdinalNotProbability(unittest.TestCase):

    def test_the_values_are_labels(self):
        self.assertEqual(
            {member.value for member in AttributionConfidence},
            {"high", "medium", "low", "insufficient_evidence"})

    def test_they_order_correctly(self):
        self.assertGreater(AttributionConfidence.HIGH.rank,
                           AttributionConfidence.MEDIUM.rank)
        self.assertEqual(AttributionConfidence.INSUFFICIENT_EVIDENCE.rank, 0)

    def test_no_table_column_could_hold_a_calibrated_probability(self):
        conn = sqlite3.connect(":memory:")
        initialize_attribution_schema(conn)
        columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(error_attributions)")}
        for forbidden in ("p_value", "probability", "calibrated_confidence"):
            self.assertNotIn(forbidden, columns)
        conn.close()

    def test_severity_and_confidence_are_independent(self):
        """
        §25. CRITICAL severity with LOW confidence is a useful thing to
        be able to say, and impossible if the two collapse.
        """
        self.assertNotEqual({m.value for m in Severity},
                            {m.value for m in AttributionConfidence})


# ======================================================================
# Persistence: §63.20, §63.21, §63.22, §56
# ======================================================================

class PersistenceCase(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript("""
            CREATE TABLE signals (signal_id TEXT PRIMARY KEY, status TEXT,
                direction TEXT, strength REAL, confidence REAL,
                suppression_note TEXT, observation_id TEXT, valid_until TEXT,
                source_information_cutoff TEXT);
            CREATE TABLE research_observations (observation_id TEXT PRIMARY KEY,
                quality_level TEXT, market_regime TEXT, information_cutoff TEXT,
                dataset_version TEXT);
        """)
        initialize_outcome_schema(self.conn)
        initialize_attribution_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def add_outcome(self, subject_id="sig-1", horizon="5d", status="available",
                    direction_result="miss", simple_return=-0.03,
                    expected_return=0.02, mfe=0.08):
        self.conn.execute("""
            INSERT OR REPLACE INTO outcome_measurements (
                subject_kind, subject_id, horizon, method_version,
                horizon_value, horizon_unit, status, reference_rule,
                direction_result, expected_direction, expected_return,
                simple_return, mfe, mae, time_to_mfe_seconds, instrument_id,
                trained_model_id, model_status, signal_status, computed_at
            ) VALUES ('signal',?,?,'v1',5.0,'d',?,'first_close_at_or_after_cutoff',
                      ?,'long',?,?,?,-0.02,86400.0,'i-1','tm-1','evaluated',
                      'active','2026-09-05T00:00:00+00:00')
        """, (subject_id, horizon, status, direction_result, expected_return,
              simple_return, mfe))
        self.conn.commit()

    def count(self, table="error_attributions"):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def go(self, **kwargs):
        found, reviews, report = run(self.conn, **kwargs)
        save(self.conn, found)
        return report


class TestIdempotencyAndVersioning(PersistenceCase):

    def test_running_twice_does_not_change_the_row_count(self):
        self.add_outcome()
        self.go()
        first = self.count()
        self.go()
        self.assertEqual(self.count(), first)

    def test_evidence_does_not_accumulate_behind_a_stable_conclusion(self):
        self.add_outcome()
        self.go()
        first = self.count("attribution_evidence")
        self.go(recompute=True)
        self.assertEqual(self.count("attribution_evidence"), first)

    def test_recompute_replaces_rather_than_appends(self):
        self.add_outcome()
        self.go()
        first = self.count()
        self.go(recompute=True)
        self.assertEqual(self.count(), first)

    def test_a_new_method_version_adds_rows_beside_the_old(self):
        self.add_outcome()
        self.go()
        first = self.count()
        self.go(method_version="v2")
        self.assertEqual(self.count(), first * 2)

    def test_the_old_version_is_untouched(self):
        """§44: do not silently rewrite historical conclusions."""
        self.add_outcome()
        self.go()
        before = self.conn.execute("""
            SELECT error_type, confidence, summary FROM error_attributions
            WHERE method_version='v1' ORDER BY error_type
        """).fetchall()
        self.go(method_version="v2")
        after = self.conn.execute("""
            SELECT error_type, confidence, summary FROM error_attributions
            WHERE method_version='v1' ORDER BY error_type
        """).fetchall()
        self.assertEqual(before, after)

    def test_versions_can_be_compared(self):
        self.add_outcome()
        self.go()
        self.go(method_version="v2")
        # Same rules, so no disagreement — but the query must work.
        self.assertEqual(compare_versions(self.conn, "v1", "v2"), [])

    def test_the_method_version_is_part_of_the_primary_key(self):
        keys = {row[1] for row in self.conn.execute(
            "SELECT * FROM pragma_table_info('error_attributions') WHERE pk > 0")}
        self.assertIn("method_version", keys)
        self.assertIn("error_type", keys)


class TestReviewQueue(PersistenceCase):
    """§63.22, §47, §48."""

    def test_a_review_case_records_what_to_check(self):
        self.add_outcome()
        queue_for_review(self.conn, {"subject_kind": "signal",
                                     "subject_id": "sig-1", "horizon": "5d"},
                         "two equally weighted causes",
                         ["prediction_error", "timing_error"], "high", "v1")
        row = self.conn.execute("""
            SELECT reason, candidate_types, recommended_check, state
            FROM attribution_review_queue
        """).fetchone()
        self.assertIn("two equally", row[0])
        self.assertIn("prediction_error", row[1])
        self.assertTrue(row[2])
        self.assertEqual(row[3], "open")

    def test_requeuing_preserves_a_reviewed_state(self):
        """
        The queue exists because a rule could not decide, so a rule must
        not decide it is finished either.
        """
        case = {"subject_kind": "signal", "subject_id": "sig-1", "horizon": "5d"}
        queue_for_review(self.conn, case, "reason", [], "low", "v1")
        self.conn.execute("UPDATE attribution_review_queue SET state='reviewed'")
        self.conn.commit()
        queue_for_review(self.conn, case, "reason again", [], "low", "v1")
        self.assertEqual(self.conn.execute(
            "SELECT state FROM attribution_review_queue").fetchone()[0],
            "reviewed")


# ======================================================================
# §64 — adversarial
# ======================================================================

class TestAdversarial(PersistenceCase):

    def test_a_losing_result_is_not_automatically_a_prediction_error(self):
        """A neutral move inside the band is not a directional failure."""
        self.add_outcome(direction_result="neutral", simple_return=-0.0004,
                         expected_return=0.0003, mfe=0.001)
        self.go()
        types = {row[0] for row in self.conn.execute(
            "SELECT error_type FROM error_attributions WHERE role='primary'")}
        self.assertNotIn("prediction_error", types)

    def test_a_winning_result_is_not_automatically_correct(self):
        """
        §27, §64. A win with a badly mis-estimated size is still a
        calibration finding.
        """
        self.add_outcome(direction_result="hit", simple_return=0.20,
                         expected_return=0.01, mfe=0.22)
        self.go()
        types = {row[0] for row in self.conn.execute(
            "SELECT error_type FROM error_attributions")}
        self.assertIn("magnitude_error", types)

    def test_missing_data_does_not_become_a_data_error(self):
        """
        An unmeasured outcome is an absence of information, not a defect
        in the data that was used.
        """
        self.add_outcome(status="pending")
        self.go()
        types = {row[0] for row in self.conn.execute(
            "SELECT error_type FROM error_attributions")}
        self.assertNotIn("data_error", types)

    def test_execution_is_never_blamed_when_no_order_existed(self):
        self.add_outcome()
        self.go()
        types = {row[0] for row in self.conn.execute(
            "SELECT error_type FROM error_attributions")}
        self.assertNotIn("execution_error", types)

    def test_no_attribution_is_ever_stored_without_evidence(self):
        self.add_outcome()
        self.go()
        orphans = self.conn.execute("""
            SELECT COUNT(*) FROM error_attributions a WHERE NOT EXISTS (
                SELECT 1 FROM attribution_evidence e
                WHERE e.subject_id=a.subject_id AND e.horizon=a.horizon
                  AND e.error_type=a.error_type
                  AND e.method_version=a.method_version)
        """).fetchone()[0]
        self.assertEqual(orphans, 0)

    def test_a_counterfactual_is_never_stored_as_observed(self):
        from src.attribution import api
        self.add_outcome()
        result = api.counterfactual(self.conn, "signal", "sig-1", "5d",
                                    "half_size")
        self.assertEqual(result["observability"],
                         Observability.HYPOTHETICAL.value)
        self.assertIsNone(result["result"])

    def test_counterfactuals_are_excluded_from_profiles_by_default(self):
        from src.attribution.analytics import load_attributions
        self.add_outcome()
        self.go()
        self.conn.execute(
            "UPDATE error_attributions SET observability='hypothetical'")
        self.conn.commit()
        self.assertEqual(load_attributions(self.conn), [],
                         "a hypothetical was counted as history")

    def test_a_duplicate_attribution_cannot_be_created(self):
        self.add_outcome()
        self.go()
        first = self.count()
        for _ in range(3):
            self.go(recompute=True)
        self.assertEqual(self.count(), first)


# ======================================================================
# §39, §40 — leakage
# ======================================================================

def package_sources():
    for name in sorted(os.listdir(PACKAGE)):
        if name.endswith(".py"):
            with open(os.path.join(PACKAGE, name), encoding="utf-8") as handle:
                yield name, handle.read()


def executed_sql(source: str):
    """
    Strings handed to execute/executemany, via the AST.

    Scoped to database calls rather than all literals, for the reason
    Phase 19 recorded: scanning every string flags the package's own
    docstrings, which describe the invariant, as violations of it.
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


class TestAttributionChangesNothing(unittest.TestCase):
    """
    §39, §59. Attribution may inspect outcomes. It must never modify a
    decision, a feature, a model or a strategy.
    """

    def test_no_module_writes_outside_the_attribution_tables(self):
        offenders = []
        for name, source in package_sources():
            for text in executed_sql(source):
                upper = " ".join(text.upper().split())
                if not any(verb in upper for verb in
                           ("INSERT", "UPDATE", "DELETE", "REPLACE", "DROP", "ALTER")):
                    continue
                for table in FORBIDDEN:
                    if table.upper() in upper:
                        offenders.append(f"{name}: writes {table}")
        self.assertEqual(offenders, [],
                         "attribution can modify the thing it diagnoses")

    def test_every_write_targets_an_attribution_table(self):
        found = set()
        for _, source in package_sources():
            for text in executed_sql(source):
                upper = " ".join(text.upper().split())
                for verb in ("INSERT OR REPLACE INTO", "INSERT INTO",
                             "UPDATE", "DELETE FROM"):
                    if verb in upper:
                        tail = upper.split(verb, 1)[1].strip()
                        found.add(tail.split()[0].strip("( ").lower())
        self.assertTrue(found, "no writes found — the parser is broken")
        self.assertTrue(found <= WRITABLE, f"writes {found - WRITABLE}")

    def test_no_module_imports_training_inference_or_promotion(self):
        forbidden = ("src.modeling.engine", "src.modeling.inference",
                     "src.modeling.promotion", "src.features.engine",
                     "src.signals.engine")
        for name, source in package_sources():
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith(("import ", "from ")):
                    for module in forbidden:
                        self.assertNotIn(module, stripped, f"{name}: {stripped}")

    def test_nothing_promotes_trains_or_changes_a_threshold(self):
        """§59: no automatic strategy or model changes."""
        for name, source in package_sources():
            lowered = source.lower()
            for word in ("def promote", "promote(", "def train", ".fit(",
                         "update trained_models", "update signals"):
                self.assertNotIn(word, lowered, f"{name} contains {word!r}")

    def test_no_earlier_pipeline_script_reads_the_attribution_tables(self):
        """
        §52's shape, applied here: a feature or training stage that read
        error labels would be training on its own diagnosis.
        """
        scripts = os.path.join(ROOT, "scripts")
        offenders = []
        for name in sorted(os.listdir(scripts)):
            if not name.endswith(".py") or name == "attribute_errors.py":
                continue
            with open(os.path.join(scripts, name), encoding="utf-8") as handle:
                body = handle.read()
            if "error_attributions" in body or "attribution_evidence" in body:
                offenders.append(name)
        self.assertEqual(offenders, [])

    def test_the_feature_and_model_layers_do_not_know_these_tables_exist(self):
        for module in ("src/features/engine.py", "src/modeling/engine.py",
                       "src/modeling/inference.py", "src/signals/strategy.py",
                       "src/outcomes/measurement.py"):
            with open(os.path.join(ROOT, module), encoding="utf-8") as handle:
                body = handle.read()
            self.assertNotIn("error_attributions", body, module)

    def test_attribution_runs_after_outcome_measurement_in_the_pipeline(self):
        path = os.path.join(ROOT, ".github", "workflows", "pipeline.yml")
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("attribute_errors.py", body)
        self.assertLess(body.index("measure_outcomes.py"),
                        body.index("attribute_errors.py"),
                        "errors are attributed before outcomes are measured")

    def test_attributing_does_not_modify_the_outcome_it_reads(self):
        conn = sqlite3.connect(":memory:")
        initialize_outcome_schema(conn)
        initialize_attribution_schema(conn)
        conn.execute("""
            INSERT INTO outcome_measurements (
                subject_kind, subject_id, horizon, method_version,
                horizon_value, horizon_unit, status, reference_rule,
                direction_result, expected_direction, expected_return,
                simple_return, mfe, computed_at
            ) VALUES ('signal','sig-1','5d','v1',5.0,'d','available',
                      'first_close_at_or_after_cutoff','miss','long',0.02,
                      -0.03,0.08,'2026-09-05T00:00:00+00:00')
        """)
        conn.commit()
        before = conn.execute("SELECT * FROM outcome_measurements").fetchall()
        found, _, _ = run(conn)
        save(conn, found)
        self.assertEqual(conn.execute(
            "SELECT * FROM outcome_measurements").fetchall(), before)
        conn.close()


if __name__ == "__main__":
    unittest.main()
