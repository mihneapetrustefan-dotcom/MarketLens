"""
tests/trading/test_execution_readiness_25_9e.py
-----------------------------------------------------------
Phase 25.9E — automated trading and IBKR execution readiness.

THE VENUE IS A DOUBLE, EVERYTHING ELSE IS REAL
--------------------------------------------------
`MockIBKRTransport` stands where the Client Portal Gateway would. The
loop, the Phase 11 portfolio and risk engine, the Phase 14 orchestrator
and reconciler, the Phase 15 gateway, the Phase 17 intake, the
operational price layer and the exchange calendar are the production
objects. No test here contacts IBKR, and every test that could submit
asserts how many times the venue was asked to place an order.

Each defect class below was reproduced against the unfixed code before
the fix; the docstrings say what it did.
"""

import os
import sqlite3
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.market_data_schema import initialize_market_data_schema
from src.domain.broker_models import MarketStatus
from src.domain.market_data_models import MarketDataAvailability, OperationalQuote
from src.domain.trading_loop_models import BlockReason, TradingMode
from src.execution.adapters.submission_guard import (
    FORBIDDEN_CODE, BrokerSubmissionForbidden, PreSubmissionGateway,
)
from src.execution.policy import client_order_id
from src.marketdata.calendar import USEquityCalendar, early_closes, holidays
from src.marketdata.repository import MarketDataRepository
from src.trading import leases, pricing, readiness
from src.trading.clock import RunMode, ReplayClock
from src.trading.mode import TradingModeStore
from src.trading.schedule import Schedule
from src.trading.session_runner import SessionRefused, SessionRunner
from src.trading.stack import build_stack
from tests.trading.helpers import (
    NOW, a_live_signal, build_loop, enable_paper, make_connection,
    store_signals, universe,
)

INSTRUMENT = "i-aapl"
UTC = timezone.utc


def quote(conn, price=123.45, age_seconds=10.0, now=NOW,
          availability=MarketDataAvailability.AVAILABLE, instrument=INSTRUMENT):
    """Write one operational quote, as the market-data service would."""
    initialize_market_data_schema(conn)
    stamp = now - timedelta(seconds=age_seconds)
    MarketDataRepository(conn).upsert_quotes([OperationalQuote(
        instrument_id=instrument, last=price, bid=price - 0.01,
        ask=price + 0.01, mid=price, availability=availability,
        broker_at=stamp, received_at=stamp)], now)
    conn.commit()


def blocks(result):
    return [b.reason for b in result.blocks]


class LoopCase(unittest.TestCase):
    """A live signal, PAPER mode, bars in the research cache at 100.00."""

    def setUp(self):
        self.conn = make_connection()
        universe(self.conn, price=100.0)
        store_signals(self.conn, [a_live_signal()])
        enable_paper(self.conn)

    def tearDown(self):
        self.conn.close()

    def operational_loop(self, **kwargs):
        kwargs.setdefault("price_source", pricing.OPERATIONAL)
        return build_loop(self.conn, **kwargs)


# ======================================================================
# CRITICAL — decisions on research-cache prices
# ======================================================================

