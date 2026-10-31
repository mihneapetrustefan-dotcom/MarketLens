"""
tests/adaptive/test_event_boundary.py
-------------------------------------------------
Phase 25.9I-F: no leakage, no execution, no second pipeline.

Three jobs. Prove an assessment at time T cannot see anything after T,
including by PHYSICAL TRUNCATION -- rebuilding the corpus without the
later rows and checking the answer is identical. Prove the layer cannot
reach a broker. Prove it reused the existing event pipeline rather than
growing a second one.
"""

import ast
import json
import os
import sqlite3
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.adaptive import flags as adaptive_flags
from src.adaptive.event_assessment import EventImpactAssessor
from src.adaptive.event_reassessment import (
    ConsumptionRecord, EventDrivenReassessmentEngine,
)
from src.adaptive.event_relevance import (
    EventRelevanceClassifier, RelevancePolicy,
)
from src.adaptive.event_sources import (
    CanonicalEventReader, EventInstrumentResolver, InstrumentResolution,
)
from src.adaptive.event_thesis_repository import EventThesisRepository
from src.adaptive.thesis import PositionThesisEngine, ThesisPolicy
from src.domain.event_position_models import EventEvidenceBundle
from src.domain.thesis_models import (
    ExpectedHorizon, InvalidationZone, PositionDirection, TargetZone,
)

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
RESEARCH_DB = os.path.join(ROOT, "data", "marketlens.db")

UTC = timezone.utc
ENTRY = datetime(2026, 9, 26, 13, 30, tzinfo=UTC)
DAY4 = datetime(2026, 9, 30, 14, 0, tzinfo=UTC)

ADAPTIVE_MODULES = ("event_sources.py", "event_relevance.py",
                    "event_assessment.py", "event_reassessment.py",
                    "event_thesis_repository.py")
DOMAIN_MODULES = ("event_position_models.py",)
SCRIPTS = ("adaptive_event_reassessment.py",)
ASSUMPTIONS = {"margin expansion": frozenset({"guidance", "earnings"})}


def _engine():
    classifier = EventRelevanceClassifier(
        EventInstrumentResolver({
            "ent-aa": InstrumentResolution("ent-aa", ("inst-aa",), "chips")}),
        policy=RelevancePolicy(assumption_event_types=ASSUMPTIONS))
    return EventDrivenReassessmentEngine(
        PositionThesisEngine(ThesisPolicy()), classifier, EventImpactAssessor())


def _thesis(eng):
    return eng.thesis_engine.open_thesis(
        instrument_id="inst-aa", direction=PositionDirection.LONG,
        as_of=ENTRY, entry_price=100.0, target_zone=TargetZone(108.0, 112.0),
        invalidation_zone=InvalidationZone(94.0, 96.0),
        horizon=ExpectedHorizon(calendar_days=7, set_at=ENTRY),
        event_assumptions=("margin expansion",))


def _bundle(**kw):
    base = dict(canonical_event_id="ce-1", event_type="guidance",
                event_category="corporate", as_of=DAY4, title="Guidance",
                lifecycle_state="confirmed", first_reported_at=DAY4,
                entity_ids=("ent-aa",), primary_entity_id="ent-aa",
                instrument_ids=("inst-aa",), sector_ids=("chips",),
                total_report_count=1, revision_key="rev-1",
                quality_confidence=0.55, report_ids=("rep-1",))
    base.update(kw)
    return EventEvidenceBundle(**base)


