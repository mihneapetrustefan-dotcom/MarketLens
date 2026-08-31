"""
tests/portfolio/test_constraints.py
----------------------------------------
Tests for the constraint set and its persistence.

The point of versioning constraints is that a past decision stays
reproducible after the limits change. That guarantee has one obvious
failure mode — silently falling back to today's defaults when the
recorded version is missing — and it is tested explicitly here.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.portfolio_models import (
    ConstraintScope, ConstraintSeverity, TradingState,
)
from src.portfolio.constraints import (
    ConstraintRepository, DEFAULT_CONSTRAINT_VERSION, default_constraint_set,
)
from tests.portfolio.helpers import make_connection


class TestDefaultConstraintSet(unittest.TestCase):
    def setUp(self):
        self.constraint_set = default_constraint_set()

    def test_every_constraint_has_a_description(self):
        """An undocumented limit cannot be judged by whoever inherits it."""
        for constraint in self.constraint_set.constraints:
            self.assertTrue(constraint.description.strip(), constraint.constraint_id)

    def test_constraint_ids_are_unique(self):
        ids = [c.constraint_id for c in self.constraint_set.constraints]
        self.assertEqual(len(ids), len(set(ids)))

    def test_trading_is_enabled_by_default(self):
        self.assertEqual(self.constraint_set.trading_state, TradingState.ENABLED)

    def test_structural_limits_are_hard(self):
        for scope in (ConstraintScope.POSITION_WEIGHT, ConstraintScope.SECTOR_WEIGHT,
                      ConstraintScope.GROSS_EXPOSURE, ConstraintScope.NET_EXPOSURE,
                      ConstraintScope.LEVERAGE):
            constraint = self.constraint_set.first(scope)
            self.assertIsNotNone(constraint, scope.value)
            self.assertEqual(constraint.severity, ConstraintSeverity.HARD, scope.value)

    def test_measured_risk_limits_are_soft(self):
        """These rest on estimates of a past window, so they escalate rather than reject."""
        for scope in (ConstraintScope.PORTFOLIO_VOLATILITY, ConstraintScope.DRAWDOWN,
                      ConstraintScope.CONCENTRATION_HHI):
            constraint = self.constraint_set.first(scope)
            self.assertIsNotNone(constraint, scope.value)
            self.assertEqual(constraint.severity, ConstraintSeverity.SOFT, scope.value)

    def test_signal_confidence_floor_is_stricter_than_phase_10_generation(self):
        from src.signals.validator import ValidationConfig
        floor = self.constraint_set.first(ConstraintScope.MIN_SIGNAL_CONFIDENCE)
        self.assertGreater(floor.min_value, ValidationConfig().min_confidence)

    def test_every_enabled_constraint_declares_a_bound(self):
        for constraint in self.constraint_set.enabled_constraints():
            self.assertTrue(
                constraint.max_value is not None or constraint.min_value is not None,
                constraint.constraint_id)


class TestConstraintPersistence(unittest.TestCase):
    def setUp(self):
        self.conn = make_connection()
        self.repository = ConstraintRepository(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_round_trip_preserves_every_constraint(self):
        original = default_constraint_set()
        self.repository.save(original)
        loaded = self.repository.load(original.version)

        self.assertIsNotNone(loaded)
        self.assertEqual(len(loaded.constraints), len(original.constraints))
        self.assertEqual(
            {c.constraint_id for c in loaded.constraints},
            {c.constraint_id for c in original.constraints})

    def test_round_trip_preserves_bounds_and_severity(self):
        self.repository.save(default_constraint_set())
        loaded = self.repository.load(DEFAULT_CONSTRAINT_VERSION)
        position = loaded.first(ConstraintScope.POSITION_WEIGHT)
        self.assertEqual(position.max_value, 0.20)
        self.assertEqual(position.severity, ConstraintSeverity.HARD)

    def test_round_trip_preserves_trading_state(self):
        original = default_constraint_set()
        original.trading_state = TradingState.REDUCE_ONLY
        original.version = "vX"
        self.repository.save(original)
        self.assertEqual(self.repository.load("vX").trading_state,
                         TradingState.REDUCE_ONLY)

    def test_unknown_version_returns_none_rather_than_current_defaults(self):
        """
        Silently substituting today's limits would let a replay claim to
        reproduce a decision it actually re-decided under different rules.
        """
        self.assertIsNone(self.repository.load("v-never-written"))

    def test_load_or_default_seeds_the_version_it_was_asked_for(self):
        seeded = self.repository.load_or_default("v7")
        self.assertEqual(seeded.version, "v7")
        # Now genuinely on record, not just returned in memory.
        self.assertIsNotNone(self.repository.load("v7"))

    def test_load_or_default_returns_the_stored_set_when_present(self):
        stored = default_constraint_set()
        stored.constraints = [c for c in stored.constraints
                              if c.scope != ConstraintScope.LEVERAGE]
        self.repository.save(stored)
        loaded = self.repository.load_or_default(stored.version)
        self.assertIsNone(loaded.first(ConstraintScope.LEVERAGE))

    def test_a_modified_version_does_not_disturb_an_earlier_one(self):
        v1 = default_constraint_set()
        self.repository.save(v1)

        v2 = default_constraint_set()
        v2.version = "v2"
        for constraint in v2.constraints:
            if constraint.scope == ConstraintScope.SECTOR_WEIGHT:
                constraint.max_value = 0.60
        self.repository.save(v2)

        self.assertEqual(
            self.repository.load("v1").first(ConstraintScope.SECTOR_WEIGHT).max_value, 0.40)
        self.assertEqual(
            self.repository.load("v2").first(ConstraintScope.SECTOR_WEIGHT).max_value, 0.60)

    def test_disabled_constraints_survive_the_round_trip(self):
        original = default_constraint_set()
        original.constraints[0].enabled = False
        original.version = "v-disabled"
        self.repository.save(original)
        loaded = self.repository.load("v-disabled")
        target = next(c for c in loaded.constraints
                      if c.constraint_id == original.constraints[0].constraint_id)
        self.assertFalse(target.enabled)


if __name__ == "__main__":
    unittest.main()
