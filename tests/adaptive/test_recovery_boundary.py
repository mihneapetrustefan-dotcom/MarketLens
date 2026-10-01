"""
tests/adaptive/test_recovery_boundary.py
--------------------------------------------------
Phase 25.9I-F.7 static boundary tests.

These read the source of the F.7 modules and assert what they cannot
do. A data-recovery phase touches the whole upstream pipeline, so the
cheapest way to be sure it cannot reach a broker, promote a model or
open the protected experiment is to prove the code contains no path to
any of them.

Structural, not behavioural, and deliberately so: a behavioural test
proves one call did not happen, while an absent import proves none can.
"""

import ast
import os
import re
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")

F7_MODULES = (
    os.path.join("src", "domain", "corpus_recovery_models.py"),
    os.path.join("src", "adaptive", "corpus_funnel.py"),
    os.path.join("src", "adaptive", "corpus_versioning.py"),
    os.path.join("src", "adaptive", "research_snapshot.py"),
)

F7_SCRIPTS = (
    os.path.join("scripts", "audit_historical_corpus.py"),
    os.path.join("scripts", "recover_historical_corpus.py"),
    os.path.join("scripts", "freeze_research_handoff.py"),
)

F7_ALL = F7_MODULES + F7_SCRIPTS

SOURCE_DB = os.path.join(ROOT, "data", "marketlens.db")
V2_DB = os.path.join(ROOT, "data", "marketlens_corpus_v2.db")


def read(relative: str) -> str:
    with open(os.path.join(ROOT, relative), encoding="utf-8") as handle:
        return handle.read()


