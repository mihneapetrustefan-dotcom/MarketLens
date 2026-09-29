"""
tests/adaptive/test_allocation_boundary.py
-------------------------------------------------------
Phase 25.9I-D: the allocator proposes, and cannot do anything else.

Three groups: as-of safety (no future data reaches a plan), the
Portfolio/Risk boundary (the canonical gate still decides), and
persistence (idempotent, append-only, adaptive tables only).
"""

import ast
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.adaptive.allocation_repository import AdaptiveAllocationRepository
from src.adaptive.allocator import AllocationConfig, SmartCapitalAllocator
from src.adaptive.correlation import AsOfCorrelationProvider
from src.adaptive.desirability import DesirabilityEngine
from src.adaptive.risk_budget import PositionRiskEstimator, RiskConfig
from src.adaptive.universe import MarketHistory
from src.data_access.adaptive_allocation_schema import (
    ADAPTIVE_ALLOCATION_TABLES, initialize_adaptive_allocation_schema,
)
from src.domain.allocation_models import (
    AllocationCandidate, PortfolioCapitalState,
)
from tests.adaptive.test_capital_allocation import AT, allocator, candidate, state
from tests.adaptive.test_dynamic_universe import build_market

UTC = timezone.utc
ROOT = os.path.join(os.path.dirname(__file__), "..", "..")

ALLOCATION_MODULES = ("allocator.py", "desirability.py", "risk_budget.py",
                      "correlation.py", "allocation_repository.py")


