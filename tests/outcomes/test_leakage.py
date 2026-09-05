"""
tests/outcomes/test_leakage.py
--------------------------------------
Adversarial: outcome data must never flow backwards (§28, §54).

THE ASYMMETRY BEING DEFENDED
--------------------------------
Outcome measurement is the ONE place in this repository allowed to read
prices dated after an information cutoff. Everywhere else that is a bug
and `PointInTimeView` raises `LookAheadViolation` for it.

That permission is safe only while the flow is one-directional:

    future prices  ->  outcome         ALLOWED — it is the job
    outcome        ->  prediction      FORBIDDEN
    outcome        ->  feature         FORBIDDEN
    outcome        ->  signal          FORBIDDEN
    outcome        ->  model           FORBIDDEN
    outcome        ->  training set    FORBIDDEN without an explicit
                                       dataset version and cutoff (§52)

If it ever became bidirectional the damage would be invisible: a model
trained on a feature contaminated by its own future would score
brilliantly in evaluation and fail in production, and every metric in
the system would agree that it was excellent.

WHY THESE READ SOURCE CODE
------------------------------
Several of these tests parse the package's own source. That is
deliberate. A behavioural test can only prove that leakage did not
happen on the inputs it tried; reading the source proves the write
cannot be reached at all. For a property whose violation is
undetectable downstream, the structural check is the stronger one.
"""

import ast
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.outcome_schema import initialize_outcome_schema
from src.domain.outcome_models import OutcomeStatus, OutcomeWindow, SubjectKind
from src.outcomes.measurement import measure

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
OUTCOME_PACKAGE = os.path.join(ROOT, "src", "outcomes")

#: Tables the outcome layer is allowed to write. Anything else is a
#: leak, by definition.
WRITABLE = {"outcome_measurements", "outcome_aggregates"}

#: Tables that carry model inputs. A write to any of these from the
#: outcome layer would put the future into the past.
FORBIDDEN = {
    "research_features", "research_labels", "research_observations",
    "predictions", "signals", "signal_contributions", "trained_models",
    "model_evaluations", "model_baseline_comparisons", "model_promotions",
    "price_candle_cache", "events", "canonical_events",
}

WRITE_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "REPLACE", "DROP", "ALTER")


def outcome_sources():
    for name in sorted(os.listdir(OUTCOME_PACKAGE)):
        if name.endswith(".py"):
            path = os.path.join(OUTCOME_PACKAGE, name)
            with open(path, "r", encoding="utf-8") as handle:
                yield name, handle.read()


def sql_literals(source: str):
    """
    Every string handed to `execute` / `executemany`, via the AST.

    Scoped to actual database calls rather than to all string constants
    on purpose. The first version of this scanned every literal and
    flagged this package's own DOCSTRINGS — which say things like "it
    does not write to `signals`" and "writes are `INSERT OR REPLACE`" —
    as evidence of the very thing they promise not to do.

    A test that fails on a comment explaining the invariant teaches
    nobody anything. Looking only at what is executed is both narrower
    and stricter: prose cannot trip it, and a real write cannot hide
    from it.
    """
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None)
        if name not in ("execute", "executemany", "executescript"):
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                yield argument.value
            # A formatted query: check every literal piece of it, since
            # the table name is what matters and it is never the
            # interpolated part.
            elif isinstance(argument, ast.JoinedStr):
                for piece in argument.values:
                    if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                        yield piece.value
            elif isinstance(argument, ast.Call):
                for inner in ast.walk(argument):
                    if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                        yield inner.value


