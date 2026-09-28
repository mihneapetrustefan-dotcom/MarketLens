"""
tests/adaptive/test_universe_persistence.py
------------------------------------------------------------
Phase 25.9I-B: what is written, what is never written.

The persistence tests care about three properties: a re-run writes no
duplicate, a revision does not erase its predecessor, and nothing
outside the five `adaptive_*` tables is touched. The last one is the
safety property -- a universe selector that could write an order row
would be a selector that could trade.
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

from src.adaptive.admission import AdmissionConfig
from src.adaptive.repository import AdaptiveUniverseRepository
from src.adaptive.selector import SessionUniverseSelector
from src.adaptive.universe import MarketHistory, ParentUniverse
from src.data_access.adaptive_universe_schema import (
    ADAPTIVE_UNIVERSE_TABLES, initialize_adaptive_universe_schema,
)
from src.domain.adaptive_models import AdmissionVerdict
from tests.adaptive.test_dynamic_universe import at, build_market, parent_of

UTC = timezone.utc
ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


class _Store(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.market_db = build_market(
            os.path.join(self.dir, "market.db"),
            {"a": {}, "b": {"volume": 4_000_000}, "c": {"volume": 3_000_000},
             "benchmark-spy": {"volume": 50_000_000}})
        self.conn = sqlite3.connect(os.path.join(self.dir, "store.db"))
        self.repository = AdaptiveUniverseRepository(self.conn)
        self.parent = parent_of("a", "b", "c")
        self.selector = SessionUniverseSelector(self.parent)

    def tearDown(self):
        self.conn.close()

    def select(self, as_of=None):
        as_of = as_of or at()
        with MarketHistory(self.market_db, as_of) as history:
            return self.selector.select(history, as_of, session_date="2026-08-27")


class TestSchema(_Store):

    def test_initialization_is_repeatable(self):
        initialize_adaptive_universe_schema(self.conn)
        initialize_adaptive_universe_schema(self.conn)
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ADAPTIVE_UNIVERSE_TABLES:
            self.assertIn(table, tables)

    def test_initialization_creates_nothing_outside_its_own_tables(self):
        fresh = sqlite3.connect(":memory:")
        initialize_adaptive_universe_schema(fresh)
        tables = {r[0] for r in fresh.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, set(ADAPTIVE_UNIVERSE_TABLES))
        fresh.close()

    def test_it_does_not_touch_an_existing_table(self):
        # A store that already holds an unrelated table keeps it, with
        # its rows: this schema adds, it never migrates destructively.
        self.conn.execute("CREATE TABLE IF NOT EXISTS unrelated (x TEXT)")
        self.conn.execute("INSERT INTO unrelated VALUES ('keep me')")
        self.conn.commit()
        initialize_adaptive_universe_schema(self.conn)
        self.assertEqual(
            self.conn.execute("SELECT x FROM unrelated").fetchone()[0], "keep me")


class TestIdempotency(_Store):

    def test_saving_the_same_selection_twice_writes_one_logical_decision(self):
        selection = self.select()
        self.assertTrue(self.repository.save_selection(selection))
        self.assertFalse(self.repository.save_selection(selection),
                         "the second save must be a no-op")
        counts = {table: self.conn.execute(
            "SELECT COUNT(*) FROM %s" % table).fetchone()[0]
            for table in ADAPTIVE_UNIVERSE_TABLES}
        self.assertEqual(counts["adaptive_session_snapshots"], 1)
        self.assertEqual(counts["adaptive_session_members"],
                         selection.universe.occupied)
        self.assertEqual(counts["adaptive_admission_decisions"],
                         len(selection.assessments))

    def test_a_re_run_of_the_selector_produces_the_same_snapshot_id(self):
        first, second = self.select(), self.select()
        self.assertEqual(first.snapshot_id, second.snapshot_id)
        self.repository.save_selection(first)
        self.repository.save_selection(second)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM adaptive_session_snapshots").fetchone()[0], 1)

    def test_registering_a_parent_universe_twice_keeps_one_row(self):
        self.assertTrue(self.repository.register_parent_universe(self.parent))
        self.assertFalse(self.repository.register_parent_universe(self.parent))
        self.assertEqual(len(self.repository.parent_versions()), 1)

    def test_expiry_events_are_idempotent_per_instant(self):
        selection = self.select()
        self.repository.save_selection(selection)
        moment = at(hour=16)
        written = self.repository.record_expiries(
            selection.snapshot_id, [("a", "validity ended")], moment)
        again = self.repository.record_expiries(
            selection.snapshot_id, [("a", "validity ended")], moment)
        self.assertEqual((written, again), (1, 0))


class TestRevisionHistory(_Store):

    def test_a_revision_does_not_erase_its_predecessor(self):
        first = self.select()
        self.repository.save_selection(first)
        later = at(hour=18)
        with MarketHistory(self.market_db, later) as history:
            revised = self.selector.revise(first, history, later, candidates=("c",))
        self.repository.save_selection(revised)

        revisions = self.repository.revisions("2026-08-27")
        self.assertEqual([r["revision"] for r in revisions], [0, 1])
        self.assertEqual(revisions[1]["previous_snapshot_id"], first.snapshot_id)
        # The original members are still readable, unchanged.
        stored = self.repository.snapshot(first.snapshot_id)
        self.assertEqual(stored["members"], sorted(first.universe.members))

    def test_two_revisions_of_one_session_keep_distinct_ids(self):
        first = self.select()
        later = at(hour=18)
        with MarketHistory(self.market_db, later) as history:
            revised = self.selector.revise(first, history, later)
        self.assertNotEqual(first.snapshot_id, revised.snapshot_id)
        self.assertEqual(revised.lineage.links()["universe_decision_id"],
                         revised.snapshot_id)

    def test_decisions_are_readable_by_verdict(self):
        selection = self.select()
        self.repository.save_selection(selection)
        passed = self.repository.decisions(selection.snapshot_id,
                                           AdmissionVerdict.PASS)
        self.assertEqual(len(passed), selection.verdict_counts()["pass"])
        for row in passed:
            self.assertEqual(row["eligibility_status"], "eligible")

    def test_stored_reasons_survive_the_round_trip(self):
        selection = self.select()
        self.repository.save_selection(selection)
        rows = self.repository.decisions(selection.snapshot_id)
        rejected = [r for r in rows if r["verdict"] != "pass"]
        for row in rejected:
            self.assertTrue(row["reasons"], row["instrument_id"])


class TestNoProductionReach(unittest.TestCase):
    """The safety properties: this layer cannot trade and cannot subscribe."""

    ADAPTIVE_MODULES = ("universe.py", "eligibility.py", "scoring.py",
                        "admission.py", "selector.py", "repository.py")

    def test_no_adaptive_module_imports_execution_or_trading(self):
        base = os.path.join(ROOT, "src", "adaptive")
        for name in self.ADAPTIVE_MODULES:
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
                        self.assertFalse(
                            module.startswith(forbidden),
                            f"src/adaptive/{name} imports {module}")

    def test_no_adaptive_module_calls_an_order_or_subscription_method(self):
        base = os.path.join(ROOT, "src", "adaptive")
        for name in self.ADAPTIVE_MODULES:
            with open(os.path.join(base, name), encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            calls = {node.func.attr for node in ast.walk(tree)
                     if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute)}
            for forbidden in ("place_order", "submit_order", "cancel_order",
                              "modify_order", "market_snapshot", "subscribe",
                              "init_brokerage_session"):
                self.assertNotIn(forbidden, calls, f"{name} calls {forbidden}")

    def test_the_repository_writes_only_adaptive_tables(self):
        with open(os.path.join(ROOT, "src", "adaptive", "repository.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        for statement in ("INSERT INTO", "INSERT OR IGNORE INTO", "UPDATE ",
                          "DELETE FROM"):
            index = 0
            while True:
                index = source.find(statement, index)
                if index == -1:
                    break
                fragment = source[index + len(statement):index + len(statement) + 40]
                self.assertTrue(
                    fragment.strip().startswith("adaptive_"),
                    f"repository writes to a non-adaptive table: {fragment!r}")
                index += 1

    def test_no_adaptive_universe_code_reads_the_protected_experiment(self):
        base = os.path.join(ROOT, "src", "adaptive")
        for name in self.ADAPTIVE_MODULES:
            with open(os.path.join(base, name), encoding="utf-8") as handle:
                source = handle.read().lower()
            for forbidden in ("anchor_v2", "anchor-v2", "validate_d20",
                              "protected_ledger", "return_60d"):
                self.assertNotIn(forbidden, source, f"{name} mentions {forbidden}")

    def test_the_adaptive_universe_flag_defaults_off(self):
        from src.adaptive import flags as adaptive_flags
        self.assertFalse(adaptive_flags.resolve({}).adaptive_universe_enabled)
        self.assertFalse(adaptive_flags.AdaptiveFlags().adaptive_universe_enabled)

    def test_the_cli_is_dry_run_by_default_and_writes_nothing(self):
        market_db = os.path.join(ROOT, "data", "marketlens.db")
        before = os.path.getmtime(market_db)
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.path.abspath(ROOT)
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "adaptive_universe.py"),
             "--as-of", "2026-08-27T13:25:00+00:00", "--json"],
            capture_output=True, text=True, env=environment, timeout=600)
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        report = json.loads(result.stdout)
        self.assertEqual(report["mode"], "dry-run")
        self.assertFalse(report["flag_adaptive_universe_enabled"])
        self.assertEqual(os.path.getmtime(market_db), before,
                         "a dry run must not write to the database")

    def test_the_capture_universe_file_is_not_written_by_this_phase(self):
        # The selector reads it for the shadow comparison and nothing else.
        for name in ("selector.py", "universe.py"):
            with open(os.path.join(ROOT, "src", "adaptive", name),
                      encoding="utf-8") as handle:
                source = handle.read()
            self.assertNotIn("capture_universe", source)


if __name__ == "__main__":
    unittest.main()
