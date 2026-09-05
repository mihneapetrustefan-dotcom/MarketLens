"""
tests/test_dashboard_signal_labels.py
-------------------------------------------
Signals labelled by company, and the ticker match that never matched.

WHAT THESE DEFEND
---------------------
1. That the signals table carries a company name and ticker, so a row
   reads "Berkshire Hathaway BRK.B" rather than "us_and_intl-brk.b".
   The id is a storage key: it tells a reader which exchange bucket the
   row lives under, not which company the signal is about.

2. That name and ticker are APPENDED to the tuple. The page indexes it
   positionally, so inserting a column would shift s[1]..s[7] and
   silently mislabel every field rather than failing.

3. That a company page finds its own signals. The old filter compared
   `instrument_id` with "crypto-" stripped against the ticker:

       "crypto-btc"        -> "BTC"              matches BTC
       "us_and_intl-aapl"  -> "US_AND_INTL-AAPL" never matches AAPL

   Crypto matched by accident and every US equity missed, so those
   company pages reported "no signals yet" while holding five.
"""

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.dashboard import DashboardGenerator

#: Indices the page JS depends on. Named here so a change that shifts
#: them fails loudly in one place instead of mislabelling five columns.
IDX_INSTRUMENT_ID = 1
IDX_DIRECTION = 2
IDX_STATUS = 3
IDX_STRENGTH = 4
IDX_CONFIDENCE = 5
IDX_EXPECTED_RETURN = 6
IDX_CUTOFF = 7
IDX_NAME = 8
IDX_TICKER = 9
#: Phase 18: the status of the model that produced this signal.
IDX_MODEL_STATUS = 10

#: Tuple width. Named once so a change fails in one place instead of
#: silently shifting a column the page reads positionally.
SIGNAL_TUPLE_WIDTH = 11


