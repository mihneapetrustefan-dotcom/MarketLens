"""
tests/trading/test_health_contract.py
--------------------------------------------
`assess_health` is the gate every order intent passes through, and its
docstring states a contract in one sentence:

    "Each parameter is Optional and None means UNMEASURED, which reads
     as DEGRADED rather than HEALTHY. That asymmetry is the point: a
     component nobody could check is not a component that passed."

Nothing tested that sentence. The loop-level tests
(tests/trading/test_fail_safe_and_boundary.py) prove the cycle places
no orders when market data is missing, a signal is stale, the gateway is
down or the account is unreadable -- but they go through `run_cycle`,
so they cannot distinguish "BLOCKED because the health gate said so"
from "no orders because some other stage happened to stop first".

These tests pin the gate itself: which readings block, which degrade,
and -- the part that matters for a system whose capture has been down
for 35 days -- that absent data never reads as healthy data.

Offline: no store, no broker, no network.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.trading_loop_models import (
    LoopHealth, ModeResolution, ModeSource, TradingMode,
)
from src.trading.accounts import (
    DEFAULT_MAX_ACCOUNT_AGE_SECONDS, DEFAULT_MAX_PRICE_AGE_DAYS, assess_health,
)

NOW = datetime(2026, 10, 30, 14, 0, tzinfo=timezone.utc)

PAPER = ModeResolution(mode=TradingMode.PAPER, source=ModeSource.STORED,
                       reason="", resolved_at=NOW)


def _reading(report, component):
    return next(r for r in report.readings if r.component == component)


def health(**overrides):
    """A fully measured, healthy call; override one input per test."""
    kwargs = dict(
        mode=PAPER,
        kill_switch=False,
        account=_KnownAccount(),
        connection_healthy=True,
        market_data_age_days=0.01,
        signals_available=3,
        portfolio_known=True,
        risk_known=True,
        execution_ready=True,
        reconciliation_clean=True,
        database_ok=True,
        scheduler_age_seconds=60.0,
    )
    kwargs.update(overrides)
    return assess_health("cycle-1", NOW, **kwargs)


class _KnownAccount:
    """The smallest thing `assess_health` will accept as a read account."""
    is_known = True
    detail = ""
    equity = 100_000.0
    base_currency = "USD"
    observed_at = NOW

    def age_seconds(self, now):                              # noqa: ARG002
        return 5.0


class TestTheBaselineIsActuallyHealthy(unittest.TestCase):

    def test_a_fully_measured_healthy_call_is_healthy(self):
        # Without this, every test below would pass for the wrong reason.
        report = health()
        self.assertIs(report.overall, LoopHealth.HEALTHY, report.blocking)


class TestUnmeasuredIsNotHealthy(unittest.TestCase):
    """None means nobody could check, and that is never a pass."""

    def test_no_input_may_read_healthy_when_it_is_none(self):
        for field in ("connection_healthy", "market_data_age_days",
                      "signals_available", "portfolio_known", "risk_known",
                      "execution_ready", "reconciliation_clean",
                      "scheduler_age_seconds"):
            with self.subTest(field=field):
                report = health(**{field: None})
                self.assertIsNot(
                    report.overall, LoopHealth.HEALTHY,
                    f"{field}=None read as HEALTHY: an unmeasured component "
                    f"was treated as a component that passed")

    def test_an_absent_account_blocks_rather_than_degrades(self):
        report = health(account=None)
        self.assertIs(report.overall, LoopHealth.BLOCKED)
        self.assertIs(_reading(report, "account_state").state, LoopHealth.BLOCKED)


class TestMarketDataGate(unittest.TestCase):
    """
    The capture outage case. Production market data is 35 days old as of
    2026-10-30; these are the readings that must follow from that.
    """

    def test_absent_market_data_blocks(self):
        report = health(market_data_age_days=None)
        reading = _reading(report, "market_data")
        self.assertIs(reading.state, LoopHealth.BLOCKED)
        self.assertIn("no market data", reading.detail)
        self.assertIs(report.overall, LoopHealth.BLOCKED)

    def test_market_data_older_than_the_limit_blocks(self):
        report = health(market_data_age_days=35.0)
        reading = _reading(report, "market_data")
        self.assertIs(reading.state, LoopHealth.BLOCKED)
        self.assertIs(report.overall, LoopHealth.BLOCKED)

    def test_the_limit_itself_is_not_a_block(self):
        # Boundary: the rule is "older than", not "at least".
        report = health(market_data_age_days=DEFAULT_MAX_PRICE_AGE_DAYS)
        self.assertIs(_reading(report, "market_data").state, LoopHealth.HEALTHY)

    def test_one_day_past_the_limit_blocks(self):
        report = health(market_data_age_days=DEFAULT_MAX_PRICE_AGE_DAYS + 1)
        self.assertIs(_reading(report, "market_data").state, LoopHealth.BLOCKED)

    def test_a_stale_age_is_reported_in_seconds_for_an_operator(self):
        report = health(market_data_age_days=35.0)
        self.assertAlmostEqual(_reading(report, "market_data").age_seconds,
                               35.0 * 86400.0, places=3)


class TestNoSignalIsNotAFailure(unittest.TestCase):

    def test_zero_signals_is_healthy_because_placing_nothing_is_correct(self):
        report = health(signals_available=0)
        reading = _reading(report, "signals")
        self.assertIs(reading.state, LoopHealth.HEALTHY)
        self.assertIs(report.overall, LoopHealth.HEALTHY)

    def test_an_unread_signal_table_is_not_the_same_as_no_signals(self):
        self.assertIs(_reading(health(signals_available=None), "signals").state,
                      LoopHealth.DEGRADED)


class TestOneBlockedComponentBlocksEverything(unittest.TestCase):
    """`overall` is the worst reading, not an average (§27)."""

    def test_a_single_block_outweighs_every_healthy_reading(self):
        report = health(connection_healthy=False)
        self.assertIs(report.overall, LoopHealth.BLOCKED)
        self.assertEqual([r.component for r in report.blocking],
                         ["broker_connection"])

    def test_the_kill_switch_blocks_on_its_own(self):
        self.assertIs(health(kill_switch=True).overall, LoopHealth.BLOCKED)

    def test_a_mode_that_may_not_trade_blocks_on_its_own(self):
        off = ModeResolution(mode=TradingMode.OFF, source=ModeSource.STORED,
                             reason="stood down", resolved_at=NOW)
        report = health(mode=off)
        self.assertIs(report.overall, LoopHealth.BLOCKED)
        self.assertIs(_reading(report, "trading_mode").state, LoopHealth.BLOCKED)

    def test_an_unresolved_reconciliation_discrepancy_blocks(self):
        report = health(reconciliation_clean=False)
        self.assertIs(report.overall, LoopHealth.BLOCKED)

    def test_an_unwritable_database_blocks(self):
        self.assertIs(health(database_ok=False).overall, LoopHealth.BLOCKED)


class TestAStaleAccountBlocks(unittest.TestCase):

    def test_an_account_past_the_age_limit_blocks(self):
        # Derived from the configured limit, so this keeps testing the
        # boundary if the limit moves; a literal would quietly stop.
        class Old(_KnownAccount):
            def age_seconds(self, now):                      # noqa: ARG002
                return DEFAULT_MAX_ACCOUNT_AGE_SECONDS + 1.0
        report = health(account=Old())
        self.assertIs(_reading(report, "account_state").state, LoopHealth.BLOCKED)

    def test_an_account_that_is_not_known_blocks(self):
        class Unknown(_KnownAccount):
            is_known = False
            detail = "the gateway returned nothing"
        report = health(account=Unknown())
        self.assertIs(_reading(report, "account_state").state, LoopHealth.BLOCKED)


class TestTheAssessmentRefusesABadClock(unittest.TestCase):

    def test_a_naive_now_is_refused(self):
        # ValueError specifically, and for the clock: assertRaises(Exception)
        # would also pass on a TypeError from a mistyped keyword below,
        # and would keep passing if the guard were removed entirely.
        with self.assertRaisesRegex(ValueError, "now"):
            health_now = NOW.replace(tzinfo=None)
            assess_health("cycle-1", health_now, mode=PAPER, kill_switch=False,
                          account=_KnownAccount(), connection_healthy=True,
                          market_data_age_days=0.01, signals_available=1,
                          portfolio_known=True, risk_known=True,
                          execution_ready=True, reconciliation_clean=True)


if __name__ == "__main__":
    unittest.main()
