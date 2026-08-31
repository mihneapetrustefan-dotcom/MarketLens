"""
tests/backtest/test_accounting.py
--------------------------------------
Position and cash accounting (spec §18, §19, §20, §92).

Accounting bugs are the quietest kind: the equity curve still looks
like an equity curve, and only the total is wrong. So these tests check
arithmetic against values computed by hand, and cover the flip case
explicitly — a sell larger than the long it is closing — because that
is where naive ledgers produce a negative quantity carrying a
meaningless average cost.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.backtest.accounting import LedgerPosition, PortfolioLedger
from src.domain.backtest_models import OrderSide, SimulatedFill
from src.domain.portfolio_models import PositionSource

AT = datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc)


def fill(instrument_id: str, side: OrderSide, quantity: float, price: float,
         at: datetime = AT, commission: float = 0.0,
         slippage: float = 0.0) -> SimulatedFill:
    return SimulatedFill(
        fill_id=f"f-{instrument_id}-{at.isoformat()}-{quantity}", run_id="r",
        order_id="o", instrument_id=instrument_id, side=side, quantity=quantity,
        price=price, reference_price=price, filled_at=at,
        commission=commission, slippage_cost=slippage)


class TestOpeningAndCash(unittest.TestCase):
    def setUp(self):
        self.ledger = PortfolioLedger(10_000.0, run_id="r")

    def test_buying_reduces_cash_by_notional_plus_costs(self):
        self.ledger.apply_fill(fill("a", OrderSide.BUY, 10, 100.0, commission=5.0))
        self.assertAlmostEqual(self.ledger.cash, 10_000.0 - 1_000.0 - 5.0)

    def test_buying_opens_a_position_at_the_fill_price(self):
        self.ledger.apply_fill(fill("a", OrderSide.BUY, 10, 100.0))
        position = self.ledger.positions["a"]
        self.assertEqual(position.quantity, 10)
        self.assertEqual(position.average_cost, 100.0)

    def test_opening_produces_no_trade(self):
        """P&L is undefined until something closes."""
        self.assertIsNone(self.ledger.apply_fill(fill("a", OrderSide.BUY, 10, 100.0)))

    def test_costs_are_accumulated_separately(self):
        self.ledger.apply_fill(
            fill("a", OrderSide.BUY, 10, 100.0, commission=5.0, slippage=2.0))
        self.assertAlmostEqual(self.ledger.total_costs, 5.0)
        self.assertAlmostEqual(self.ledger.total_slippage, 2.0)

    def test_traded_notional_accumulates_for_turnover(self):
        self.ledger.apply_fill(fill("a", OrderSide.BUY, 10, 100.0))
        self.ledger.apply_fill(fill("b", OrderSide.BUY, 5, 40.0))
        self.assertAlmostEqual(self.ledger.traded_notional, 1_000.0 + 200.0)

    def test_zero_quantity_fill_is_ignored(self):
        self.ledger.apply_fill(fill("a", OrderSide.BUY, 0.0, 100.0))
        self.assertEqual(self.ledger.cash, 10_000.0)


class TestAverageCost(unittest.TestCase):
    def setUp(self):
        self.ledger = PortfolioLedger(100_000.0, run_id="r")

    def test_adding_blends_the_average(self):
        self.ledger.apply_fill(fill("a", OrderSide.BUY, 10, 100.0))
        self.ledger.apply_fill(fill("a", OrderSide.BUY, 10, 120.0))
        position = self.ledger.positions["a"]
        self.assertEqual(position.quantity, 20)
        self.assertAlmostEqual(position.average_cost, 110.0)

    def test_reducing_leaves_the_average_unchanged(self):
        self.ledger.apply_fill(fill("a", OrderSide.BUY, 20, 100.0))
        self.ledger.apply_fill(fill("a", OrderSide.SELL, 5, 130.0))
        position = self.ledger.positions["a"]
        self.assertEqual(position.quantity, 15)
        self.assertAlmostEqual(position.average_cost, 100.0)

    def test_reducing_realises_against_the_average(self):
        self.ledger.apply_fill(fill("a", OrderSide.BUY, 20, 100.0))
        trade = self.ledger.apply_fill(fill("a", OrderSide.SELL, 5, 130.0))
        self.assertIsNotNone(trade)
        self.assertAlmostEqual(trade.gross_pnl, (130.0 - 100.0) * 5)
        self.assertAlmostEqual(self.ledger.realized_pnl, 150.0)


class TestClosingAndFlipping(unittest.TestCase):
    def setUp(self):
        self.ledger = PortfolioLedger(100_000.0, run_id="r")

    def test_closing_fully_clears_the_position(self):
        self.ledger.apply_fill(fill("a", OrderSide.BUY, 10, 100.0))
        self.ledger.apply_fill(fill("a", OrderSide.SELL, 10, 110.0))
        self.assertFalse(self.ledger.positions["a"].is_open)

    def test_closing_produces_a_trade_with_correct_pnl(self):
        self.ledger.apply_fill(fill("a", OrderSide.BUY, 10, 100.0))
        trade = self.ledger.apply_fill(
            fill("a", OrderSide.SELL, 10, 110.0, commission=3.0))
        self.assertAlmostEqual(trade.gross_pnl, 100.0)
        self.assertAlmostEqual(trade.costs, 3.0)
        self.assertAlmostEqual(trade.net_pnl, 97.0)
        self.assertTrue(trade.is_win)

    def test_a_losing_close_is_not_a_win(self):
        self.ledger.apply_fill(fill("a", OrderSide.BUY, 10, 100.0))
        trade = self.ledger.apply_fill(fill("a", OrderSide.SELL, 10, 90.0))
        self.assertAlmostEqual(trade.net_pnl, -100.0)
        self.assertFalse(trade.is_win)

    def test_selling_more_than_held_flips_to_a_short(self):
        """
        The case naive ledgers break on: 10 long, sell 25.
        The long must close and realise, and a 15 short must open at
        the fill price — not a -15 position carrying the old average.
        """
        self.ledger.apply_fill(fill("a", OrderSide.BUY, 10, 100.0))
        trade = self.ledger.apply_fill(fill("a", OrderSide.SELL, 25, 120.0))

        self.assertIsNotNone(trade)
        self.assertAlmostEqual(trade.quantity, 10)
        self.assertAlmostEqual(trade.gross_pnl, (120.0 - 100.0) * 10)

        position = self.ledger.positions["a"]
        self.assertAlmostEqual(position.quantity, -15)
        self.assertAlmostEqual(position.average_cost, 120.0)
        self.assertEqual(position.opened_at, AT)

    def test_short_pnl_is_correct_when_price_falls(self):
        self.ledger.apply_fill(fill("a", OrderSide.SELL, 10, 100.0))
        trade = self.ledger.apply_fill(fill("a", OrderSide.BUY, 10, 80.0))
        self.assertAlmostEqual(trade.gross_pnl, 200.0)

    def test_short_pnl_is_negative_when_price_rises(self):
        self.ledger.apply_fill(fill("a", OrderSide.SELL, 10, 100.0))
        trade = self.ledger.apply_fill(fill("a", OrderSide.BUY, 10, 120.0))
        self.assertAlmostEqual(trade.gross_pnl, -200.0)

    def test_short_proceeds_are_credited_to_cash(self):
        self.ledger.apply_fill(fill("a", OrderSide.SELL, 10, 100.0))
        self.assertAlmostEqual(self.ledger.cash, 100_000.0 + 1_000.0)


class TestMarkToMarket(unittest.TestCase):
    def setUp(self):
        self.ledger = PortfolioLedger(10_000.0, run_id="r")
        self.ledger.apply_fill(fill("a", OrderSide.BUY, 10, 100.0))

    def test_equity_reflects_price_movement(self):
        snapshot = self.ledger.mark_to_market(AT, {"a": 120.0})
        self.assertAlmostEqual(snapshot.positions_value, 1_200.0)
        self.assertAlmostEqual(snapshot.equity, 9_000.0 + 1_200.0)

    def test_missing_price_is_counted_not_valued_at_cost(self):
        """
        Carrying an unpriceable holding at its purchase price silently
        asserts it has not moved. It is excluded and counted instead.
        """
        snapshot = self.ledger.mark_to_market(AT, {"a": None})
        self.assertEqual(snapshot.unpriced_positions, 1)
        self.assertAlmostEqual(snapshot.positions_value, 0.0)

    def test_short_adds_to_gross_and_short_exposure(self):
        ledger = PortfolioLedger(10_000.0, run_id="r")
        ledger.apply_fill(fill("a", OrderSide.SELL, 10, 100.0))
        snapshot = ledger.mark_to_market(AT, {"a": 100.0})
        self.assertAlmostEqual(snapshot.gross_exposure, 1_000.0)
        self.assertAlmostEqual(snapshot.short_exposure, 1_000.0)
        self.assertAlmostEqual(snapshot.net_exposure, -1_000.0)

    def test_excursions_are_tracked_across_marks(self):
        self.ledger.mark_to_market(AT, {"a": 130.0})
        self.ledger.mark_to_market(AT, {"a": 80.0})
        position = self.ledger.positions["a"]
        self.assertAlmostEqual(position.max_favourable, 300.0)
        self.assertAlmostEqual(position.max_adverse, -200.0)


class TestBridgeToPhase11(unittest.TestCase):
    def test_positions_convert_to_phase_11_type(self):
        ledger = PortfolioLedger(10_000.0, run_id="r")
        ledger.apply_fill(fill("a", OrderSide.BUY, 10, 100.0))
        positions = ledger.to_positions("pf", AT)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].instrument_id, "a")
        self.assertEqual(positions[0].quantity, 10)
        self.assertEqual(positions[0].source, PositionSource.SIMULATED)

    def test_closed_positions_are_not_exported(self):
        ledger = PortfolioLedger(10_000.0, run_id="r")
        ledger.apply_fill(fill("a", OrderSide.BUY, 10, 100.0))
        ledger.apply_fill(fill("a", OrderSide.SELL, 10, 100.0))
        self.assertEqual(ledger.to_positions("pf", AT), [])


class TestLiquidation(unittest.TestCase):
    def test_close_all_realises_open_positions(self):
        ledger = PortfolioLedger(10_000.0, run_id="r")
        ledger.apply_fill(fill("a", OrderSide.BUY, 10, 100.0))
        closed = ledger.close_all(AT + timedelta(days=1), {"a": 110.0})
        self.assertEqual(len(closed), 1)
        self.assertAlmostEqual(closed[0].gross_pnl, 100.0)
        self.assertFalse(ledger.positions["a"].is_open)

    def test_unpriced_positions_are_left_open_rather_than_invented(self):
        ledger = PortfolioLedger(10_000.0, run_id="r")
        ledger.apply_fill(fill("a", OrderSide.BUY, 10, 100.0))
        closed = ledger.close_all(AT + timedelta(days=1), {"a": None})
        self.assertEqual(closed, [])
        self.assertTrue(ledger.positions["a"].is_open)

    def test_liquidation_charges_no_costs(self):
        ledger = PortfolioLedger(10_000.0, run_id="r")
        ledger.apply_fill(fill("a", OrderSide.BUY, 10, 100.0))
        before = ledger.total_costs
        ledger.close_all(AT + timedelta(days=1), {"a": 110.0})
        self.assertAlmostEqual(ledger.total_costs, before)


class TestConstruction(unittest.TestCase):
    def test_non_positive_capital_is_rejected(self):
        with self.assertRaises(ValueError):
            PortfolioLedger(0.0)
        with self.assertRaises(ValueError):
            PortfolioLedger(-100.0)


if __name__ == "__main__":
    unittest.main()