def _blank_docstrings(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body[0].value.value = ""


def without_prose(relative: str) -> str:
    """
    The source with docstrings and `#` comments gone, but every other
    string literal kept -- so embedded SQL is still checkable.
    """
    tree = ast.parse(read(relative))
    _blank_docstrings(tree)
    return ast.unparse(tree)


def code_only(relative: str) -> str:
    """
    The source with docstrings, comments and *all* string literals gone:
    identifiers and structure only.

    A prohibition has to be checked against what the code can do, not
    against the prose that explains the prohibition. These modules state
    in their docstrings that they create no `OrderIntent` and promote no
    model, and a blunt substring search over the whole file flags its own
    explanation. Phase 25.9I-F.5 hit the same trap with a seal test; the
    structural assertion is kept and the wording is no longer the thing
    under test.
    """
    tree = ast.parse(read(relative))
    _blank_docstrings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    return ast.unparse(tree)


def imported_names(relative: str):
    """Every module name this file imports, flattened."""
    tree = ast.parse(read(relative))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            for alias in node.names:
                names.add(f"{node.module}.{alias.name}")
    return names


class TestNoBrokerPath(unittest.TestCase):
    """No order can be placed, created, cancelled or modified."""

    FORBIDDEN_IMPORTS = (
        "src.execution", "src.brokers", "src.paper", "src.trading",
        "src.ibkr", "ib_insync", "ibapi",
    )
    FORBIDDEN_SYMBOLS = (
        "OrderIntent", "place_order", "submit_order", "cancel_order",
        "modify_order", "ExecutionEngine", "BrokerClient", "PaperBroker",
        "IBKRClient", "market_snapshot",
    )

    def test_no_f7_file_imports_an_execution_or_broker_module(self):
        for relative in F7_ALL:
            names = imported_names(relative)
            for forbidden in self.FORBIDDEN_IMPORTS:
                for name in names:
                    self.assertFalse(
                        name == forbidden or name.startswith(forbidden + "."),
                        f"{relative} imports {name}")

    def test_no_f7_file_names_an_order_or_broker_symbol(self):
        for relative in F7_ALL:
            source = code_only(relative)
            for forbidden in self.FORBIDDEN_SYMBOLS:
                self.assertNotIn(forbidden, source,
                                 f"{relative} mentions {forbidden}")

    def test_no_f7_file_writes_to_an_order_or_execution_table(self):
        tables = ("order_intents", "execution_orders", "paper_orders",
                  "simulated_orders", "execution_fills", "paper_fills",
                  "order_state_history", "execution_audit")
        for relative in F7_ALL:
            source = without_prose(relative).lower()
            for table in tables:
                self.assertNotIn(table, source, f"{relative}: {table}")


class TestNoModelPromotion(unittest.TestCase):
    """Nothing here can promote, train or score a model."""

    FORBIDDEN = (
        "promote_model", "train_and_evaluate", "ModelingEngine",
        "evaluate_fold", "PRODUCTION_ACTIVE", "production_active",
        "ModelStatus.ACTIVE", "fit(", "predict(",
    )

    def test_no_f7_file_can_promote_or_fit_a_model(self):
        for relative in F7_ALL:
            source = code_only(relative)
            for forbidden in self.FORBIDDEN:
                self.assertNotIn(forbidden, source,
                                 f"{relative} mentions {forbidden}")

    def test_no_f7_file_imports_the_modeling_engine(self):
        for relative in F7_ALL:
            names = imported_names(relative)
            for name in names:
                self.assertFalse(name.startswith("src.modeling.engine"),
                                 f"{relative} imports {name}")

    def test_no_f7_file_writes_to_a_model_table(self):
        """
        The recovery clears these tables in its own new file, which is
        the opposite of writing a model into them -- so the clearing
        list is allowed to name them, and only an INSERT or UPDATE is
        forbidden.
        """
        for relative in F7_ALL:
            source = without_prose(relative)
            for table in ("trained_models", "model_evaluations",
                          "model_baseline_comparisons"):
                for statement in (f"INSERT INTO {table}",
                                  f"INSERT OR REPLACE INTO {table}",
                                  f"UPDATE {table}"):
                    self.assertNotIn(statement, source,
                                     f"{relative}: {statement}")


class TestProtectedExperimentSealed(unittest.TestCase):
    """D20 is not run, read, inferred or consumed."""

    FORBIDDEN = (
        "validate_d20_reversal", "anchor_v2", "anchor-v2",
        "build_anchor_v2_labels", "d20.abnormal_return", "d20.raw_return",
        "check_d20_readiness", "return_60d",
    )

    def test_no_f7_file_references_the_protected_validator_or_labels(self):
        for relative in F7_ALL:
            source = without_prose(relative)
            for forbidden in self.FORBIDDEN:
                self.assertNotIn(forbidden, source,
                                 f"{relative} mentions {forbidden}")

    def test_no_f7_file_reads_a_label_value_column(self):
        """
        `value_json` is where every label outcome lives, protected or
        not. A module that never names it cannot read one.
        """
        for relative in F7_MODULES:
            self.assertNotIn("value_json", without_prose(relative), relative)

    def test_no_f7_file_restates_the_frozen_hypothesis(self):
        for relative in F7_ALL:
            lowered = read(relative).lower()  # prose included: it must not say it either
            for fragment in ("market.return_60d", "abnormal return at d20",
                             "d20 reversal", "negative relation"):
                self.assertNotIn(fragment, lowered, f"{relative}: {fragment}")

    def test_the_protected_horizon_is_named_only_to_be_excluded(self):
        source = read(os.path.join("src", "adaptive", "research_snapshot.py"))
        self.assertIn('PROTECTED_HORIZONS: Tuple[str, ...] = ("d20",)', source)
        self.assertIn("is_protected", source)

    def test_no_f7_file_imports_the_protected_research_package(self):
        """
        `src/research/protected_ledger.py` is where the protected
        machinery lives. F.7 imports `src.domain.research_models` for
        `DatasetVersion` -- a generic contract shared by the whole
        pipeline -- and must not reach into the package itself.
        """
        for relative in F7_ALL:
            names = imported_names(relative)
            for name in names:
                for forbidden in ("src.research", "research.protected_ledger",
                                  "protected_ledger"):
                    self.assertFalse(
                        name == forbidden or name.startswith(forbidden + "."),
                        f"{relative} imports {name}")

    def test_the_only_research_import_is_the_generic_domain_contract(self):
        from src.domain import research_models
        self.assertTrue(hasattr(research_models, "DatasetVersion"))
        source = code_only(os.path.join("src", "adaptive",
                                        "corpus_versioning.py"))
        self.assertIn("DatasetVersion", source)
        self.assertNotIn("ProtectedLedger", source)

    def test_asking_the_recovery_about_the_protected_horizon_is_refused(self):
        from src.adaptive.research_snapshot import effective_sample
        conn = sqlite3.connect(":memory:")
        try:
            with self.assertRaises(ValueError):
                effective_sample(conn, "d20")
        finally:
            conn.close()


class TestOldArtifactsNotMutated(unittest.TestCase):
    """The protected artifact is never opened for writing."""

    def test_the_recovery_refuses_to_target_its_own_source(self):
        from scripts.recover_historical_corpus import RecoveryError, prepare_target
        with self.assertRaises(RecoveryError):
            prepare_target(SOURCE_DB, SOURCE_DB)

    def test_the_audit_script_opens_the_database_read_only(self):
        source = without_prose(os.path.join("scripts",
                                            "audit_historical_corpus.py"))
        self.assertIn("mode=ro", source)
        self.assertNotIn("sqlite3.connect(args.db)", source)

    def test_the_recovery_reads_its_source_only_through_a_read_only_helper(self):
        source = without_prose(os.path.join("scripts",
                                            "recover_historical_corpus.py"))
        self.assertIn("def _ro(path: str)", source)
        self.assertIn("mode=ro", source)
        writes = re.findall(r"sqlite3\.connect\(([^)]*)\)", source)
        for call in writes:
            if "mode=ro" in call:
                continue
            self.assertIn("target_db", call,
                          f"a writable connection to something other than the "
                          f"target: {call}")

    def test_no_f7_file_writes_to_the_v1_snapshot_tables_in_the_source(self):
        """
        The recovery writes research tables only inside the new file. The
        guarantee that matters is that `source_db` is never passed to a
        writable connection, which the previous test establishes; this
        one checks no DROP or ALTER exists anywhere.
        """
        for relative in F7_ALL:
            source = without_prose(relative).upper()
            self.assertNotIn("DROP TABLE", source, relative)
            self.assertNotIn("ALTER TABLE", source, relative)

    def test_the_registry_schema_is_additive_only(self):
        source = without_prose(os.path.join("src", "adaptive",
                                            "corpus_versioning.py"))
        creates = re.findall(r"CREATE (?:TABLE|INDEX)[^(]*", source)
        self.assertTrue(creates)
        for statement in creates:
            self.assertIn("IF NOT EXISTS", statement, statement)

    def test_overwriting_a_frozen_manifest_is_refused(self):
        from src.adaptive.corpus_versioning import _guard_immutable
        from src.domain.corpus_recovery_models import (
            ArtifactState, FrozenArtifactError)
        with self.assertRaises(FrozenArtifactError):
            _guard_immutable("x", ("frozen", "aaaa"), ArtifactState.FROZEN,
                             "bbbb")

    def test_only_eligibility_may_advance_on_a_frozen_artifact(self):
        from src.adaptive.corpus_versioning import _guard_immutable
        from src.domain.corpus_recovery_models import (
            ArtifactState, FrozenArtifactError)
        self.assertFalse(_guard_immutable(
            "x", ("frozen", "aaaa"), ArtifactState.ELIGIBLE_FOR_RESEARCH, "aaaa"))
        self.assertTrue(_guard_immutable(
            "x", ("frozen", "aaaa"), ArtifactState.FROZEN, "aaaa"))
        with self.assertRaises(FrozenArtifactError):
            _guard_immutable("x", ("frozen", "aaaa"), ArtifactState.BUILDING,
                             "aaaa")


class TestNoSecondPipeline(unittest.TestCase):
    """
    Recovery sequences the existing stages. It does not reimplement
    entity resolution, extraction or fusion.
    """

    def test_the_recovery_invokes_the_existing_stage_scripts(self):
        from scripts.recover_historical_corpus import STAGES
        invoked = {argv[0] for _, argv in STAGES}
        self.assertEqual(invoked, {
            "scripts/backfill_article_entities.py",
            "scripts/populate_events.py",
            "scripts/fuse_events.py",
            "scripts/build_event_studies.py",
            "scripts/build_research_observations.py",
            "scripts/compute_features.py",
        })

    def test_no_stage_is_invoked_with_a_publication_floor(self):
        """The `--since` flag is the root cause. It must not reappear."""
        from scripts.recover_historical_corpus import STAGES
        for _, argv in STAGES:
            for flag in argv:
                self.assertNotIn("--since", flag)
                self.assertNotIn("--limit", flag)

    def test_the_recovery_does_not_import_an_entity_or_event_extractor(self):
        forbidden = ("EntityResolver", "EventExtractor", "EventFusion",
                     "FusionEngine", "EventStudyEngine")
        source = code_only(os.path.join("scripts", "recover_historical_corpus.py"))
        for name in forbidden:
            self.assertNotIn(name, source, f"recovery mentions {name}")

    def test_the_funnel_module_defines_no_resolution_logic(self):
        source = code_only(os.path.join("src", "adaptive", "corpus_funnel.py"))
        for name in ("EntityResolver", "resolve(", "COMPANY_REGISTRY"):
            self.assertNotIn(name, source)


class TestNoCostAndNoNetwork(unittest.TestCase):
    """The recovery is local and free, and provably so."""

    FORBIDDEN_IMPORTS = ("requests", "urllib", "urllib.request", "http",
                         "http.client", "httpx", "aiohttp", "socket",
                         "openai", "anthropic", "boto3")

    def test_no_f7_file_imports_a_network_or_llm_client(self):
        for relative in F7_ALL:
            names = imported_names(relative)
            for forbidden in self.FORBIDDEN_IMPORTS:
                self.assertNotIn(forbidden, names, f"{relative}: {forbidden}")

    def test_no_f7_file_names_a_paid_provider(self):
        for relative in F7_ALL:
            lowered = without_prose(relative).lower()
            for provider in ("api_key", "apikey", "polygon.io",
                             "alphavantage", "finnhub.io", "bearer "):
                self.assertNotIn(provider, lowered, f"{relative}: {provider}")

    def test_the_plan_reports_zero_external_cost(self):
        from scripts.recover_historical_corpus import build_plan
        if not os.path.exists(SOURCE_DB):
            self.skipTest("source corpus absent")
        cost = build_plan(SOURCE_DB)["external_cost"]
        self.assertEqual(cost["paid_api_calls"], 0)
        self.assertEqual(cost["llm_calls"], 0)
        self.assertEqual(cost["network_calls"], 0)


class TestNoProductionBehaviourChange(unittest.TestCase):
    """Portfolio, Risk and capture are not touched."""

    def test_no_f7_file_imports_portfolio_risk_or_capture(self):
        forbidden = ("src.portfolio", "src.risk", "src.capture",
                     "src.signals", "src.impact")
        for relative in F7_ALL:
            names = imported_names(relative)
            for prefix in forbidden:
                for name in names:
                    self.assertFalse(
                        name == prefix or name.startswith(prefix + "."),
                        f"{relative} imports {name}")

    def test_f7_adds_no_feature_flag_that_could_enable_anything(self):
        for relative in F7_ALL:
            lowered = code_only(relative).lower()
            for pattern in ("enable_broker", "allow_broker", "live_trading",
                            "enable_live", "feature_flag"):
                self.assertNotIn(pattern, lowered, f"{relative}: {pattern}")

    def test_f7_defines_no_qualification_gate_of_its_own(self):
        """
        `research_snapshot` may *reference* the canonical gate and alias
        the canonical floor. It may not assign a numeric threshold to
        either name.
        """
        source = without_prose(os.path.join("src", "adaptive",
                                            "research_snapshot.py"))
        self.assertIsNone(re.search(r"MIN_EFFECTIVE_SAMPLE\s*=\s*\d", source))
        self.assertIsNone(re.search(r"MIN_VALID_FOLDS\s*=\s*\d", source))
        self.assertIn("from src.adaptive.sample_adequacy import", source)
        self.assertIn("ModelEvaluation.is_deployable", source)


if __name__ == "__main__":
    unittest.main()
