"""
tests/marketdata/test_separation_and_safety.py
-----------------------------------------------------------
Source separation, the price boundary, and safety (§36 C, H, I, J, K).

THE CENTRAL PROPERTY

A trading consumer must never receive a research price believing it
got a current one. The forbidden sequence, named in the phase spec, is:

    IBKR unavailable -> use a five-day-old research close
    -> present it as current -> trade on it

These cases assert that the sequence is impossible by construction,
not merely discouraged: `operational_price` reads one table and has no
access to the other.
"""

import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.market_data_schema import (
    DEFAULT_BAR_RETENTION_DAYS, initialize_market_data_schema,
    market_data_tables_present, prune,
)
from src.domain.market_data_models import (
    MarketDataAvailability, OperationalQuote, UniverseEntry,
)
from src.domain.paper_models import DataFreshness
from src.marketdata import universe as universe_module
from src.marketdata.prices import (
    PriceSource, operational_price, research_price, tradeable_prices,
)
from src.marketdata.repository import MarketDataRepository

NOW = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)
INSTRUMENT = "us_and_intl-aapl"


def memory_db():
    conn = sqlite3.connect(":memory:")
    initialize_market_data_schema(conn)
    conn.execute("""
        CREATE TABLE price_candle_cache (
            instrument_id TEXT, interval TEXT, timestamp TEXT,
            open REAL, high REAL, low REAL, close REAL,
            adjusted_close REAL, volume REAL, source TEXT, fetched_at TEXT
        )
    """)
    conn.commit()
    return conn


def cached_candle(conn, instrument_id=INSTRUMENT, close=250.0, days_old=5):
    stamp = (NOW - timedelta(days=days_old)).isoformat()
    conn.execute(
        "INSERT INTO price_candle_cache (instrument_id, interval, timestamp, "
        "close) VALUES (?,?,?,?)", (instrument_id, "1d", stamp, close))
    conn.commit()


def live_quote(price=334.5, age_seconds=5,
               availability=MarketDataAvailability.AVAILABLE):
    moment = NOW - timedelta(seconds=age_seconds)
    return OperationalQuote(
        instrument_id=INSTRUMENT, conid="265598", last=price, mid=price,
        availability=availability, broker_at=moment, received_at=moment,
        evaluated_at=NOW)


class TestSchema(unittest.TestCase):

    def test_initialisation_is_idempotent(self):
        conn = memory_db()
        initialize_market_data_schema(conn)
        initialize_market_data_schema(conn)
        self.assertEqual(len(market_data_tables_present(conn)), 3)

    def test_state_holds_one_row_per_instrument(self):
        """
        There is no history here by design: a second row would be a
        second answer to "what is it worth now".
        """
        conn = memory_db()
        repository = MarketDataRepository(conn)
        repository.upsert_quotes([live_quote(price=100.0)], NOW)
        repository.upsert_quotes([live_quote(price=200.0)], NOW)
        rows = repository.all_latest()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["last"], 200.0)

    def test_retention_never_prunes_current_state(self):
        conn = memory_db()
        MarketDataRepository(conn).upsert_quotes([live_quote()], NOW)
        prune(conn, bar_retention_days=0, cycle_retention_days=0)
        self.assertEqual(len(MarketDataRepository(conn).all_latest()), 1)