class TestDecisionsUseOperationalPrices(LoopCase):
    """
    Before the fix every price on the order path came from
    `price_candle_cache`, accepted up to five days old, whatever the
    transport. The production snapshot's newest close was eleven days
    old on 2026-09-16.
    """

    def test_a_real_venue_is_always_operational(self):
        for configured in ("research", "operational", "", "anything"):
            self.assertEqual(pricing.resolve_price_source("client_portal", configured),
                             pricing.OPERATIONAL)
        self.assertEqual(pricing.resolve_price_source("mock", "research"),
                         pricing.RESEARCH)

    def test_the_order_is_priced_from_the_current_quote_not_the_cache(self):
        quote(self.conn, price=123.45)
        loop = self.operational_loop()
        result = loop.run_cycle(NOW)
        self.assertEqual(result.orders_submitted, 1, [b.detail for b in result.blocks])
        order = next(iter(loop.stack.orchestrator.orders.values()))
        self.assertAlmostEqual(order.reference_price, 123.45)

    def test_no_quote_means_no_order_even_with_a_cached_close(self):
        loop = self.operational_loop()
        result = loop.run_cycle(NOW)
        self.assertEqual(result.orders_submitted, 0)
        self.assertEqual(loop.stack.transport.place_calls, 0)
        self.assertIn(BlockReason.STALE_MARKET_DATA, blocks(result))

    def test_a_stale_quote_means_no_order(self):
        quote(self.conn, age_seconds=16 * 60)
        loop = self.operational_loop()
        self.assertEqual(loop.run_cycle(NOW).orders_submitted, 0)
        self.assertEqual(loop.stack.transport.place_calls, 0)

    def test_a_delayed_quote_means_no_order(self):
        quote(self.conn, availability=MarketDataAvailability.DELAYED)
        loop = self.operational_loop()
        self.assertEqual(loop.run_cycle(NOW).orders_submitted, 0)

    def test_a_quote_from_the_future_is_refused(self):
        """Point in time: stamped after the decision moment."""
        quote(self.conn, age_seconds=-120)
        repository = pricing.OperationalPriceRepository(self.conn, evaluated_at=NOW)
        self.assertEqual(repository.prices_as_of([INSTRUMENT], NOW), {})

    def test_a_disconnected_feed_ages_into_a_block(self):
        """Good quote, then silence: sixteen minutes later it is not a price."""
        quote(self.conn, age_seconds=5)
        self.assertIsNotNone(pricing.freshest_operational_age_days(self.conn, NOW))
        later = NOW + timedelta(minutes=16)
        self.assertIsNone(pricing.freshest_operational_age_days(self.conn, later))

    def test_reconnect_restores_freshness(self):
        quote(self.conn, age_seconds=5)
        later = NOW + timedelta(minutes=16)
        self.assertIsNone(pricing.freshest_operational_age_days(self.conn, later))
        quote(self.conn, age_seconds=3, now=later)
        self.assertIsNotNone(pricing.freshest_operational_age_days(self.conn, later))


# ======================================================================
# HIGH — position reconciliation compared the broker with itself
# ======================================================================

class TestReconciliationComparesOurBook(LoopCase):
    """
    Before the fix `internal_positions` was built from the gateway's own
    positions, so a position we never traded could not be a mismatch.
    The Phase 25.5 "ghost position" test injected a ghost ORDER, which
    order reconciliation already caught.
    """

    def test_local_zero_broker_ten_blocks_new_execution(self):
        loop = build_loop(self.conn, positions={INSTRUMENT: 10.0})
        result = loop.run_cycle(NOW)
        self.assertGreater(result.discrepancies, 0)
        self.assertIn(BlockReason.RECONCILIATION_UNRESOLVED, blocks(result))
        self.assertEqual(result.orders_submitted, 0)
        self.assertEqual(loop.stack.transport.place_calls, 0)

    def test_the_block_persists_until_an_operator_accepts(self):
        loop = build_loop(self.conn, positions={INSTRUMENT: 10.0})
        loop.run_cycle(NOW)
        again = loop.run_cycle(NOW + timedelta(minutes=15))
        self.assertIn(BlockReason.RECONCILIATION_UNRESOLVED, blocks(again),
                      "the mismatch cleared itself without anyone resolving it")
        loop.accept_broker_positions(actor="operator", reason="manual test position",
                                     now=NOW + timedelta(minutes=20))
        cleared = loop.run_cycle(NOW + timedelta(minutes=30))
        self.assertNotIn(BlockReason.RECONCILIATION_UNRESOLVED, blocks(cleared))

    def test_our_own_fill_is_not_a_mismatch(self):
        from tests.trading.helpers import settle
        loop = build_loop(self.conn)
        loop.run_cycle(NOW)
        order = next(iter(loop.stack.orchestrator.orders.values()))
        settle(loop, order, 100.0)
        result = loop.run_cycle(NOW + timedelta(minutes=15))
        self.assertNotIn(BlockReason.RECONCILIATION_UNRESOLVED, blocks(result))

    def test_actuals_are_reconciled_only_after_reconciliation_agrees(self):
        loop = build_loop(self.conn, positions={INSTRUMENT: 10.0})
        loop.run_cycle(NOW)
        origins = {r[0] for r in self.conn.execute(
            "SELECT origin FROM position_actuals")}
        self.assertNotIn("broker_reconciled", origins)