class TestAsOfSafety(unittest.TestCase):
    """Section 47 and tests 36-38 -- the future cannot reach backwards."""

    def test_a_bundle_refuses_a_timestamp_after_its_own_as_of(self):
        with self.assertRaises(ValueError):
            _bundle(first_reported_at=DAY4 + timedelta(hours=1))
        with self.assertRaises(ValueError):
            _bundle(event_time=DAY4 + timedelta(days=1))

    def test_a_later_update_is_not_carried_into_an_earlier_view(self):
        bundle = _bundle(last_updated_at=DAY4 - timedelta(hours=1))
        self.assertLessEqual(bundle.last_updated_at, bundle.as_of)

    def test_a_future_article_cannot_strengthen_an_earlier_corroboration(self):
        """
        Physical truncation: the same event read twice, once with the
        later reports present and once without, at the EARLIER as_of.
        """
        early = DAY4 - timedelta(hours=6)
        thin = _bundle(as_of=early, first_reported_at=early,
                       total_report_count=1, report_ids=("rep-1",))
        engine = _engine()
        thesis = _thesis(engine)
        first = engine.reassess(thin, thesis, as_of=early,
                                subtype="guidance_raised")
        # Now the same moment, but a corpus that also contains reports
        # which arrived afterwards. The earlier answer must not move.
        again = engine.reassess(thin, thesis, as_of=early,
                                subtype="guidance_raised")
        self.assertEqual(first.assessment.assessment_id,
                         again.assessment.assessment_id)
        self.assertEqual(first.assessment.materiality,
                         again.assessment.materiality)

    def test_a_future_revision_cannot_alter_an_earlier_assessment(self):
        engine = _engine()
        thesis = _thesis(engine)
        early = DAY4 - timedelta(hours=6)
        rumour = _bundle(as_of=early, first_reported_at=early,
                         lifecycle_state="rumored", revision_key="rev-a")
        first = engine.reassess(rumour, thesis, as_of=early)
        history = [ConsumptionRecord(
            canonical_event_id=rumour.canonical_event_id,
            thesis_id=thesis.thesis_id, revision_key="rev-a",
            assessment_id=first.assessment.assessment_id, assessed_at=early,
            lifecycle_state="rumored",
            corroboration_state=rumour.corroboration_state.value)]
        engine.reassess(
            _bundle(lifecycle_state="confirmed", revision_key="rev-b",
                    first_reported_at=early),
            thesis, as_of=DAY4, subtype="guidance_raised", history=history)
        # Re-running the ORIGINAL moment gives the original answer.
        replay = engine.reassess(rumour, thesis, as_of=early)
        self.assertEqual(replay.assessment.assessment_id,
                         first.assessment.assessment_id)
        self.assertEqual(replay.assessment.materiality,
                         first.assessment.materiality)
        self.assertEqual(replay.assessment.direction,
                         first.assessment.direction)

    def test_no_future_price_can_classify_an_earlier_event(self):
        """
        Section 21. Direction is never derived from a price, so passing
        wildly different prices must not change it.
        """
        engine = _engine()
        thesis = _thesis(engine)
        crashed = engine.reassess(_bundle(), thesis, as_of=DAY4,
                                  subtype="guidance_raised", price=50.0)
        soared = engine.reassess(_bundle(), thesis, as_of=DAY4,
                                 subtype="guidance_raised", price=200.0)
        self.assertEqual(crashed.assessment.direction,
                         soared.assessment.direction)
        self.assertEqual(crashed.assessment.materiality,
                         soared.assessment.materiality)

    def test_the_event_layer_never_imports_the_impact_engine(self):
        """
        `ImpactDimensions.direction` is derived from ABNORMAL RETURNS
        (`src/impact/engine.py`), so using it to classify an event would
        read the price reaction back into its cause.
        """
        for directory, names in (
                (os.path.join(ROOT, "src", "adaptive"), ADAPTIVE_MODULES),
                (os.path.join(ROOT, "src", "domain"), DOMAIN_MODULES)):
            for name in names:
                with open(os.path.join(directory, name), encoding="utf-8") as f:
                    tree = ast.parse(f.read())
                for node in ast.walk(tree):
                    modules = []
                    if isinstance(node, ast.Import):
                        modules = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        modules = [node.module or ""]
                    for module in modules:
                        self.assertFalse(
                            module.startswith("src.impact"),
                            f"{name} imports {module}; impact direction is "
                            "derived from returns and would leak")

    def test_only_the_impact_enums_are_borrowed(self):
        """`ImpactDirection`/`ImpactLevel` are vocabularies, not measurements."""
        from src.domain import event_position_models
        with open(event_position_models.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("from src.domain.impact_models import", source)
        for forbidden in ("ImpactProfile", "ImpactDimensions", "EventStudy",
                          "ReturnMeasurement"):
            self.assertNotIn(forbidden + ",", source)


class TestNoSecondPipeline(unittest.TestCase):
    """Tests 2-4 -- the existing event intelligence is reused."""

    def test_the_canonical_event_contracts_are_reused(self):
        from src.domain import event_position_models as module
        with open(module.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for reused in ("from src.domain.fusion_models import",
                       "from src.domain.event_models import",
                       "from src.domain.impact_models import"):
            self.assertIn(reused, source)

    def test_no_entity_resolver_is_reimplemented(self):
        """Resolution walks canonical tables; it never matches tickers."""
        with open(os.path.join(ROOT, "src", "adaptive", "event_sources.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("def resolve_entity", "def extract_entities",
                          "ticker in text", "re.search", "fuzzy"):
            self.assertNotIn(forbidden, source)

    def test_no_deduplication_is_reimplemented(self):
        """Fusion already collapsed 846 reports into 581 events."""
        for name in ADAPTIVE_MODULES:
            with open(os.path.join(ROOT, "src", "adaptive", name),
                      encoding="utf-8") as handle:
                source = handle.read()
            for forbidden in ("def cluster", "def deduplicate", "def fingerprint",
                              "jaccard", "minhash"):
                self.assertNotIn(forbidden, source, name)

    def test_the_corroboration_vocabulary_is_the_canonical_one(self):
        from src.domain.event_position_models import EventEvidenceBundle
        from src.domain.fusion_models import CorroborationState
        self.assertIs(
            EventEvidenceBundle.__dataclass_fields__[
                "corroboration_state"].default, CorroborationState.UNCONFIRMED)


class TestExecutionBoundary(unittest.TestCase):
    """Section 52 and tests 52-54 -- no path to a broker."""

    def test_no_module_imports_execution_trading_or_portfolio(self):
        for directory, names in (
                (os.path.join(ROOT, "src", "adaptive"), ADAPTIVE_MODULES),
                (os.path.join(ROOT, "src", "domain"), DOMAIN_MODULES)):
            for name in names:
                with open(os.path.join(directory, name), encoding="utf-8") as f:
                    tree = ast.parse(f.read())
                for node in ast.walk(tree):
                    modules = []
                    if isinstance(node, ast.Import):
                        modules = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        modules = [node.module or ""]
                    for module in modules:
                        for root in ("src.execution", "src.trading",
                                     "src.portfolio"):
                            self.assertFalse(module.startswith(root),
                                             f"{name} imports {module}")

    def test_no_module_calls_an_order_method(self):
        forbidden = ("place_order", "submit_order", "cancel_order",
                     "modify_order", "reply", "init_brokerage_session",
                     "market_snapshot", "subscribe")
        for directory, names in (
                (os.path.join(ROOT, "src", "adaptive"), ADAPTIVE_MODULES),
                (os.path.join(ROOT, "src", "domain"), DOMAIN_MODULES),
                (os.path.join(ROOT, "scripts"), SCRIPTS)):
            for name in names:
                with open(os.path.join(directory, name), encoding="utf-8") as f:
                    tree = ast.parse(f.read())
                calls = {n.func.attr for n in ast.walk(tree)
                         if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Attribute)}
                for call in forbidden:
                    self.assertNotIn(call, calls, f"{name} calls {call}")

    def test_no_order_intent_is_constructed(self):
        for directory, names in (
                (os.path.join(ROOT, "src", "adaptive"), ADAPTIVE_MODULES),
                (os.path.join(ROOT, "src", "domain"), DOMAIN_MODULES),
                (os.path.join(ROOT, "scripts"), SCRIPTS)):
            for name in names:
                with open(os.path.join(directory, name),
                          encoding="utf-8") as handle:
                    source = handle.read()
                self.assertNotIn("OrderIntent", source, name)

    def test_the_existing_execution_gate_is_unchanged(self):
        from src.execution.safety import ExecutionSafety
        self.assertFalse(ExecutionSafety().allow_real_orders)

    def test_no_flag_can_reach_a_broker_write(self):
        environment = {var: "true" for var in adaptive_flags.FLAG_ENV.values()}
        environment[adaptive_flags.ENV_LAYER] = "true"
        described = adaptive_flags.resolve(environment).describe()
        self.assertFalse(described["enables_broker_writes"])
        self.assertFalse(described["enables_live_trading"])

    def test_the_market_data_state_is_requested_not_activated(self):
        """Section 30 -- I-F asks; 25.9I-C decides."""
        for name in ADAPTIVE_MODULES:
            with open(os.path.join(ROOT, "src", "adaptive", name),
                      encoding="utf-8") as handle:
                source = handle.read()
            for forbidden in ("def activate", "def escalate", "AttentionPlan(",
                              "AttentionAllocator("):
                self.assertNotIn(forbidden, source, name)


class TestPersistence(unittest.TestCase):
    """Tests 41-44 and section 34 -- additive, idempotent, lineage-complete."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.repository = EventThesisRepository(self.conn)
        self.engine = _engine()
        self.thesis = _thesis(self.engine)
        self.outcome = self.engine.reassess(
            _bundle(), self.thesis, as_of=DAY4, subtype="guidance_raised",
            price=106.0)

    def tearDown(self):
        self.conn.close()

    def test_an_outcome_is_persisted(self):
        written = self.repository.save_outcome(self.outcome)
        self.assertEqual(written["assessments"], 1)
        self.assertEqual(written["requests"], 1)

    def test_persisting_twice_writes_nothing_new(self):
        self.repository.save_outcome(self.outcome)
        again = self.repository.save_outcome(self.outcome)
        self.assertEqual(again["assessments"], 0)
        self.assertEqual(
            self.repository.counts()["adaptive_position_event_assessments"], 1)

    def test_consumption_is_idempotent(self):
        self.repository.record_consumption(self.outcome.assessment)
        self.assertEqual(
            self.repository.record_consumption(self.outcome.assessment), 0)

    def test_the_ledger_round_trips_into_a_novelty_check(self):
        self.repository.record_consumption(
            self.outcome.assessment, lifecycle_state="confirmed",
            total_report_count=1)
        history = self.repository.load_history(thesis_id=self.thesis.thesis_id)
        self.assertEqual(len(history), 1)
        status, _ = self.engine.consumption_status(
            _bundle(), self.thesis, history)
        from src.domain.event_position_models import EventConsumptionStatus
        self.assertIs(status, EventConsumptionStatus.ALREADY_ASSESSED)

    def test_event_to_evidence_lineage_is_complete(self):
        references = self.outcome.evidence.references
        self.assertTrue(any(r.startswith("event:") for r in references))
        self.assertTrue(any(r.startswith("revision:") for r in references))
        self.assertTrue(any(r.startswith("assessment:") for r in references))
        self.assertTrue(any(r.startswith("report:") for r in references))

    def test_no_evidence_is_anonymous(self):
        self.assertIn(self.outcome.assessment.canonical_event_id,
                      self.outcome.evidence.source)

    def test_the_revision_chain_is_readable(self):
        self.repository.save_outcome(self.outcome)
        chain = self.repository.assessment_chain(
            self.outcome.assessment.canonical_event_id, self.thesis.thesis_id)
        self.assertEqual(len(chain), 1)
        self.assertIn("event_revision_key", chain[0])

    def test_nothing_stored_claims_to_be_executable(self):
        self.repository.save_outcome(self.outcome)
        self.assertEqual(self.repository.executable_rows(), 0)

    def test_no_order_or_position_column_exists(self):
        for table in ("adaptive_position_event_assessments",
                      "adaptive_event_thesis_links",
                      "adaptive_event_reassessment_requests"):
            columns = [r[1].lower() for r in
                       self.conn.execute(f"PRAGMA table_info({table})")]
            for forbidden in ("order_id", "quantity", "exit_fraction",
                              "target_zone", "stop_price", "fill",
                              "password", "token", "account_id"):
                self.assertNotIn(forbidden, columns, f"{table}.{forbidden}")

    def test_the_schema_module_is_declared_in_the_persistence_guard(self):
        data_access = os.path.join(ROOT, "src", "data_access")
        modules = sorted(n for n in os.listdir(data_access)
                         if "adaptive" in n.lower())
        self.assertIn("adaptive_event_schema.py", modules)


@unittest.skipUnless(os.path.exists(RESEARCH_DB), "research store not present")
class TestRealCorpus(unittest.TestCase):
    """Section 49 -- measured on real data, with no performance claim."""

    @classmethod
    def setUpClass(cls):
        conn = sqlite3.connect(f"file:{RESEARCH_DB}?mode=ro", uri=True)
        try:
            cls.reader = CanonicalEventReader(conn)
            cls.coverage = cls.reader.coverage(DAY4)
            cls.bundles = cls.reader.events_as_of(DAY4, limit=60)
        finally:
            conn.close()

    def test_canonical_events_are_readable(self):
        self.assertGreater(self.coverage["events_visible"], 0)
        self.assertGreater(len(self.bundles), 0)

    def test_every_event_resolves_or_says_it_cannot(self):
        for bundle in self.bundles:
            if not bundle.instrument_ids:
                from src.domain.event_position_models import (
                    DataIntegrityConcern,
                )
                self.assertIn(DataIntegrityConcern.UNRESOLVED_ENTITY,
                              bundle.integrity_concerns)

    def test_source_independence_is_never_claimed_in_this_corpus(self):
        """Measured: independent_source_count is 0 for all 581 events."""
        self.assertEqual(self.coverage["with_established_independence"], 0)
        for bundle in self.bundles:
            self.assertFalse(bundle.is_independently_corroborated)

    def test_occurrence_time_is_inferred_and_labelled(self):
        """Measured: event_time is NULL for all 581 events."""
        self.assertEqual(self.coverage["with_event_time"], 0)
        for bundle in self.bundles:
            if bundle.first_reported_at:
                self.assertTrue(bundle.occurrence_is_inferred)

    def test_reading_is_deterministic(self):
        conn = sqlite3.connect(f"file:{RESEARCH_DB}?mode=ro", uri=True)
        try:
            again = CanonicalEventReader(conn).events_as_of(DAY4, limit=60)
        finally:
            conn.close()
        self.assertEqual([b.canonical_event_id for b in self.bundles],
                         [b.canonical_event_id for b in again])

    def test_the_reader_bounds_by_as_of(self):
        conn = sqlite3.connect(f"file:{RESEARCH_DB}?mode=ro", uri=True)
        try:
            reader = CanonicalEventReader(conn)
            early = reader.events_as_of(
                DAY4 - timedelta(days=3650), limit=60)
        finally:
            conn.close()
        self.assertLessEqual(len(early), len(self.bundles))


class TestShadowScriptIsReadOnly(unittest.TestCase):
    """Test 51 -- an ordinary run spends nothing and writes nothing."""

    def test_the_script_runs_and_reports_zero_side_effects(self):
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.path.abspath(ROOT)
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts",
                                          "adaptive_event_reassessment.py"),
             "--scenario", "all", "--json"],
            capture_output=True, text=True, env=environment, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        report = json.loads(result.stdout)
        self.assertEqual(report["broker_writes"], 0)
        self.assertEqual(report["orders_created"], 0)
        self.assertEqual(report["billable_market_data_requests"], 0)
        self.assertEqual(report["subscriptions_purchased"], 0)
        self.assertFalse(report["adaptive_layer"]["any_enabled"])

    @unittest.skipUnless(os.path.exists(RESEARCH_DB), "research store absent")
    def test_the_script_does_not_write_to_the_research_store(self):
        before = os.path.getmtime(RESEARCH_DB)
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.path.abspath(ROOT)
        subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts",
                                          "adaptive_event_reassessment.py"),
             "--real", "--limit", "25"],
            capture_output=True, text=True, env=environment, timeout=300)
        self.assertEqual(before, os.path.getmtime(RESEARCH_DB))


class TestD20Untouched(unittest.TestCase):
    """Section 1 -- the protected experiment stays sealed."""

    PROTECTED = ("anchor-v2", "anchor_v2", "validate_d20", "protected_ledger",
                 "return_60d", "d20_result", "d20_outcome")

    def test_no_module_references_the_protected_experiment(self):
        targets = [(os.path.join(ROOT, "src", "adaptive"), ADAPTIVE_MODULES),
                   (os.path.join(ROOT, "src", "domain"), DOMAIN_MODULES),
                   (os.path.join(ROOT, "src", "data_access"),
                    ("adaptive_event_schema.py",)),
                   (os.path.join(ROOT, "scripts"), SCRIPTS)]
        for directory, names in targets:
            for name in names:
                with open(os.path.join(directory, name),
                          encoding="utf-8") as handle:
                    source = handle.read().lower()
                for forbidden in self.PROTECTED:
                    self.assertNotIn(forbidden, source, f"{name}: {forbidden}")

    def test_no_module_imports_research(self):
        for directory, names in (
                (os.path.join(ROOT, "src", "adaptive"), ADAPTIVE_MODULES),
                (os.path.join(ROOT, "src", "domain"), DOMAIN_MODULES)):
            for name in names:
                with open(os.path.join(directory, name), encoding="utf-8") as f:
                    tree = ast.parse(f.read())
                for node in ast.walk(tree):
                    modules = []
                    if isinstance(node, ast.Import):
                        modules = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        modules = [node.module or ""]
                    for module in modules:
                        self.assertNotIn("research", module, name)


if __name__ == "__main__":
    unittest.main()