class TestSourceSeparation(unittest.TestCase):

    def test_the_operational_layer_never_writes_the_research_cache(self):
        """The single most important assertion in this phase."""
        conn = memory_db()
        cached_candle(conn)
        before = conn.execute(
            "SELECT COUNT(*), SUM(close) FROM price_candle_cache").fetchone()
        MarketDataRepository(conn).upsert_quotes(
            [live_quote(price=999.0)], NOW)
        after = conn.execute(
            "SELECT COUNT(*), SUM(close) FROM price_candle_cache").fetchone()
        self.assertEqual(before, after)

    def test_a_research_price_is_never_tradeable(self):
        """
        Not because of its age -- because of its provenance. A close
        from this morning is still not the current market.
        """
        conn = memory_db()
        cached_candle(conn, days_old=0)
        quote = research_price(conn, INSTRUMENT, NOW)
        self.assertIs(quote.source, PriceSource.RESEARCH)
        self.assertFalse(quote.is_tradeable)
        self.assertIn("NOT a current market price", quote.note)

    def test_no_operational_state_does_not_fall_back_to_the_cache(self):
        """
        The forbidden sequence. With a cached candle present and no
        live state, the operational answer must be an explicit
        absence, never the cached close.
        """
        conn = memory_db()
        cached_candle(conn, close=250.0)
        quote = operational_price(conn, INSTRUMENT, NOW)
        self.assertIsNone(quote.price)
        self.assertIs(quote.freshness, DataFreshness.UNAVAILABLE)
        self.assertFalse(quote.is_tradeable)
        self.assertNotEqual(quote.price, 250.0)

    def test_the_two_sources_are_reported_separately(self):
        conn = memory_db()
        cached_candle(conn, close=250.0)
        MarketDataRepository(conn).upsert_quotes(
            [live_quote(price=334.5)], NOW)
        live = operational_price(conn, INSTRUMENT, NOW)
        cached = research_price(conn, INSTRUMENT, NOW)
        self.assertEqual(live.price, 334.5)
        self.assertEqual(cached.price, 250.0)
        self.assertIs(live.source, PriceSource.OPERATIONAL)
        self.assertIs(cached.source, PriceSource.RESEARCH)

    def test_a_missing_state_table_is_reported_not_crashed(self):
        conn = sqlite3.connect(":memory:")
        quote = operational_price(conn, INSTRUMENT, NOW)
        self.assertIsNone(quote.price)
        self.assertIn("never run", quote.note)


class TestTradingBoundary(unittest.TestCase):

    def test_a_fresh_live_price_is_tradeable(self):
        conn = memory_db()
        MarketDataRepository(conn).upsert_quotes([live_quote()], NOW)
        self.assertTrue(operational_price(conn, INSTRUMENT, NOW).is_tradeable)

    def test_a_stale_price_is_refused(self):
        conn = memory_db()
        MarketDataRepository(conn).upsert_quotes(
            [live_quote(age_seconds=600)], NOW)
        quote = operational_price(conn, INSTRUMENT, NOW)
        self.assertFalse(quote.is_tradeable)

    def test_a_delayed_price_is_refused_however_recent(self):
        conn = memory_db()
        MarketDataRepository(conn).upsert_quotes(
            [live_quote(age_seconds=1,
                        availability=MarketDataAvailability.DELAYED)], NOW)
        self.assertFalse(operational_price(conn, INSTRUMENT, NOW).is_tradeable)

    def test_freshness_is_rejudged_against_the_caller_clock(self):
        """
        The verdict stored at write time was true then and says
        nothing about now -- the failure mode after a restart.
        """
        conn = memory_db()
        MarketDataRepository(conn).upsert_quotes([live_quote()], NOW)
        later = NOW + timedelta(hours=2)
        self.assertTrue(operational_price(conn, INSTRUMENT, NOW).is_tradeable)
        self.assertFalse(operational_price(conn, INSTRUMENT, later).is_tradeable)

    def test_tradeable_prices_omits_untrustworthy_instruments(self):
        """
        Absent, not present-with-a-suspect-number. A caller iterating
        the result cannot accidentally act on one.
        """
        conn = memory_db()
        repository = MarketDataRepository(conn)
        good = live_quote()
        bad = OperationalQuote(
            instrument_id="us_and_intl-msft", last=400.0, mid=400.0,
            availability=MarketDataAvailability.DELAYED,
            broker_at=NOW, received_at=NOW, evaluated_at=NOW)
        repository.upsert_quotes([good, bad], NOW)
        prices = tradeable_prices(
            conn, [INSTRUMENT, "us_and_intl-msft"], NOW)
        self.assertIn(INSTRUMENT, prices)
        self.assertNotIn("us_and_intl-msft", prices)


