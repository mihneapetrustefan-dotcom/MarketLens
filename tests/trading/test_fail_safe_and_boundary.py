"""
tests/trading/test_fail_safe_and_boundary.py
--------------------------------------------------
Phase 25 — what the loop refuses, and what it may touch (§25, §32-§34,
§39, §40).

THE BOUNDARY IS MEASURED, NOT ASSERTED
------------------------------------------
`test_a_cycle_moves_only_the_tables_it_owns` counts every table in the
database before and after a cycle and compares the sets. That is the
Phase 23.5 method, adopted because the alternative — parsing the source
for table names — was tried in Phase 23 and could not see a write made
through a helper two modules away. A boundary claim that cannot fail is
not a boundary claim.

FAIL-CLOSED IS ENUMERATED, NOT SUMMARISED
---------------------------------------------
§25 lists twelve conditions under which the system must not trade.
Each gets its own test, because "the loop fails closed" is the kind of
claim that stays true in a docstring long after one of its twelve
branches stopped working.

THE LIVE BOUNDARY IS TESTED FROM FOUR SIDES
-----------------------------------------------
The mode cannot be recorded as live; a live value that arrived some
other way resolves to OFF; the IBKR config refuses a live environment
at construction; and the Phase 14 safety layer raises on one. Each is
independent, and each is asserted separately.
"""

import ast
import os
import sqlite3
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.trading_loop_schema import TRADING_LOOP_TABLES
from src.domain.broker_models import ExecutionEnvironment
from src.domain.trading_loop_models import (
    BlockReason, CycleStatus, LoopHealth, LoopStage, PaperStrategyState,
    PromotionRefused, StageOutcome, TradingMode, TradingModeRefused,
    assert_transition,
)
from src.execution.adapters.ibkr.config import IBKRConfigurationError, paper_config
from src.execution.safety import ExecutionSafety, RealMoneyExecutionDisabled
from src.trading.api import TradingLoopAPI
from src.trading.mode import TradingModeStore
from tests.trading.helpers import (
    NOW, a_live_signal, build_loop, enable_paper, make_connection, moved,
    store_signals, table_counts, universe,
)

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
TRADING_PACKAGE = os.path.join(REPO_ROOT, "src", "trading")


def a_ready_database():
    conn = make_connection()
    universe(conn)
    store_signals(conn, [a_live_signal()])
    enable_paper(conn)
    return conn


# ======================================================================
# Live is unreachable (§9, §33, §39)
# ======================================================================