# ======================================================================
# HIGH — no durable record before the venue call
# ======================================================================

class TestWriteAheadAndRestart(LoopCase):

    def test_an_order_is_durable_before_the_gateway_is_called(self):
        loop = build_loop(self.conn)
        seen = []
        original = loop.stack.gateway.submit_order

        def spy(order, now):
            seen.append(self.conn.execute(
                "SELECT state FROM execution_orders WHERE order_id = ?",
                (order.order_id,)).fetchone())
            return original(order, now)

        loop.stack.gateway.submit_order = spy
        loop.run_cycle(NOW)
        self.assertEqual(seen, [("submitting",)])

    def test_a_crash_after_the_venue_accepted_is_not_resubmitted(self):
        """
        Submit sent, broker accepted, process dies before the result is
        recorded. Restart must reconcile before it can submit again.
        """
        loop = build_loop(self.conn)
        with mock.patch.object(type(loop.stack.service), "_persist",
                               side_effect=KeyboardInterrupt("killed")):
            with self.assertRaises(KeyboardInterrupt):
                loop.run_cycle(NOW)
        self.assertEqual(loop.stack.transport.place_calls, 1)
        venue = loop.stack.transport

        restarted = build_loop(self.conn)
        restarted.stack.transport = venue                 # same broker
        for entry in [restarted.stack.orchestrator.registry.get("ibkr")]:
            entry.gateway.transport = venue
        self.assertEqual(restarted.stack.recovery.get("in_flight"), 1)
        result = restarted.run_cycle(NOW + timedelta(minutes=15))
        self.assertEqual(venue.place_calls, 1, "a blind duplicate was submitted")
        self.assertEqual(result.orders_submitted, 0)

    def test_a_failed_write_ahead_sends_nothing(self):
        loop = build_loop(self.conn)
        with mock.patch.object(loop.stack.repository, "save_execution",
                               side_effect=sqlite3.OperationalError("database is locked")):
            result = loop.run_cycle(NOW)
        self.assertEqual(loop.stack.transport.place_calls, 0)
        self.assertEqual(result.orders_submitted, 0)


# ======================================================================
# HIGH — two runners could operate one account
# ======================================================================

class _Session:
    def any_open(self, entries, now):
        return True


class _MarketData:
    session = _Session()

    def run_cycle(self, *args, **kwargs):
        class _C:
            requested = tradeable = 1
            bars_written = 0
        return _C()

    def health(self, now=None, limit=None):
        class _H:
            connected = True
        return _H()


class _Loop:
    class config:
        broker_id = "ibkr"
        account_id = "DU-FIXTURE"
        lease_owner = ""

    def __init__(self):
        self.calls = 0

    def run_cycle(self, now=None, worker=""):
        self.calls += 1

        class _R:
            cycle_id = "c"
            orders_submitted = 0
        return _R()

    def deployable_models(self):
        return {}


def runner(conn, clock, worker):
    initialize_market_data_schema(conn)
    built = SessionRunner(conn, _Loop(), _MarketData(), mode=RunMode.TEST_REPLAY,
                          clock=clock, schedule=Schedule.default(tick_seconds=60),
                          worker=worker)
    built._universe = lambda: []
    built._usable_prices = lambda now: {"x": 1.0}
    return built