class _Market(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = build_market(
            os.path.join(self.dir, "market.db"),
            {"a": {"daily_drift": 0.001}, "b": {"daily_drift": 0.0005},
             "benchmark-spy": {"volume": 50_000_000}},
            start=datetime(2026, 5, 1, tzinfo=UTC), days=200)

    def candidates_from(self, db, as_of):
        """Build candidates the way the CLI does, from as-of history."""
        engine, estimator = DesirabilityEngine(), PositionRiskEstimator()
        built = []
        with MarketHistory(db, as_of) as history:
            for instrument_id in ("a", "b"):
                candles = history.history(instrument_id)
                volatility = candles.realized_volatility()
                liquidity = candles.median_dollar_volume()
                estimate = estimator.estimate(instrument_id,
                                              daily_volatility=volatility)
                built.append(AllocationCandidate(
                    instrument_id=instrument_id, as_of=as_of,
                    desirability=engine.assess(
                        instrument_id, opportunity_score=0.7,
                        score_completeness=0.5, liquidity_notional=liquidity,
                        volatility=volatility, existing_exposure_fraction=0.0,
                        sector_exposure_fraction=0.0,
                        risk_is_known=estimate.is_usable),
                    risk=estimate, sector_id="technology"))
        return built


class TestAsOfSafety(_Market):

    def test_future_candles_cannot_change_a_plan(self):
        as_of = datetime(2026, 8, 1, 13, 25, tzinfo=UTC)
        truncated = os.path.join(self.dir, "truncated.db")
        source = sqlite3.connect(self.db)
        source.execute("ATTACH DATABASE ? AS trimmed", (truncated,))
        source.execute("""CREATE TABLE trimmed.price_candle_cache AS
                          SELECT * FROM price_candle_cache WHERE timestamp <= ?""",
                       (as_of.isoformat(),))
        source.commit()
        source.close()

        with_future = allocator().allocate(
            self.candidates_from(self.db, as_of), state(1000.0), as_of)
        without_future = allocator().allocate(
            self.candidates_from(truncated, as_of), state(1000.0), as_of)

        self.assertEqual(with_future.plan_id, without_future.plan_id)
        self.assertEqual(
            {l.instrument_id: l.allocated_notional for l in with_future.lines},
            {l.instrument_id: l.allocated_notional for l in without_future.lines})
        self.assertEqual(
            [l.risk.risk_per_dollar for l in with_future.lines],
            [l.risk.risk_per_dollar for l in without_future.lines])

    def test_volatility_inputs_are_bounded_by_as_of(self):
        early = datetime(2026, 6, 1, tzinfo=UTC)
        late = datetime(2026, 10, 1, tzinfo=UTC)
        with MarketHistory(self.db, early) as history:
            early_count = history.history("a").count
        with MarketHistory(self.db, late) as history:
            late_count = history.history("a").count
        self.assertLess(early_count, late_count)

    def test_correlations_are_bounded_by_as_of(self):
        as_of = datetime(2026, 7, 1, tzinfo=UTC)
        with MarketHistory(self.db, as_of) as history:
            provider = AsOfCorrelationProvider(history)
            result = provider.correlation("a", "b")
            series = provider._series("a")
        self.assertTrue(all(stamp <= as_of.isoformat() for stamp, _ in series))
        self.assertIsInstance(result.observations, int)

    def test_missing_correlation_is_not_zero(self):
        as_of = datetime(2026, 5, 10, tzinfo=UTC)          # only a few candles
        with MarketHistory(self.db, as_of) as history:
            result = AsOfCorrelationProvider(history).correlation("a", "b")
        self.assertIsNone(result.value)
        self.assertFalse(result.is_measured)
        self.assertIn("below the minimum", result.reason)

    def test_an_unmeasured_correlation_gives_no_diversification_credit(self):
        as_of = datetime(2026, 8, 1, 13, 25, tzinfo=UTC)
        with MarketHistory(self.db, as_of) as history:
            provider = AsOfCorrelationProvider(history, min_observations=999)
            # The fixture's smooth series has ~zero volatility, so risk is
            # UNKNOWN; without a cap for unknown risk nothing is funded at
            # all, which is correct but tests a different rule than this one.
            plan = SmartCapitalAllocator(
                AllocationConfig(max_pairwise_correlation=0.8,
                                 minimum_allocation_usd=0.0),
                RiskConfig(unknown_risk_notional_cap_usd=50.0)).allocate(
                self.candidates_from(self.db, as_of),
                state(1000.0, holds=()), as_of, correlations=provider)
        funded = [l for l in plan.lines if l.allocated_notional > 0]
        self.assertTrue(funded)
        # No correlation cut can be claimed from an unmeasured pair.
        for line in funded:
            self.assertFalse(any(c.constraint == "max_pairwise_correlation"
                                 for c in line.constraints_applied))

    def test_no_protected_experiment_artefact_is_referenced(self):
        base = os.path.join(ROOT, "src", "adaptive")
        for name in ALLOCATION_MODULES:
            with open(os.path.join(base, name), encoding="utf-8") as handle:
                source = handle.read().lower()
            for forbidden in ("anchor_v2", "anchor-v2", "validate_d20",
                              "protected_ledger", "return_60d"):
                self.assertNotIn(forbidden, source, f"{name} mentions {forbidden}")


class TestPortfolioRiskBoundary(unittest.TestCase):

    def test_no_allocation_module_imports_execution_trading_or_portfolio(self):
        base = os.path.join(ROOT, "src", "adaptive")
        for name in ALLOCATION_MODULES:
            with open(os.path.join(base, name), encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                for module in modules:
                    for forbidden in ("src.execution", "src.trading",
                                      "src.portfolio"):
                        self.assertFalse(module.startswith(forbidden),
                                         f"src/adaptive/{name} imports {module}")

    def test_the_allocator_produces_no_order_intent(self):
        plan = allocator().allocate([candidate("a", 0.9)], state(100.0), AT)
        for forbidden in ("order_intent", "intent", "submit", "execute"):
            self.assertFalse(hasattr(plan, forbidden))
        for line in plan.lines:
            self.assertFalse(hasattr(line, "is_executable"))

    def test_the_canonical_risk_gate_still_refuses_an_unapproved_decision(self):
        # The allocator cannot make a rejected decision executable: the
        # canonical boundary is unchanged and still raises.
        from src.domain.portfolio_models import (
            OrderIntent, RiskDecision, RiskDecisionState,
        )
        rejected = RiskDecision(
            decision_id="rd-1", portfolio_id="p", as_of=AT,
            state=RiskDecisionState.REJECTED)
        with self.assertRaises(ValueError):
            OrderIntent.require_approval(rejected)

    def test_a_plan_cannot_be_converted_into_an_order_by_this_phase(self):
        # No module in src/adaptive constructs an OrderIntent or an
        # AllocationProposal: the hand-off does not exist yet, by design.
        base = os.path.join(ROOT, "src", "adaptive")
        for name in os.listdir(base):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(base, name), encoding="utf-8") as handle:
                source = handle.read()
            for forbidden in ("OrderIntent(", "AllocationProposal(",
                              "TargetPosition("):
                self.assertNotIn(forbidden, source, f"{name} builds {forbidden}")

    def test_no_allocation_module_calls_a_broker_method(self):
        base = os.path.join(ROOT, "src", "adaptive")
        for name in ALLOCATION_MODULES:
            with open(os.path.join(base, name), encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            calls = {node.func.attr for node in ast.walk(tree)
                     if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute)}
            for forbidden in ("place_order", "submit_order", "cancel_order",
                              "modify_order", "market_snapshot", "subscribe"):
                self.assertNotIn(forbidden, calls, f"{name} calls {forbidden}")

    def test_the_smart_allocation_flag_defaults_off(self):
        from src.adaptive import flags as adaptive_flags
        self.assertFalse(
            adaptive_flags.resolve({}).smart_capital_allocation_enabled)
        self.assertFalse(
            adaptive_flags.AdaptiveFlags().smart_capital_allocation_enabled)

    def test_legacy_position_sizing_is_untouched(self):
        # The canonical sizing strategies remain the only production path
        # and are not subclassed, wrapped or monkey-patched here.
        from src.portfolio.sizing import FixedFractionSizing, PositionSizingStrategy
        self.assertTrue(issubclass(FixedFractionSizing, PositionSizingStrategy))
        base = os.path.join(ROOT, "src", "adaptive")
        for name in os.listdir(base):
            if name.endswith(".py"):
                with open(os.path.join(base, name), encoding="utf-8") as handle:
                    self.assertNotIn("PositionSizingStrategy", handle.read())


class TestPersistence(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.conn = sqlite3.connect(os.path.join(self.dir, "store.db"))
        self.repository = AdaptiveAllocationRepository(self.conn)
        self.plan = allocator(account_risk_budget_usd=5.0).allocate(
            [candidate("a", 0.9), candidate("b", 0.6), candidate("c", 0.2)],
            state(1000.0), AT)

    def tearDown(self):
        self.conn.close()

    def test_initialization_creates_only_its_own_tables(self):
        fresh = sqlite3.connect(":memory:")
        initialize_adaptive_allocation_schema(fresh)
        tables = {r[0] for r in fresh.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, set(ADAPTIVE_ALLOCATION_TABLES))
        fresh.close()

    def test_saving_twice_writes_one_plan(self):
        self.assertTrue(self.repository.save_plan(self.plan, "su-test"))
        self.assertFalse(self.repository.save_plan(self.plan, "su-test"))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM adaptive_allocation_plans").fetchone()[0], 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM adaptive_allocation_lines").fetchone()[0],
            len(self.plan.lines))

    def test_a_revision_preserves_the_earlier_plan(self):
        self.repository.save_plan(self.plan, "su-test")
        later = AT + timedelta(hours=2)
        revised = allocator(account_risk_budget_usd=5.0).allocate(
            [candidate("a", 0.9)], state(1000.0), later, revision=1,
            previous_plan_id=self.plan.plan_id)
        self.repository.save_plan(revised, "su-test")

        revisions = self.repository.revisions("su-test")
        self.assertEqual([r["revision"] for r in revisions], [0, 1])
        self.assertEqual(revisions[1]["previous_plan_id"], self.plan.plan_id)
        stored = self.repository.plan(self.plan.plan_id)
        self.assertEqual(len(stored["lines"]), len(self.plan.lines))

    def test_reasons_and_constraints_survive_the_round_trip(self):
        self.repository.save_plan(self.plan, "su-test")
        stored = self.repository.plan(self.plan.plan_id)
        for line in stored["lines"]:
            if line["allocated_notional"] == 0:
                self.assertTrue(line["reasons"] or line["constraints_applied"],
                                line["instrument_id"])

    def test_lineage_to_the_session_universe_is_stored(self):
        self.repository.save_plan(self.plan, "su-test")
        rows = self.conn.execute(
            "SELECT DISTINCT session_snapshot_id FROM adaptive_allocation_lines"
        ).fetchall()
        self.assertEqual(rows, [("su-test",)])

    def test_the_repository_writes_only_allocation_tables(self):
        with open(os.path.join(ROOT, "src", "adaptive",
                               "allocation_repository.py"), encoding="utf-8") as handle:
            source = handle.read()
        for statement in ("INSERT INTO", "INSERT OR IGNORE INTO", "UPDATE ",
                          "DELETE FROM"):
            index = 0
            while True:
                index = source.find(statement, index)
                if index == -1:
                    break
                fragment = source[index + len(statement):index + len(statement) + 40]
                self.assertTrue(fragment.strip().startswith("adaptive_allocation"),
                                f"writes outside its tables: {fragment!r}")
                index += 1


class TestShadowCli(unittest.TestCase):

    def test_the_cli_is_dry_run_by_default_and_writes_nothing(self):
        market_db = os.path.join(ROOT, "data", "marketlens.db")
        before = os.path.getmtime(market_db)
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.path.abspath(ROOT)
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "adaptive_allocation.py"),
             "--as-of", "2026-08-27", "--equity", "250", "--cash", "250",
             "--account-risk-budget", "8", "--json"],
            capture_output=True, text=True, env=environment, timeout=900)
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        report = json.loads(result.stdout)
        self.assertEqual(report["mode"], "dry-run")
        self.assertFalse(report["flag_smart_capital_allocation_enabled"])
        self.assertTrue(report["identity_reconciles"])
        self.assertEqual(os.path.getmtime(market_db), before)


if __name__ == "__main__":
    unittest.main()