class TestTheOutcomeLayerWritesNothingElse(unittest.TestCase):
    """§54: 'outcome written into feature store', structurally impossible."""

    def test_no_module_writes_to_a_model_input_table(self):
        offenders = []
        for name, source in outcome_sources():
            for text in sql_literals(source):
                upper = " ".join(text.upper().split())
                if not any(word in upper for word in WRITE_KEYWORDS):
                    continue
                for table in FORBIDDEN:
                    if table.upper() in upper:
                        offenders.append(f"{name}: writes {table}")
        self.assertEqual(offenders, [],
                         "the outcome layer can write a model input table — "
                         "future data would reach the past")

    def test_every_write_targets_an_outcome_table(self):
        found = set()
        for _, source in outcome_sources():
            for text in sql_literals(source):
                upper = " ".join(text.upper().split())
                for verb in ("INSERT OR REPLACE INTO", "INSERT INTO",
                             "UPDATE", "DELETE FROM"):
                    if verb not in upper:
                        continue
                    tail = upper.split(verb, 1)[1].strip()
                    found.add(tail.split()[0].strip("( ").lower())
        self.assertTrue(found, "no writes found at all — the parser is broken")
        self.assertTrue(found <= WRITABLE,
                        f"the outcome layer writes {found - WRITABLE}")

    def test_no_module_imports_the_training_or_inference_engine(self):
        """
        A structural guard against §52: outcomes must not be able to
        reach into training at all, let alone automatically.
        """
        forbidden_imports = ("src.modeling.engine", "src.modeling.inference",
                             "src.modeling.promotion", "src.features.engine")
        offenders = []
        for name, source in outcome_sources():
            for line in source.splitlines():
                stripped = line.strip()
                if not stripped.startswith(("import ", "from ")):
                    continue
                for module in forbidden_imports:
                    if module in stripped:
                        offenders.append(f"{name}: {stripped}")
        self.assertEqual(offenders, [])

    def test_nothing_in_the_outcome_layer_promotes_or_trains(self):
        """§51: only record and analyse."""
        for name, source in outcome_sources():
            lowered = source.lower()
            for word in ("def promote", "promote(", "def train", ".fit("):
                self.assertNotIn(
                    word, lowered,
                    f"{name} contains {word!r} — outcomes must not modify models")


