"""
tests/test_dashboard_trading_loop.py
------------------------------------------
Phase 25 §30 — the trading-loop workspace.

Three properties matter more than the layout:

    THE PAGE CANNOT SHOW AN INTENTION AS A HOLDING.
    `positions` is filtered on `origin = 'broker_reconciled'` inside
    the SQL, not by whoever reads the payload. §16 exists because a
    system that reports its intended book as its actual one is the
    single most expensive mistake a paper-trading page can make.

    THE PAGE CANNOT SHOW A STORED "live" AS LIVE.
    The mode is resolved through `TradingMode.resolve` rather than read
    raw, so a row that says live displays as OFF with the reason.

    THE PAGE CANNOT SHOW ONLY THE SIGNALS THAT TRADED.
    Every eligibility code is collected, including the refusals. A
    conversion figure computed from winners is not a conversion figure.
"""

import json
import os
import re
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.dashboard import DashboardGenerator
from src.data_access.trading_loop_schema import initialize_trading_loop_schema
from src.domain.trading_loop_models import TradingMode
from src.trading.mode import TradingModeStore

AT = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)


def a_database():
    conn = sqlite3.connect(":memory:")
    initialize_trading_loop_schema(conn)
    return conn


def a_cycle(conn, cycle_id="cyc-1", status="completed", health="healthy",
            signals=10, eligible=2, orders=1, fills=1, discrepancies=0):
    conn.execute("""
        INSERT INTO trading_cycles
        (cycle_id, session_id, method_version, anchor, status, mode, health,
         signals_seen, signals_eligible, targets_set, intents_created,
         orders_submitted, orders_rejected, fills_recorded,
         positions_reconciled, discrepancies, outcomes_recorded, blocks_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (cycle_id, "sess-1", "phase25-v1", AT.isoformat(), status, "paper",
          health, signals, eligible, 1, 1, orders, 0, fills, 1,
          discrepancies, 1, "[]"))
    conn.commit()


def a_position(conn, origin="broker_reconciled", quantity=500.0,
               instrument="i-aapl", cycle_id="cyc-1"):
    conn.execute("""
        INSERT INTO position_actuals
        (cycle_id, instrument_id, method_version, quantity, average_price,
         market_price, unrealized_pnl, realized_pnl, origin, broker_id,
         account_id, observed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (cycle_id, instrument, "phase25-v1", quantity, 100.0, 101.0, 500.0,
          0.0, origin, "ibkr", "DU1", AT.isoformat()))
    conn.commit()


def an_eligibility(conn, code, signal_id="sig-1", cycle_id="cyc-1"):
    conn.execute("""
        INSERT INTO signal_eligibility
        (cycle_id, signal_id, method_version, instrument_id, code, detail,
         checks_performed, experimental, evaluated_at)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (cycle_id, signal_id, "phase25-v1", "i-aapl", code, "because", 9,
          1, AT.isoformat()))
    conn.commit()


class TestThePayload(unittest.TestCase):

    def test_every_key_the_sidebar_reads_exists_in_the_payload(self):
        """
        The guard that catches a blank terminal.

        Phase 25 adds a sidebar entry reading `D.tradingloop`, and the
        mistake of adding one without the payload has now been made
        twice in this project — once in Phase 20 and once in Phase 21,
        and it left the whole page blank for two phases because nobody
        opened it in a browser.
        """
        html = DashboardGenerator().generate_report(conn=a_database())
        data = json.loads(re.search(r"var D = (\{.*?\});\n", html, re.S).group(1))
        navigation = re.search(r"var NAV = \[(.*?)\n  \];", html, re.S)
        self.assertIsNotNone(navigation)
        referenced = set(re.findall(r"D\.([A-Za-z_][A-Za-z0-9_]*)",
                                    navigation.group(1)))
        missing = sorted(name for name in referenced if name not in data)
        self.assertEqual(missing, [], "sidebar dereferences %s" % missing)

    def test_the_trading_loop_key_is_present(self):
        html = DashboardGenerator().generate_report(conn=a_database())
        data = json.loads(re.search(r"var D = (\{.*?\});\n", html, re.S).group(1))
        self.assertIn("tradingloop", data)


class TestTheCollector(unittest.TestCase):

    def setUp(self):
        self.conn = a_database()
        self.generator = DashboardGenerator()

    def tearDown(self):
        self.conn.close()

    def collect(self):
        return self.generator._collect_trading_loop(self.conn)

    def test_an_absent_table_is_reported_as_unavailable(self):
        empty = sqlite3.connect(":memory:")
        collected = self.generator._collect_trading_loop(empty)
        self.assertFalse(collected["available"])
        self.assertEqual(collected["mode"], "off")
        empty.close()

    def test_an_empty_loop_is_reported_as_unavailable(self):
        self.assertFalse(self.collect()["available"])

    def test_a_stored_live_mode_displays_as_off(self):
        """
        The row cannot be written through `TradingModeStore`, but it
        could arrive from a restored backup or a hand-edited database.
        The page must resolve it, not print it.
        """
        a_cycle(self.conn)
        self.conn.execute("""
            INSERT INTO trading_mode
            (singleton, mode, reason, actor, kill_switch, kill_reason,
             method_version, updated_at)
            VALUES (1,'live','','a',0,'','phase25-v1',?)
        """, (AT.isoformat(),))
        self.conn.commit()
        collected = self.collect()
        self.assertEqual(collected["mode"], "off")
        self.assertIn("live trading is blocked",
                      collected["mode_detail"]["reason"])
        self.assertEqual(collected["mode_detail"]["stored_raw"], "live")

    def test_the_kill_switch_is_surfaced(self):
        a_cycle(self.conn)
        TradingModeStore(self.conn).set_mode(
            TradingMode.PAPER, actor="a", reason="r", at=AT)
        TradingModeStore(self.conn).activate_kill_switch(
            actor="a", reason="drawdown", at=AT)
        collected = self.collect()
        self.assertTrue(collected["kill_switch"])
        self.assertEqual(collected["mode"], "off")

    def test_only_broker_reconciled_positions_are_listed(self):
        """§16: an intention must never be reported as a holding."""
        a_cycle(self.conn)
        a_position(self.conn, origin="broker_reconciled", instrument="i-real")
        a_position(self.conn, origin="local_projection", instrument="i-guess")
        a_position(self.conn, origin="unknown", instrument="i-unknown")
        instruments = {p["instrument_id"] for p in self.collect()["positions"]}
        self.assertEqual(instruments, {"i-real"})

    def test_a_flat_position_is_not_listed(self):
        a_cycle(self.conn)
        a_position(self.conn, quantity=0.0, instrument="i-closed")
        self.assertEqual(self.collect()["positions"], [])

    def test_every_eligibility_code_is_collected_not_just_the_winners(self):
        """§14: a page showing only what traded cannot explain what did not."""
        a_cycle(self.conn)
        an_eligibility(self.conn, "eligible", "sig-a")
        an_eligibility(self.conn, "suppressed", "sig-b")
        an_eligibility(self.conn, "stale", "sig-c")
        codes = {e["code"] for e in self.collect()["eligibility"]}
        self.assertEqual(codes, {"eligible", "suppressed", "stale"})

    def test_blocked_cycles_are_counted(self):
        a_cycle(self.conn, cycle_id="cyc-ok", status="completed")
        a_cycle(self.conn, cycle_id="cyc-blocked", status="blocked")
        collected = self.collect()
        self.assertEqual(collected["total_cycles"], 2)
        self.assertEqual(collected["blocked_cycles"], 1)

    def test_the_collector_carries_no_overall_score(self):
        a_cycle(self.conn)
        collected = self.collect()
        for key in ("score", "overall", "rank", "total_score"):
            self.assertNotIn(key, collected)


class TestThePageIsHonest(unittest.TestCase):

    def setUp(self):
        self.conn = a_database()
        a_cycle(self.conn)
        a_position(self.conn)
        an_eligibility(self.conn, "eligible")
        TradingModeStore(self.conn).set_mode(
            TradingMode.PAPER, actor="a", reason="r", at=AT)
        self.html = DashboardGenerator().generate_report(conn=self.conn)

    def tearDown(self):
        self.conn.close()

    def test_the_page_says_everything_is_paper(self):
        self.assertIn("Totul de pe aceasta pagina este PAPER", self.html)

    def test_the_page_states_that_live_is_blocked(self):
        self.assertIn("Tranzactionarea reala este blocata", self.html)

    def test_the_page_makes_no_network_calls(self):
        for word in ("fetch(", "XMLHttpRequest", "WebSocket"):
            self.assertNotIn(word, self.html)

    def test_the_page_offers_a_command_rather_than_a_trade_button(self):
        self.assertIn("scripts/run_trading_loop.py", self.html)
        self.assertIn("Nu exista niciun buton aici care sa plaseze un ordin",
                      self.html)

    def test_the_page_distinguishes_target_from_actual(self):
        self.assertIn("Tinta fata de realitate", self.html)
        self.assertIn("Pozitii detinute", self.html)


if __name__ == "__main__":
    unittest.main()