class SignalLabelCase(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript("""
            CREATE TABLE companies (company_id TEXT PRIMARY KEY,
                canonical_name TEXT, aliases_json TEXT, sector_id TEXT);
            CREATE TABLE securities (security_id TEXT PRIMARY KEY,
                company_id TEXT, instrument_type TEXT, currency TEXT);
            CREATE TABLE instruments (instrument_id TEXT PRIMARY KEY,
                security_id TEXT, exchange_id TEXT, ticker TEXT, asset_class TEXT);
            CREATE TABLE signals (signal_id TEXT PRIMARY KEY, instrument_id TEXT,
                direction TEXT, status TEXT, strength REAL, confidence REAL,
                expected_return REAL, source_information_cutoff TEXT);
        """)

    def tearDown(self):
        self.conn.close()

    def register(self, instrument_id, ticker, name):
        cid = instrument_id + "-co"
        sid = instrument_id + "-sec"
        self.conn.execute("INSERT INTO companies VALUES (?,?,'[]','s')", (cid, name))
        self.conn.execute("INSERT INTO securities VALUES (?,?,'common','USD')", (sid, cid))
        self.conn.execute("INSERT INTO instruments VALUES (?,?,'X',?,'stock')",
                          (instrument_id, sid, ticker))
        self.conn.commit()

    def add_signal(self, instrument_id, signal_id="sig-1",
                   cutoff="2026-09-03T00:00:00+00:00"):
        self.conn.execute(
            "INSERT INTO signals VALUES (?,?, 'long','active',0.5,0.3,0.01,?)",
            (signal_id, instrument_id, cutoff))
        self.conn.commit()

    def recent(self):
        g = DashboardGenerator.__new__(DashboardGenerator)
        return g._collect_signals(self.conn)["recent"]


class TestTheNameIsCarried(SignalLabelCase):

    def test_a_signal_carries_its_company_name_and_ticker(self):
        self.register("us_and_intl-brk.b", "BRK.B", "Berkshire Hathaway")
        self.add_signal("us_and_intl-brk.b")
        row = self.recent()[0]
        self.assertEqual(row[IDX_NAME], "Berkshire Hathaway")
        self.assertEqual(row[IDX_TICKER], "BRK.B")

    def test_crypto_resolves_too(self):
        self.register("crypto-btc", "BTC", "Bitcoin")
        self.add_signal("crypto-btc")
        row = self.recent()[0]
        self.assertEqual(row[IDX_NAME], "Bitcoin")

    def test_an_unregistered_instrument_yields_none_not_a_guess(self):
        """
        The page falls back to the id. Inventing a name from the slug
        would produce a label that looks authoritative and is made up.
        """
        self.add_signal("us_and_intl-ghost")
        row = self.recent()[0]
        self.assertIsNone(row[IDX_NAME])
        self.assertIsNone(row[IDX_TICKER])
        self.assertEqual(row[IDX_INSTRUMENT_ID], "us_and_intl-ghost")

    def test_a_signal_without_a_registered_instrument_is_still_listed(self):
        """A LEFT JOIN, not an inner one: an unlabelled signal must not vanish."""
        self.register("us_and_intl-aapl", "AAPL", "Apple")
        self.add_signal("us_and_intl-aapl", "sig-1")
        self.add_signal("us_and_intl-ghost", "sig-2")
        self.assertEqual(len(self.recent()), 2)


class TestTheLabelCannotDeleteTheSignal(SignalLabelCase):
    """
    The first version of this feature joined instruments/securities/
    companies straight into the signals query. `_rows` swallows a
    missing table into [], so on a database holding signals but no
    instrument registry EVERY SIGNAL DISAPPEARED -- not unlabelled,
    gone, silently.

    tests/test_dashboard.py caught it. The label is decoration; the
    signal is the data, and decoration must never be able to delete
    data.
    """

    def test_signals_survive_a_missing_instrument_registry(self):
        self.conn.execute("DROP TABLE instruments")
        self.conn.execute("DROP TABLE securities")
        self.conn.execute("DROP TABLE companies")
        self.add_signal("crypto-btc")
        rows = self.recent()
        self.assertEqual(len(rows), 1, "the signal vanished with the registry")
        self.assertEqual(rows[0][IDX_INSTRUMENT_ID], "crypto-btc")
        self.assertIsNone(rows[0][IDX_NAME])

    def test_the_tuple_keeps_its_shape_without_the_registry(self):
        """The page indexes positionally; a short tuple would raise there."""
        self.conn.execute("DROP TABLE instruments")
        self.add_signal("crypto-btc")
        self.assertEqual(len(self.recent()[0]), SIGNAL_TUPLE_WIDTH)


class TestTheTupleShapeIsStable(SignalLabelCase):
    """
    The page indexes this tuple positionally. Inserting a column rather
    than appending would shift every later field and mislabel five
    columns silently.
    """

    def test_the_established_indices_still_mean_what_they_meant(self):
        self.register("us_and_intl-aapl", "AAPL", "Apple")
        self.conn.execute(
            "INSERT INTO signals VALUES ('sig-x','us_and_intl-aapl','short',"
            "'suppressed',0.75,0.30,-0.0123,'2026-09-01T00:00:00+00:00')")
        self.conn.commit()
        row = self.recent()[0]
        self.assertEqual(row[IDX_INSTRUMENT_ID], "us_and_intl-aapl")
        self.assertEqual(row[IDX_DIRECTION], "short")
        self.assertEqual(row[IDX_STATUS], "suppressed")
        self.assertAlmostEqual(row[IDX_STRENGTH], 0.75)
        self.assertAlmostEqual(row[IDX_CONFIDENCE], 0.30)
        self.assertAlmostEqual(row[IDX_EXPECTED_RETURN], -0.0123)
        self.assertTrue(row[IDX_CUTOFF].startswith("2026-09-01"))

    def test_the_name_and_ticker_are_appended_at_the_end(self):
        self.register("us_and_intl-aapl", "AAPL", "Apple")
        self.add_signal("us_and_intl-aapl")
        self.assertEqual(len(self.recent()[0]), SIGNAL_TUPLE_WIDTH)


class TestTheCompanyPageMatch(unittest.TestCase):
    """
    The rule the page uses to decide which signals belong to a company.

    Reproduced here rather than tested through the browser, because the
    bug was in this comparison and nowhere else.
    """

    @staticmethod
    def old_rule(instrument_id, ticker_on_page):
        return (str(instrument_id).replace("crypto-", "").upper()
                == ticker_on_page.upper())

    @staticmethod
    def new_rule(instrument_id, signal_ticker, ticker_on_page):
        tick = signal_ticker if signal_ticker else str(instrument_id).replace("crypto-", "")
        return str(tick).upper() == ticker_on_page.upper()

    def test_the_old_rule_missed_every_us_equity(self):
        self.assertFalse(self.old_rule("us_and_intl-aapl", "AAPL"))
        self.assertFalse(self.old_rule("us_and_intl-brk.b", "BRK.B"))

    def test_the_old_rule_matched_crypto_by_accident(self):
        self.assertTrue(self.old_rule("crypto-btc", "BTC"))

    def test_the_new_rule_matches_both(self):
        self.assertTrue(self.new_rule("us_and_intl-aapl", "AAPL", "AAPL"))
        self.assertTrue(self.new_rule("us_and_intl-brk.b", "BRK.B", "BRK.B"))
        self.assertTrue(self.new_rule("crypto-btc", "BTC", "BTC"))

    def test_the_new_rule_still_separates_different_companies(self):
        self.assertFalse(self.new_rule("us_and_intl-aapl", "AAPL", "MSFT"))

    def test_the_new_rule_falls_back_when_the_ticker_is_absent(self):
        self.assertTrue(self.new_rule("crypto-btc", None, "BTC"))


if __name__ == "__main__":
    unittest.main()