class TestOneRunnerPerAccount(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "lease.db")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_second_runner_is_refused(self):
        first = runner(sqlite3.connect(self.path), ReplayClock(NOW), "a")
        first.start()
        second = runner(sqlite3.connect(self.path), ReplayClock(NOW), "b")
        with self.assertRaises(SessionRefused):
            second.start()

    def test_same_worker_name_in_two_processes_is_still_two_owners(self):
        first = runner(sqlite3.connect(self.path), ReplayClock(NOW), "same")
        first.start()
        with self.assertRaises(SessionRefused):
            runner(sqlite3.connect(self.path), ReplayClock(NOW), "same").start()

    def test_a_crashed_runner_is_taken_over_after_expiry(self):
        crashed = runner(sqlite3.connect(self.path), ReplayClock(NOW), "a")
        crashed.start()                                  # never closes
        later = NOW + timedelta(seconds=crashed.lease_ttl_seconds + 1)
        replacement = runner(sqlite3.connect(self.path), ReplayClock(later), "b")
        state = replacement.start()
        self.assertTrue(any("took over" in b for b in state.blocks))

    def test_a_runner_that_lost_its_lease_stops_acting(self):
        crashed = runner(sqlite3.connect(self.path), ReplayClock(NOW), "a")
        crashed.start()
        later = NOW + timedelta(seconds=crashed.lease_ttl_seconds + 1)
        runner(sqlite3.connect(self.path), ReplayClock(later), "b").start()
        tick = crashed.run_tick(later + timedelta(seconds=1))
        self.assertTrue(any("lease was lost" in b for b in tick.blocks))
        self.assertEqual(crashed.loop.calls, 0)

    def test_close_releases_for_the_next_session(self):
        first = runner(sqlite3.connect(self.path), ReplayClock(NOW), "a")
        first.start()
        first.close()
        runner(sqlite3.connect(self.path), ReplayClock(NOW), "b").start()

    def test_the_loop_refuses_to_trade_under_another_owner(self):
        conn = make_connection()
        universe(conn)
        store_signals(conn, [a_live_signal()])
        enable_paper(conn)
        loop = build_loop(conn)
        leases.acquire(conn, leases.lease_scope("ibkr", loop.stack.account_id),
                       "someone-else", NOW)
        result = loop.run_cycle(NOW)
        self.assertIn(BlockReason.RUNNER_NOT_OWNER, blocks(result))
        self.assertEqual(loop.stack.transport.place_calls, 0)
        conn.close()


# ======================================================================
# HIGH — no exchange calendar
# ======================================================================

def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


class TestExchangeCalendar(unittest.TestCase):
    calendar = USEquityCalendar()

    def status(self, text):
        return self.calendar.status(at(text))

    def test_published_2026_holidays_and_early_closes(self):
        self.assertEqual(sorted(holidays(2026)), [
            date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
            date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
            date(2026, 11, 26), date(2026, 12, 25)])
        self.assertEqual(sorted(early_closes(2026)),
                         [date(2026, 11, 27), date(2026, 12, 24)])

    def test_regular_boundaries_in_both_dst_regimes(self):
        self.assertIs(self.status("2026-09-16T13:29:59"), MarketStatus.PRE_MARKET)
        self.assertIs(self.status("2026-09-16T13:30:00"), MarketStatus.OPEN)
        self.assertIs(self.status("2026-09-16T19:59:59"), MarketStatus.OPEN)
        self.assertIs(self.status("2026-09-16T20:00:00"), MarketStatus.AFTER_HOURS)
        self.assertIs(self.status("2026-12-16T14:29:00"), MarketStatus.PRE_MARKET)
        self.assertIs(self.status("2026-12-16T14:30:00"), MarketStatus.OPEN)

    def test_holiday_weekend_and_early_close(self):
        self.assertIs(self.status("2026-09-07T15:00:00"), MarketStatus.HOLIDAY)
        self.assertIs(self.status("2026-09-19T15:00:00"), MarketStatus.CLOSED)
        self.assertIs(self.status("2026-11-27T17:59:00"), MarketStatus.OPEN)
        self.assertIs(self.status("2026-11-27T18:00:00"), MarketStatus.AFTER_HOURS)

    def test_the_gateway_obeys_the_exchange_over_a_fresh_venue_quote(self):
        """IBKR serves available quotes outside hours; that is not a session."""
        conn = make_connection()
        universe(conn)
        stack = build_stack(conn, actor="t", mock=True)
        resolution = stack.gateway.resolve_contract(INSTRUMENT, "AAPL",
                                                    sec_type="STK", currency="USD")
        self.assertTrue(resolution.ok)
        self.assertIsNotNone(stack.gateway.exchange_calendar)
        self.assertIs(stack.gateway.market_status(INSTRUMENT, NOW), MarketStatus.HOLIDAY)
        after = at("2026-09-16T22:00:00")
        self.assertIs(stack.gateway.market_status(INSTRUMENT, after),
                      MarketStatus.AFTER_HOURS)
        conn.close()