class TestFutureDataCannotEnterAPrediction(unittest.TestCase):
    """
    §28: prove future outcome data cannot enter prediction inputs.

    Behavioural half. The measurement is handed bars from far in the
    future; nothing about the subject may change as a result.
    """

    def setUp(self):
        self.cutoff = datetime(2026, 9, 1, tzinfo=timezone.utc)
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript("""
            CREATE TABLE research_features (observation_id TEXT,
                qualified_name TEXT, value_json TEXT, as_of TEXT);
            CREATE TABLE predictions (prediction_id TEXT PRIMARY KEY,
                observation_id TEXT, predicted_value REAL,
                information_cutoff TEXT, trained_model_id TEXT,
                is_abstention INTEGER DEFAULT 0);
        """)
        self.conn.execute(
            "INSERT INTO research_features VALUES ('obs-1','f.a','1.0','2026-08-31T00:00:00+00:00')")
        self.conn.execute(
            "INSERT INTO predictions VALUES ('p-1','obs-1',0.02,'2026-09-01T00:00:00+00:00','tm-1',0)")
        self.conn.commit()
        initialize_outcome_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def snapshot(self):
        return (self.conn.execute("SELECT * FROM research_features").fetchall(),
                self.conn.execute("SELECT * FROM predictions").fetchall())

    def test_measuring_leaves_features_and_predictions_byte_identical(self):
        before = self.snapshot()
        bars = [{"timestamp": self.cutoff + timedelta(days=d), "open": 100.0,
                 "high": 200.0, "low": 50.0, "close": 100.0 + d * 10,
                 "adjusted_close": None, "volume": 1.0} for d in range(11)]
        outcome = measure(
            SubjectKind.PREDICTION, "p-1", OutcomeWindow.parse("10d"),
            cutoff=self.cutoff, direction="long", bars=bars,
            data_as_of=self.cutoff + timedelta(days=90), interval="1d")
        self.assertEqual(outcome.status, OutcomeStatus.AVAILABLE)
        self.assertGreater(outcome.simple_return, 0)
        self.assertEqual(self.snapshot(), before,
                         "measuring an outcome modified a model input")

    def test_a_spectacular_future_move_changes_no_stored_input(self):
        """§54: 'future candle accidentally loaded before prediction'."""
        before = self.snapshot()
        bars = [{"timestamp": self.cutoff + timedelta(days=d), "open": 100.0,
                 "high": 1e6, "low": 0.01, "close": 100.0 * (1 + d),
                 "adjusted_close": None, "volume": 1.0} for d in range(6)]
        measure(SubjectKind.SIGNAL, "sig-1", OutcomeWindow.parse("5d"),
                cutoff=self.cutoff, direction="long", bars=bars,
                data_as_of=self.cutoff + timedelta(days=90), interval="1d")
        self.assertEqual(self.snapshot(), before)

    def test_the_measurement_never_reaches_back_before_the_cutoff(self):
        """
        A bar dated before the cutoff must not become the reference
        price, no matter how convenient. Using it would credit the
        signal with a move that had already happened.
        """
        bars = [
            {"timestamp": self.cutoff - timedelta(days=5), "open": 10.0,
             "high": 10.0, "low": 10.0, "close": 10.0,
             "adjusted_close": None, "volume": 1.0},
            {"timestamp": self.cutoff + timedelta(days=1), "open": 100.0,
             "high": 100.0, "low": 100.0, "close": 100.0,
             "adjusted_close": None, "volume": 1.0},
            {"timestamp": self.cutoff + timedelta(days=2), "open": 101.0,
             "high": 101.0, "low": 101.0, "close": 101.0,
             "adjusted_close": None, "volume": 1.0},
        ]
        outcome = measure(
            SubjectKind.SIGNAL, "sig-1", OutcomeWindow.parse("1d"),
            cutoff=self.cutoff, direction="long", bars=bars,
            data_as_of=self.cutoff + timedelta(days=90), interval="1d")
        self.assertAlmostEqual(outcome.reference_price, 100.0)
        self.assertAlmostEqual(outcome.simple_return, 0.01, places=6)