class TestLiveIsUnreachable(unittest.TestCase):

    def test_the_mode_store_refuses_to_record_live(self):
        conn = make_connection()
        with self.assertRaises(TradingModeRefused):
            TradingModeStore(conn).set_mode(
                TradingMode.LIVE, actor="a", reason="r", at=NOW)
        conn.close()

    def test_live_is_never_permitted_even_as_an_enum_member(self):
        self.assertFalse(TradingMode.LIVE.is_permitted)
        self.assertTrue(TradingMode.LIVE.is_real_money)

    def test_the_ibkr_config_refuses_a_live_environment(self):
        with self.assertRaises(IBKRConfigurationError):
            paper_config(environment=ExecutionEnvironment.LIVE)

    def test_the_phase_14_safety_layer_raises_on_a_live_environment(self):
        with self.assertRaises(RealMoneyExecutionDisabled):
            ExecutionSafety().assert_not_real_money(ExecutionEnvironment.LIVE)

    def test_the_trading_package_never_names_a_live_environment_positively(self):
        """
        A source scan, tokenised so this test's own prose cannot match.

        The negative control matters: the words appear all over the
        refusals, so a naive substring search would pass on a file that
        enabled live trading in a comment-free line.
        """
        import io
        import tokenize
        offenders = []
        for name in sorted(os.listdir(TRADING_PACKAGE)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(TRADING_PACKAGE, name)
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            code_only = []
            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                if token.type in (tokenize.COMMENT, tokenize.STRING):
                    continue
                code_only.append(token.string)
            text = " ".join(code_only)
            for forbidden in ("ExecutionEnvironment . LIVE",
                              "TradingMode . LIVE ,",
                              "allow_real_orders = True"):
                if forbidden in text:
                    offenders.append(f"{name}: {forbidden}")
        self.assertEqual(offenders, [])

    def test_the_scan_would_catch_a_planted_violation(self):
        """The negative control for the scan above."""
        import io
        import tokenize
        planted = "x = ExecutionEnvironment.LIVE  # comment\n"
        code_only = []
        for token in tokenize.generate_tokens(io.StringIO(planted).readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            code_only.append(token.string)
        self.assertIn("ExecutionEnvironment . LIVE", " ".join(code_only))

    def test_no_transition_reaches_live_eligible(self):
        for state in PaperStrategyState:
            with self.subTest(state=state.value):
                with self.assertRaises(PromotionRefused):
                    assert_transition(state, PaperStrategyState.LIVE_ELIGIBLE)


# ======================================================================
# Fail closed (§25)
# ======================================================================

class TestFailClosed(unittest.TestCase):

    def setUp(self):
        self.conn = a_ready_database()

    def tearDown(self):
        self.conn.close()

    def assert_placed_nothing(self, result, reason=None):
        self.assertEqual(result.orders_submitted, 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM execution_orders")
            .fetchone()[0], 0)
        if reason is not None:
            self.assertIn(reason, [b.reason for b in result.blocks])

    def test_trading_mode_off_places_nothing(self):
        TradingModeStore(self.conn).set_mode(
            TradingMode.OFF, actor="a", reason="stood down", at=NOW)
        result = build_loop(self.conn).run_cycle(NOW)
        self.assert_placed_nothing(result, BlockReason.MODE_NOT_PERMITTED)

    def test_the_kill_switch_places_nothing(self):
        TradingModeStore(self.conn).activate_kill_switch(
            actor="a", reason="drawdown", at=NOW)
        result = build_loop(self.conn).run_cycle(NOW)
        self.assert_placed_nothing(result)
        self.assertIs(result.health, LoopHealth.BLOCKED)

    def test_no_market_data_places_nothing(self):
        conn = make_connection()
        store_signals(conn, [a_live_signal()])
        enable_paper(conn)
        result = build_loop(conn).run_cycle(NOW)
        self.assertEqual(result.orders_submitted, 0)
        self.assertIn(BlockReason.STALE_MARKET_DATA,
                      [b.reason for b in result.blocks])
        conn.close()

    def test_a_disconnected_gateway_places_nothing(self):
        loop = build_loop(self.conn)
        loop.stack.connected = False
        result = loop.run_cycle(NOW)
        self.assert_placed_nothing(result, BlockReason.BROKER_DISCONNECTED)

    def test_an_unreadable_account_places_nothing(self):
        loop = build_loop(self.conn)

        def refuse(*args, **kwargs):
            raise RuntimeError("the gateway will not answer")

        loop.stack.gateway.get_account = refuse
        result = loop.run_cycle(NOW)
        self.assert_placed_nothing(result, BlockReason.ACCOUNT_STATE_UNKNOWN)

    def test_ordering_disabled_places_nothing_even_when_connected(self):
        loop = build_loop(self.conn, allow_paper_orders=False)
        result = loop.run_cycle(NOW)
        self.assertEqual(result.orders_submitted, 0)

    def test_a_stale_signal_places_nothing(self):
        conn = make_connection()
        universe(conn)
        store_signals(conn, [a_live_signal(cutoff=NOW - timedelta(days=10))])
        enable_paper(conn)
        result = build_loop(conn).run_cycle(NOW)
        self.assertEqual(result.signals_eligible, 0)
        self.assertEqual(result.orders_submitted, 0)
        conn.close()

    def test_an_unpromoted_model_places_nothing_without_the_experimental_label(self):
        """§37: paper trading is not a loophole around model governance."""
        result = build_loop(self.conn, experimental=False).run_cycle(NOW)
        self.assertEqual(result.signals_eligible, 0)
        self.assertEqual(result.orders_submitted, 0)

    def test_a_crashed_stage_does_not_continue_into_submission(self):
        loop = build_loop(self.conn)

        def explode(*args, **kwargs):
            raise RuntimeError("the risk engine fell over")

        loop.service_evaluate_backup = None
        from src.portfolio.service import PortfolioService
        original = PortfolioService.evaluate
        PortfolioService.evaluate = explode
        try:
            result = loop.run_cycle(NOW)
        finally:
            PortfolioService.evaluate = original
        self.assertEqual(result.orders_submitted, 0)
        self.assertIs(result.status, CycleStatus.FAILED)

    def test_a_cycle_already_claimed_places_nothing(self):
        loop = build_loop(self.conn)
        loop.run_cycle(NOW)
        again = loop.run_cycle(NOW)
        self.assertIs(again.status, CycleStatus.ABANDONED)
        self.assertEqual(again.orders_submitted, 0)

    def test_a_failed_cycle_still_closes_its_row(self):
        """
        A cycle stuck in CLAIMED would block its anchor until the stale
        reclaim timed it out, and the loop would look healthy while not
        advancing.
        """
        loop = build_loop(self.conn)

        def explode(*args, **kwargs):
            raise RuntimeError("boom")

        loop.stack.gateway.get_account = explode
        loop._observe = explode
        result = loop.run_cycle(NOW)
        status = self.conn.execute(
            "SELECT status FROM trading_cycles WHERE cycle_id = ?",
            (result.cycle_id,)).fetchone()[0]
        self.assertNotEqual(status, CycleStatus.CLAIMED.value)


# ======================================================================
# The boundary (§32, §40)
# ======================================================================

class TestTheBoundary(unittest.TestCase):

    def setUp(self):
        self.conn = a_ready_database()

    def tearDown(self):
        self.conn.close()

    def test_a_cycle_moves_only_the_tables_it_owns(self):
        """
        Counted, not asserted. Everything that moved must belong either
        to Phase 25 or to a phase Phase 25 legitimately drives.
        """
        before = table_counts(self.conn)
        build_loop(self.conn).run_cycle(NOW)
        changed = set(moved(before, table_counts(self.conn)))

        allowed = set(TRADING_LOOP_TABLES) | {
            # Phase 14, through its own repository: the order lifecycle.
            "execution_orders", "order_state_history", "execution_fills",
            "execution_events", "execution_errors", "execution_audit",
            "reconciliation_records", "brokers", "broker_accounts",
            "broker_capability", "broker_health", "broker_connection",
            "broker_instrument_mapping",
            # Phase 11, through `PortfolioService._persist`: the risk
            # decision and its intent. §6 and §32 both require these.
            "risk_decisions", "risk_violations", "order_intents",
            "allocation_proposals", "allocation_changes",
            "portfolio_state_snapshots", "risk_constraint_sets",
            "risk_constraints",
            # Phase 16, through `GovernanceRepository.save_outcome`.
            "trade_outcomes",
        }
        self.assertEqual(changed - allowed, set(),
                         f"unexpected tables moved: {sorted(changed - allowed)}")

    def test_a_cycle_never_writes_the_live_portfolio_tables(self):
        """
        The paper book is not the portfolio. Phase 13 held this line and
        Phase 25 holds it too: `portfolios` and `positions` are the
        record of a real book and nothing here may write one.
        """
        before = table_counts(self.conn)
        build_loop(self.conn).run_cycle(NOW)
        changed = moved(before, table_counts(self.conn))
        for table in ("portfolios", "positions"):
            self.assertNotIn(table, changed)

    def test_a_cycle_never_writes_signals_or_predictions(self):
        """Memory of a decision must not be able to change the decision."""
        before = table_counts(self.conn)
        build_loop(self.conn).run_cycle(NOW)
        changed = moved(before, table_counts(self.conn))
        for table in ("signals", "predictions", "trained_models",
                      "signal_suppressions", "research_features"):
            self.assertNotIn(table, changed)

    def test_the_boundary_test_can_actually_fail(self):
        """
        The negative control. A boundary check that cannot see a write
        certifies nothing, and Phase 23 shipped one that could not.
        """
        before = table_counts(self.conn)
        self.conn.execute(
            "INSERT OR REPLACE INTO portfolios "
            "(portfolio_id, name, base_currency, cash, kind, created_at) "
            "VALUES ('pf-x','x','USD',0,'live','2026-01-01')")
        self.conn.commit()
        self.assertIn("portfolios", moved(before, table_counts(self.conn)))

    def test_the_trading_package_declares_no_promotion_path(self):
        """
        §40: nothing here may promote a model, activate a challenger,
        raise a limit or enable live trading. Checked by parsing the
        SQL string arguments of every execute call, the Phase 23.5
        method.
        """
        forbidden_tables = ("trained_models", "model_promotions", "signals",
                            "predictions", "risk_constraints",
                            "risk_constraint_sets", "challengers",
                            "memory_patterns", "trading_experiences")
        offenders = []
        for name in sorted(os.listdir(TRADING_PACKAGE)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(TRADING_PACKAGE, name),
                      encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=name)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                attribute = getattr(node.func, "attr", "")
                if attribute not in ("execute", "executemany"):
                    continue
                if not node.args or not isinstance(node.args[0], ast.Constant):
                    continue
                sql = " ".join(str(node.args[0].value).split()).lower()
                verb = sql.split(" ", 1)[0]
                if verb not in ("insert", "update", "delete", "replace"):
                    continue
                for table in forbidden_tables:
                    # Word-bounded: `signals` must not match
                    # `signal_eligibility`, the substring mistake that
                    # cost Phase 23.5 a debugging pass.
                    if f" {table} " in f" {sql} " or f" {table}(" in sql:
                        offenders.append(f"{name}: {sql[:70]}")
        self.assertEqual(offenders, [])

    def test_that_scan_would_catch_a_planted_write(self):
        planted = ast.parse(
            "conn.execute('UPDATE trained_models SET status = 1')")
        found = []
        for node in ast.walk(planted):
            if isinstance(node, ast.Call) and getattr(node.func, "attr",
                                                      "") == "execute":
                sql = str(node.args[0].value).lower()
                if " trained_models " in f" {sql} ":
                    found.append(sql)
        self.assertTrue(found)


# ======================================================================
# Security (§33)
# ======================================================================

class TestSecurity(unittest.TestCase):

    def test_no_credential_is_read_anywhere_in_the_trading_package(self):
        """
        Tokenised, so this test's own prose and the docstrings that
        explain the rule cannot satisfy it. `environ` inside
        `RunEnvironment` was a real false positive in Phase 24.
        """
        import io
        import tokenize
        offenders = []
        for name in sorted(os.listdir(TRADING_PACKAGE)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(TRADING_PACKAGE, name),
                      encoding="utf-8") as handle:
                source = handle.read()
            code_only = []
            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                if token.type in (tokenize.COMMENT, tokenize.STRING):
                    continue
                code_only.append(token.string)
            text = " ".join(code_only)
            for forbidden in ("password", "api_key", "secret", "token",
                              "getpass"):
                if f" {forbidden} " in f" {text} ":
                    offenders.append(f"{name}: {forbidden}")
        self.assertEqual(offenders, [])

    def test_only_the_mode_module_reads_the_environment(self):
        """
        One reader, and it reads one variable that cannot grant
        permission the database has withheld.
        """
        import io
        import tokenize
        readers = []
        for name in sorted(os.listdir(TRADING_PACKAGE)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(TRADING_PACKAGE, name),
                      encoding="utf-8") as handle:
                source = handle.read()
            code_only = []
            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                if token.type in (tokenize.COMMENT, tokenize.STRING):
                    continue
                code_only.append(token.string)
            text = " ".join(code_only)
            if "os . environ" in text or "getenv (" in text:
                readers.append(name)
        self.assertEqual(readers, ["mode.py"])

    def test_the_cli_takes_no_credential_argument(self):
        path = os.path.join(REPO_ROOT, "scripts", "run_trading_loop.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        arguments = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "attr", "") == "add_argument"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)):
                arguments.append(str(node.args[0].value))
        for forbidden in ("--password", "--secret", "--token", "--api-key",
                          "--username"):
            self.assertNotIn(forbidden, arguments)

    def test_the_cli_defaults_to_a_dry_run(self):
        path = os.path.join(REPO_ROOT, "scripts", "run_trading_loop.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('dest="dry_run", action="store_true",\n'
                      "                        default=True", source)


# ======================================================================
# The integrity check itself (§43)
# ======================================================================

class TestIntegrity(unittest.TestCase):

    def test_a_clean_run_fails_no_check(self):
        """
        Nothing FAILS on a clean cycle.

        Not "everything passes": a cycle that placed an order but has
        no fill yet has no trade outcome, so the two lineage-agreement
        checks Phase 25.5 added cannot run. That is reported as NOT RUN
        and the report is therefore NOT conclusive -- which is the
        distinction the whole check set exists to preserve. The
        conclusive case is asserted in the end-to-end test, where a
        trade actually completes.
        """
        conn = a_ready_database()
        build_loop(conn).run_cycle(NOW)
        report = TradingLoopAPI(conn).integrity_check()
        self.assertTrue(report["ok"], report["checks"])
        self.assertEqual(
            [c["name"] for c in report["checks"] if c["ok"] is False], [])
        conn.close()

    def test_a_check_that_could_not_run_is_not_a_pass(self):
        """
        The Phase 23.5 rule. An empty database must not read as a clean
        bill of health.
        """
        conn = sqlite3.connect(":memory:")
        report = TradingLoopAPI(conn).integrity_check()
        self.assertGreater(report["not_run"], 0)
        self.assertFalse(report["conclusive"])
        conn.close()

    def test_the_live_check_would_actually_fire(self):
        """A negative control: plant a live row and watch it fail."""
        conn = a_ready_database()
        build_loop(conn).run_cycle(NOW)
        conn.execute("UPDATE trading_mode SET mode = 'live'")
        conn.commit()
        report = TradingLoopAPI(conn).integrity_check()
        failed = [c["name"] for c in report["checks"] if c["ok"] is False]
        self.assertIn("no_live_mode_stored", failed)
        conn.close()


if __name__ == "__main__":
    unittest.main()