# ======================================================================
# THE STRUCTURAL STOP AND THE READINESS RESULT
# ======================================================================

def deployable(loop, verdict=True):
    """A deployable-model FIXTURE at the canonical gate's boundary."""
    return mock.patch.object(type(loop), "_model_governance",
                             lambda self: ({"tm-fixture-1": verdict},
                                           {"tm-fixture-1": "active" if verdict
                                            else "evaluated"}, ""))


class TestPreSubmissionStop(LoopCase):

    def guarded_loop(self, **kwargs):
        kwargs.setdefault("experimental", False)
        kwargs.setdefault("pre_submission_only", True)
        kwargs.setdefault("price_source", pricing.OPERATIONAL)
        loop = build_loop(self.conn, **kwargs)
        loop.stack.gateway = PreSubmissionGateway(loop.stack.gateway)
        entry = loop.stack.orchestrator.registry.get("ibkr")
        entry.gateway = loop.stack.gateway
        return loop

    def test_the_healthy_fixture_reaches_ready_to_submit_and_stops(self):
        """§75, the acceptance scenario."""
        quote(self.conn)
        loop = self.guarded_loop()
        with deployable(loop):
            result = loop.run_cycle(NOW)
        verdict = result.readiness
        self.assertEqual(verdict.verdict, readiness.READY_TO_SUBMIT, verdict.as_dict())
        self.assertTrue(verdict.system_ready)
        self.assertFalse(verdict.order_authorized)
        self.assertEqual(verdict.requests_ready, 1)
        self.assertEqual(result.orders_submitted, 0)
        self.assertEqual(loop.stack.transport.place_calls, 0)
        self.assertEqual(loop.stack.gateway.attempted_writes, [])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM execution_orders").fetchone()[0], 0)

    def test_the_trap_fires_when_a_forbidden_path_is_taken(self):
        """Negative control: the tripwire is live."""
        loop = self.guarded_loop()
        with self.assertRaises(BrokerSubmissionForbidden) as caught:
            loop.stack.gateway.submit_order(object(), NOW)
        self.assertEqual(caught.exception.code, FORBIDDEN_CODE)
        self.assertEqual(loop.stack.gateway.attempted_writes, ["submit_order"])
        self.assertEqual(loop.stack.transport.place_calls, 0)

    def test_the_stack_builder_guards_the_registered_gateway(self):
        conn = make_connection()
        stack = build_stack(conn, actor="t", mock=True, allow_paper_orders=True,
                            pre_submission_only=True)
        self.assertTrue(stack.gateway.submission_forbidden)
        self.assertIs(stack.orchestrator.registry.get("ibkr").gateway, stack.gateway)
        self.assertFalse(stack.may_submit)
        conn.close()

    def test_readiness_is_not_permission(self):
        quote(self.conn)
        loop = self.guarded_loop(allow_paper_orders=False)
        with deployable(loop):
            verdict = loop.run_cycle(NOW).readiness
        self.assertTrue(verdict.system_ready)
        self.assertFalse(verdict.order_authorized)
        self.assertTrue(any("paper ordering gate" in m
                            for m in verdict.authorization_missing))