class TestUniverse(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("""
            CREATE TABLE broker_instrument_mapping (
                canonical_instrument_id TEXT, broker_id TEXT,
                broker_symbol TEXT, venue TEXT, asset_class TEXT,
                currency TEXT, tradable INTEGER, broker_payload_json TEXT
            )
        """)

    def add(self, instrument_id, conid="1", asset_class="stock", tradable=1):
        payload = f'{{"conid": "{conid}"}}' if conid else "{}"
        self.conn.execute(
            "INSERT INTO broker_instrument_mapping VALUES (?,?,?,?,?,?,?,?)",
            (instrument_id, "ibkr", "SYM", "NASDAQ", asset_class, "USD",
             tradable, payload))
        self.conn.commit()

    def test_an_unresolved_contract_is_blocked_with_a_reason(self):
        self.add("us_and_intl-aapl", conid="")
        entries = universe_module.resolve_universe(self.conn)
        self.assertEqual(len(universe_module.active(entries)), 0)
        self.assertIn("no resolved IBKR contract",
                      universe_module.excluded(entries)["us_and_intl-aapl"])

    def test_an_unsupported_asset_class_is_blocked_not_attempted(self):
        self.add("bvb-tlv", conid="99", asset_class="bvb")
        entries = universe_module.resolve_universe(self.conn)
        self.assertEqual(len(universe_module.active(entries)), 0)
        self.assertIn("not supported",
                      universe_module.excluded(entries)["bvb-tlv"])

    def test_the_universe_is_deterministic(self):
        for name in ("c", "a", "b"):
            self.add(name, conid=name)
        first = [e.instrument_id
                 for e in universe_module.resolve_universe(self.conn)]
        second = [e.instrument_id
                  for e in universe_module.resolve_universe(self.conn)]
        self.assertEqual(first, second)
        self.assertEqual(first, ["a", "b", "c"])

    def test_a_limit_truncates_after_ordering_not_arbitrarily(self):
        for name in ("c", "a", "b"):
            self.add(name, conid=name)
        entries = universe_module.resolve_universe(self.conn, limit=2)
        self.assertEqual([e.instrument_id
                          for e in universe_module.active(entries)],
                         ["a", "b"])

    def test_capacity_refuses_a_universe_that_exceeds_the_budget(self):
        report = universe_module.capacity_report(
            entry_count=500, requests_per_cycle=500,
            interval_seconds=60.0, budget_per_minute=50)
        self.assertFalse(report["fits"])

    def test_batching_makes_a_sixty_second_cycle_fit_comfortably(self):
        report = universe_module.capacity_report(
            entry_count=50, requests_per_cycle=1,
            interval_seconds=60.0, budget_per_minute=50)
        self.assertTrue(report["fits"])
        self.assertEqual(report["requests_per_minute"], 1.0)


class TestSafety(unittest.TestCase):
    """
    §36 K: this layer must not be able to trade.

    Scanned by AST rather than substring, per this project's own
    convention that scanners must be tokenised. A substring scan
    flagged the CLI's own docstring for the sentence "never builds an
    IntentRequest", which is documentation of the guarantee rather
    than a breach of it.
    """

    #: Names that would mean market data had reached execution.
    FORBIDDEN = frozenset({
        "submit_order", "place_order", "from_decision", "ExecutionService",
        "ExecutionOrchestrator", "IntentRequest", "PreTradeValidator",
    })

    def _names_used(self, path):
        """Every identifier this module imports or calls."""
        import ast
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    used.add(alias.name.split(".")[-1])
            elif isinstance(node, ast.ImportFrom):
                used.add((node.module or "").split(".")[-1])
                for alias in node.names:
                    used.add(alias.name)
            elif isinstance(node, ast.Attribute):
                used.add(node.attr)
            elif isinstance(node, ast.Name):
                used.add(node.id)
        return used

    def test_the_package_reaches_no_execution_machinery(self):
        import src.marketdata.prices as prices
        import src.marketdata.quotes as quotes
        import src.marketdata.service as service
        for module in (service, quotes, prices):
            used = self._names_used(module.__file__)
            breaches = used & self.FORBIDDEN
            self.assertEqual(
                breaches, set(),
                f"{os.path.basename(module.__file__)} reaches {breaches}; "
                f"market data must not touch execution")

    def test_the_operator_script_cannot_place_an_order(self):
        path = os.path.join(os.path.dirname(__file__), "..", "..",
                            "scripts", "run_market_data.py")
        used = self._names_used(path)
        self.assertEqual(used & self.FORBIDDEN, set())

    def test_the_operator_script_declares_no_ordering_flag(self):
        """No --submit, no --allow-paper-orders: nothing to switch on."""
        import ast
        path = os.path.join(os.path.dirname(__file__), "..", "..",
                            "scripts", "run_market_data.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        flags = [node.args[0].value
                 for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "add_argument"
                 and node.args and isinstance(node.args[0], ast.Constant)]
        for flag in flags:
            self.assertNotIn("submit", flag)
            self.assertNotIn("order", flag)
        self.assertIn("--status", flags)


if __name__ == "__main__":
    unittest.main(verbosity=2)