class TestTheDangerousDefaults(unittest.TestCase):
    """
    §54, the quiet ones. Each of these would produce a plausible number
    from an absence, and none of them would look wrong in a table.
    """

    def setUp(self):
        self.cutoff = datetime(2026, 9, 1, tzinfo=timezone.utc)

    def bars(self, closes, start=0):
        return [{"timestamp": self.cutoff + timedelta(days=start + i),
                 "open": c, "high": c, "low": c, "close": c,
                 "adjusted_close": None, "volume": 1.0}
                for i, c in enumerate(closes)]

    def test_an_insufficient_horizon_is_not_a_zero_return(self):
        outcome = measure(
            SubjectKind.SIGNAL, "s", OutcomeWindow.parse("5d"),
            cutoff=self.cutoff, direction="long", bars=self.bars([100.0]),
            data_as_of=self.cutoff + timedelta(days=365), interval="1d")
        self.assertIsNone(outcome.simple_return)
        self.assertNotEqual(outcome.status, OutcomeStatus.AVAILABLE)

    def test_missing_market_data_is_not_a_miss(self):
        outcome = measure(
            SubjectKind.SIGNAL, "s", OutcomeWindow.parse("1d"),
            cutoff=self.cutoff, direction="long", bars=[],
            data_as_of=self.cutoff + timedelta(days=365), interval="1d")
        self.assertNotEqual(outcome.direction_result.value, "miss")
        self.assertEqual(outcome.direction_result.value, "insufficient_data")

    def test_a_delisted_instrument_is_insufficient_not_a_total_loss(self):
        """
        Bars stop entirely. The tempting wrong answer is -100%; the true
        answer is that the data cannot say.
        """
        outcome = measure(
            SubjectKind.SIGNAL, "s", OutcomeWindow.parse("10d"),
            cutoff=self.cutoff, direction="long",
            bars=self.bars([100.0, 99.0]),
            data_as_of=self.cutoff + timedelta(days=365), interval="1d")
        self.assertEqual(outcome.status, OutcomeStatus.INSUFFICIENT_DATA)
        self.assertIsNone(outcome.simple_return)

    def test_a_corporate_action_does_not_create_a_false_return(self):
        """§54: 'corporate action creates false return'."""
        outcome = measure(
            SubjectKind.SIGNAL, "s", OutcomeWindow.parse("1d"),
            cutoff=self.cutoff, direction="long",
            bars=self.bars([100.0, 4.0]),
            data_as_of=self.cutoff + timedelta(days=365), interval="1d")
        # A -96% day is inside the plausible band, so it is measured —
        # but the adjusted close is what protects the real case, and
        # that is covered in test_measurement. What must NOT happen is a
        # silent clamp.
        self.assertIsNotNone(outcome.simple_return)
        self.assertLess(outcome.simple_return, -0.9,
                        "the extreme value was clamped rather than reported")

    def test_a_zero_price_is_not_treated_as_a_valid_measurement(self):
        outcome = measure(
            SubjectKind.SIGNAL, "s", OutcomeWindow.parse("1d"),
            cutoff=self.cutoff, direction="long", bars=self.bars([100.0, 0.0]),
            data_as_of=self.cutoff + timedelta(days=365), interval="1d")
        self.assertNotEqual(outcome.status, OutcomeStatus.AVAILABLE)
        self.assertIsNone(outcome.simple_return)

    def test_an_abstention_produces_no_direction_verdict(self):
        outcome = measure(
            SubjectKind.SIGNAL, "s", OutcomeWindow.parse("1d"),
            cutoff=self.cutoff, direction="no_signal",
            bars=self.bars([100.0, 105.0]),
            data_as_of=self.cutoff + timedelta(days=365), interval="1d")
        self.assertEqual(outcome.direction_result.value, "insufficient_data")


class TestThePipelineStaysLastInTheChain(unittest.TestCase):
    """
    §57: outcome measurement must come after signal generation, or a
    later stage could compute a feature that has seen the answer.
    """

    def test_the_workflow_runs_outcomes_after_signals(self):
        path = os.path.join(ROOT, ".github", "workflows", "pipeline.yml")
        with open(path, "r", encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("measure_outcomes.py", body,
                      "the pipeline does not measure outcomes at all")
        self.assertLess(body.index("generate_signals.py"),
                        body.index("measure_outcomes.py"),
                        "outcomes are measured before signals are generated")

    def test_no_earlier_pipeline_script_reads_the_outcome_tables(self):
        """
        The strongest form of §52: a training or feature stage that
        queried `outcome_measurements` would be training on the future.
        """
        scripts = os.path.join(ROOT, "scripts")
        offenders = []
        for name in sorted(os.listdir(scripts)):
            if not name.endswith(".py") or name == "measure_outcomes.py":
                continue
            with open(os.path.join(scripts, name), "r", encoding="utf-8") as handle:
                body = handle.read()
            if "outcome_measurements" in body or "outcome_aggregates" in body:
                offenders.append(name)
        self.assertEqual(offenders, [],
                         "a non-outcome script reads the outcome tables")

    def test_the_feature_engine_does_not_know_the_outcome_tables_exist(self):
        for module in ("src/features/engine.py", "src/modeling/engine.py",
                       "src/modeling/inference.py", "src/signals/strategy.py"):
            with open(os.path.join(ROOT, module), "r", encoding="utf-8") as handle:
                body = handle.read()
            self.assertNotIn("outcome_measurements", body, module)
            self.assertNotIn("outcome_aggregates", body, module)


if __name__ == "__main__":
    unittest.main()