class TestNoTradeClassification(LoopCase):
    """§70-§74 and §82: healthy nothing is not a failure."""

    def run_guarded(self, loop, deployable_verdict=True):
        loop.stack.gateway = PreSubmissionGateway(loop.stack.gateway)
        loop.stack.orchestrator.registry.get("ibkr").gateway = loop.stack.gateway
        with deployable(loop, deployable_verdict):
            result = loop.run_cycle(NOW)
        self.assertEqual(loop.stack.transport.place_calls, 0)
        return result.readiness

    def loop(self, **kwargs):
        kwargs.setdefault("experimental", False)
        kwargs.setdefault("price_source", pricing.OPERATIONAL)
        return build_loop(self.conn, **kwargs)

    def test_no_model_day_is_a_normal_no_trade(self):
        quote(self.conn)
        verdict = self.run_guarded(self.loop(), deployable_verdict=False)
        self.assertEqual(verdict.classification, readiness.NORMAL_NO_TRADE,
                         verdict.as_dict())
        self.assertIn("model", verdict.reasons[0])

    def test_no_signal_day_is_a_normal_no_trade(self):
        self.conn.execute("DELETE FROM signals")
        self.conn.commit()
        quote(self.conn)
        verdict = self.run_guarded(self.loop())
        self.assertEqual(verdict.classification, readiness.NORMAL_NO_TRADE)
        self.assertEqual(verdict.reasons, ["no live signal"])

    def test_risk_declined_day_places_nothing_and_says_why(self):
        self.conn.execute("DELETE FROM signals")
        self.conn.commit()
        store_signals(self.conn, [a_live_signal(confidence=0.30)])
        quote(self.conn)
        verdict = self.run_guarded(self.loop())
        self.assertEqual(verdict.verdict, readiness.NOT_READY)
        self.assertEqual(verdict.classification, readiness.NORMAL_NO_TRADE)

    def test_stale_data_day_is_a_temporary_block(self):
        quote(self.conn, age_seconds=3600)
        verdict = self.run_guarded(self.loop())
        self.assertEqual(verdict.classification, readiness.TEMPORARY_BLOCK)

    def test_reconciliation_blocked_day_is_a_temporary_block(self):
        quote(self.conn)
        verdict = self.run_guarded(self.loop(positions={INSTRUMENT: 10.0}))
        self.assertEqual(verdict.classification, readiness.TEMPORARY_BLOCK)
        self.assertFalse(verdict.system_ready)

    def test_mode_off_is_a_governance_hold(self):
        TradingModeStore(self.conn).set_mode(TradingMode.OFF, actor="op",
                                             reason="hold", at=NOW)
        quote(self.conn)
        verdict = self.run_guarded(self.loop())
        self.assertEqual(verdict.classification, readiness.GOVERNANCE_HOLD)

    def test_forbidden_live_mode_cannot_be_recorded_or_resolved(self):
        from src.domain.trading_loop_models import TradingModeRefused
        with self.assertRaises(TradingModeRefused):
            TradingModeStore(self.conn).set_mode(TradingMode.LIVE, actor="x",
                                                 reason="y", at=NOW)
        self.conn.execute("UPDATE trading_mode SET mode = 'live'")
        self.conn.commit()
        self.assertFalse(TradingModeStore(self.conn).resolve(NOW).may_trade)

    def test_an_environment_variable_cannot_grant_paper(self):
        TradingModeStore(self.conn).set_mode(TradingMode.OFF, actor="op",
                                             reason="hold", at=NOW)
        with mock.patch.dict(os.environ, {"MARKETLENS_TRADING_MODE": "paper"}):
            self.assertFalse(TradingModeStore(self.conn).resolve(NOW).may_trade)

    def test_a_crashed_stage_is_a_system_error(self):
        quote(self.conn)
        loop = self.loop()
        with mock.patch.object(type(loop.stack.orchestrator), "poll_broker",
                               side_effect=RuntimeError("disk")):
            verdict = self.run_guarded(loop)
        self.assertEqual(verdict.classification, readiness.SYSTEM_ERROR)

    def test_a_lapsed_broker_session_blocks(self):
        quote(self.conn)
        loop = self.loop()
        loop.stack.transport.connected = False
        loop.stack.gateway.heartbeat()                   # the venue went away
        verdict = self.run_guarded(loop)
        self.assertEqual(verdict.classification, readiness.TEMPORARY_BLOCK)


class TestFillsAgainstTheNewBaseline(LoopCase):
    """
    Partial, duplicate and restarted fills through the real loop, now that
    reconciliation compares our book: none of them may read as a mismatch,
    double a position, or re-order the remainder.
    """

    def test_partial_duplicate_and_restart_stay_consistent(self):
        loop = build_loop(self.conn, session_id="fills")
        loop.run_cycle(NOW)
        order = next(iter(loop.stack.orchestrator.orders.values()))
        venue = loop.stack.transport
        half = order.quantity / 2

        venue.duplicate_executions = True                 # every execution twice
        venue.fill(order.broker_order_id, half, 100.0)
        first = loop.run_cycle(NOW + timedelta(minutes=15))
        self.assertNotIn(BlockReason.RECONCILIATION_UNRESOLVED, blocks(first),
                         [b.detail for b in first.blocks])
        self.assertAlmostEqual(order.filled_quantity, half)

        restarted = build_loop(self.conn, session_id="fills")
        restarted.stack.transport = venue
        restarted.stack.orchestrator.registry.get("ibkr").gateway.transport = venue
        venue.fill(order.broker_order_id, half, 101.0)
        second = restarted.run_cycle(NOW + timedelta(minutes=30))
        self.assertNotIn(BlockReason.RECONCILIATION_UNRESOLVED, blocks(second),
                         [b.detail for b in second.blocks])
        self.assertEqual(venue.place_calls, 1, "the remainder was re-ordered")
        restored = next(iter(restarted.stack.orchestrator.orders.values()))
        self.assertAlmostEqual(restored.filled_quantity, order.quantity)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM execution_fills").fetchone()[0], 2)


class TestSessionClose(unittest.TestCase):
    """§67, §68: the runner stops at the exchange's close, early or not."""

    def run_session(self, start):
        calendar = USEquityCalendar()

        class _CalendarSession:
            def any_open(self, entries, now):
                return calendar.status(now) is MarketStatus.OPEN

        md = _MarketData()
        md.session = _CalendarSession()
        conn = sqlite3.connect(":memory:")
        built = runner(conn, ReplayClock(start), "close-test")
        built.market_data = md
        state = built.run_until_close()
        return built, state

    def test_an_early_close_ends_the_session_at_13_00_new_york(self):
        built, state = self.run_session(at("2026-11-27T17:30:00"))
        self.assertIsNotNone(state.closed_at)
        self.assertLessEqual(state.closed_at, at("2026-11-27T18:01:00"))
        self.assertGreater(state.ticks, 0)
        self.assertIsNone(leases.holder(built.conn, built.lease.scope,
                                        state.closed_at),
                          "the account lease outlived the session")

    def test_a_holiday_does_not_start(self):
        with self.assertRaises(SessionRefused):
            self.run_session(at("2026-11-26T15:00:00"))


class TestIdentity(unittest.TestCase):

    def test_client_order_id_is_deterministic_bounded_and_traceable(self):
        key = "a" * 64
        self.assertEqual(client_order_id(key), client_order_id(key))
        self.assertLessEqual(len(client_order_id(key)), 23)
        self.assertTrue(client_order_id(key).startswith("ml-"))
        self.assertNotEqual(client_order_id("b" + key[1:]), client_order_id(key))

    def test_a_pre_submission_rerun_at_the_same_anchor_is_one_cycle(self):
        conn = make_connection()
        universe(conn)
        store_signals(conn, [a_live_signal()])
        enable_paper(conn)
        quote(conn)
        loop = build_loop(conn, experimental=True, pre_submission_only=True,
                          price_source=pricing.OPERATIONAL)
        loop.run_cycle(NOW)
        second = loop.run_cycle(NOW + timedelta(seconds=10))
        self.assertIn(BlockReason.CYCLE_ALREADY_RUNNING, blocks(second))
        self.assertEqual(loop.stack.transport.place_calls, 0)
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
