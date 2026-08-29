"""
tests/signals/test_signal_models.py
-----------------------------------------------------------
Tests for the Phase 10 signal domain model and persistence.

The properties defended here are the structural ones: that a signal
carries no quantity, that suppression preserves rather than discards,
that NEUTRAL and NO_SIGNAL stay distinct, and that identity is
deterministic over information state rather than over output.
"""

import dataclasses
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.signal_schema import initialize_signal_schema
from src.data_access.signal_repository import SignalRepository, compute_identity_hash
from src.domain.signal_models import (
    AgreementState, ModelContribution, Signal, SignalCandidate, SignalContext,
    SignalDirection, SignalExplanation, SignalProvenance, SignalStatus,
    SignalStrategyDefinition, SignalType, SuppressionReason,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def make_signal(signal_id="sig-1", **overrides):
    defaults = dict(
        signal_id=signal_id, instrument_id="inst-nvda",
        signal_type=SignalType.DIRECTIONAL, direction=SignalDirection.LONG,
        status=SignalStatus.ACTIVE, strength=0.6, confidence=0.55,
        probability_up=0.62, agreement_state=AgreementState.AGREEMENT,
        provenance=SignalProvenance(strategy_id="st1", strategy_version="v1",
                                    configuration_version="v1",
                                    source_information_cutoff=NOW),
        context=SignalContext(market_regime="normal", volatility_percentile=0.4),
        created_at=NOW, valid_from=NOW, valid_until=NOW + timedelta(days=5),
    )
    defaults.update(overrides)
    return Signal(**defaults)


class TestStructuralCommitments(unittest.TestCase):
    """A signal must not be able to express a trade."""

    def test_signal_has_no_quantity_field(self):
        names = {f.name for f in dataclasses.fields(Signal)}
        for forbidden in ("quantity", "position_size", "notional", "order_size",
                          "account_id", "broker"):
            self.assertNotIn(forbidden, names)

    def test_signals_table_has_no_quantity_column(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            initialize_signal_schema(conn)
            columns = {r[1] for r in conn.execute("PRAGMA table_info(signals)")}
            conn.close()
            for forbidden in ("quantity", "position_size", "notional", "account_id"):
                self.assertNotIn(forbidden, columns)
        finally:
            os.remove(path)


class TestSignalValidation(unittest.TestCase):
    def test_strength_outside_unit_range_is_rejected(self):
        with self.assertRaises(ValueError):
            make_signal(strength=1.5)

    def test_confidence_outside_unit_range_is_rejected(self):
        with self.assertRaises(ValueError):
            make_signal(confidence=-0.1)

    def test_naive_datetime_is_rejected(self):
        with self.assertRaises(ValueError):
            make_signal(created_at=datetime(2026, 8, 20, 12, 0))

    def test_probability_down_is_derived_not_stored(self):
        names = {f.name for f in dataclasses.fields(Signal)}
        self.assertNotIn("probability_down", names)
        self.assertAlmostEqual(make_signal(probability_up=0.62).probability_down, 0.38)


class TestDirectionSemantics(unittest.TestCase):
    def test_neutral_and_no_signal_are_distinct(self):
        # NEUTRAL is a claim; NO_SIGNAL is the absence of one.
        self.assertNotEqual(SignalDirection.NEUTRAL, SignalDirection.NO_SIGNAL)

    def test_neutral_is_not_actionable(self):
        self.assertFalse(make_signal(direction=SignalDirection.NEUTRAL).is_actionable)

    def test_long_active_signal_is_actionable(self):
        self.assertTrue(make_signal().is_actionable)


class TestSuppression(unittest.TestCase):
    def test_suppression_preserves_the_signal_and_records_the_reason(self):
        signal = make_signal()
        signal.suppress(SuppressionReason.LOW_CONFIDENCE, "below threshold")
        self.assertEqual(signal.status, SignalStatus.SUPPRESSED)
        self.assertIn(SuppressionReason.LOW_CONFIDENCE, signal.suppression_reasons)
        self.assertIn("below threshold", signal.suppression_note)

    def test_suppressed_signal_is_not_actionable(self):
        signal = make_signal()
        signal.suppress(SuppressionReason.STALE_PREDICTION)
        self.assertFalse(signal.is_actionable)

    def test_suppression_adds_a_caveat_to_the_explanation(self):
        signal = make_signal()
        signal.suppress(SuppressionReason.MODEL_CONFLICT)
        self.assertTrue(any("model_conflict" in c for c in signal.explanation.caveats))

    def test_repeated_suppression_does_not_duplicate_the_reason(self):
        signal = make_signal()
        signal.suppress(SuppressionReason.LOW_CONFIDENCE)
        signal.suppress(SuppressionReason.LOW_CONFIDENCE)
        self.assertEqual(len(signal.suppression_reasons), 1)


class TestExpiry(unittest.TestCase):
    def test_signal_is_expired_after_valid_until(self):
        self.assertTrue(make_signal().is_expired_at(NOW + timedelta(days=10)))

    def test_signal_is_not_expired_within_window(self):
        self.assertFalse(make_signal().is_expired_at(NOW + timedelta(days=1)))


class TestIdentityHash(unittest.TestCase):
    def test_same_information_state_yields_same_hash(self):
        a = compute_identity_hash("st1", "v1", "v1", "inst-nvda", NOW)
        b = compute_identity_hash("st1", "v1", "v1", "inst-nvda", NOW)
        self.assertEqual(a, b)

    def test_different_cutoff_yields_different_hash(self):
        a = compute_identity_hash("st1", "v1", "v1", "inst-nvda", NOW)
        b = compute_identity_hash("st1", "v1", "v1", "inst-nvda", NOW + timedelta(days=1))
        self.assertNotEqual(a, b)

    def test_different_strategy_version_yields_different_hash(self):
        a = compute_identity_hash("st1", "v1", "v1", "inst-nvda", NOW)
        b = compute_identity_hash("st1", "v2", "v1", "inst-nvda", NOW)
        self.assertNotEqual(a, b)


class TestRepository(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.db_path)
        initialize_signal_schema(self.conn)
        self.repo = SignalRepository(self.conn)

    def tearDown(self):
        self.conn.close()
        os.remove(self.db_path)

    def test_schema_is_safe_to_run_twice(self):
        initialize_signal_schema(self.conn)  # must not raise

    def test_round_trip_preserves_every_field(self):
        signal = make_signal()
        signal.explanation.summary = "test summary"
        signal.explanation.add_factor("momentum positive")
        signal.explanation.add_caveat("small sample")
        signal.contributions.append(ModelContribution(
            prediction_id="pr-1", trained_model_id="tm-1", model_qualified_id="m:v1",
            predicted_value=0.02, confidence=0.6, weight=1.0))
        self.repo.save(signal)

        back = self.repo.get("sig-1")
        self.assertEqual(back.direction, SignalDirection.LONG)
        self.assertEqual(back.strength, 0.6)
        self.assertEqual(back.agreement_state, AgreementState.AGREEMENT)
        self.assertEqual(back.explanation.factors, ["momentum positive"])
        self.assertEqual(back.explanation.caveats, ["small sample"])
        self.assertEqual(len(back.contributions), 1)
        self.assertEqual(back.contributions[0].prediction_id, "pr-1")
        self.assertEqual(back.provenance.strategy_id, "st1")
        self.assertEqual(back.provenance.source_information_cutoff, NOW)
        self.assertEqual(back.context.market_regime, "normal")

    def test_suppression_reasons_survive_a_round_trip(self):
        signal = make_signal()
        signal.suppress(SuppressionReason.POOR_DATA_QUALITY, "missing features")
        self.repo.save(signal)
        back = self.repo.get("sig-1")
        self.assertIn(SuppressionReason.POOR_DATA_QUALITY, back.suppression_reasons)
        self.assertEqual(back.status, SignalStatus.SUPPRESSED)

    def test_find_by_identity_detects_a_duplicate_claim(self):
        self.repo.save(make_signal())
        identity = compute_identity_hash("st1", "v1", "v1", "inst-nvda", NOW)
        self.assertIsNotNone(self.repo.find_by_identity(identity))

    def test_find_by_identity_misses_a_different_information_state(self):
        self.repo.save(make_signal())
        identity = compute_identity_hash("st1", "v1", "v1", "inst-nvda", NOW + timedelta(days=1))
        self.assertIsNone(self.repo.find_by_identity(identity))

    def test_supersede_preserves_the_original_claim(self):
        self.repo.save(make_signal())
        self.repo.save(make_signal(signal_id="sig-2"))
        self.repo.supersede("sig-1", "sig-2")
        old = self.repo.get("sig-1")
        self.assertEqual(old.status, SignalStatus.SUPERSEDED)
        self.assertEqual(old.superseded_by, "sig-2")
        # The claim itself is untouched.
        self.assertEqual(old.direction, SignalDirection.LONG)
        self.assertEqual(old.strength, 0.6)

    def test_expire_before_only_touches_active_signals(self):
        active = make_signal()
        suppressed = make_signal(signal_id="sig-2")
        suppressed.suppress(SuppressionReason.LOW_CONFIDENCE)
        self.repo.save(active)
        self.repo.save(suppressed)

        changed = self.repo.expire_before(NOW + timedelta(days=10))
        self.assertEqual(changed, 1)
        self.assertEqual(self.repo.get("sig-1").status, SignalStatus.EXPIRED)
        # A suppressed signal keeps its reason rather than being expired.
        self.assertEqual(self.repo.get("sig-2").status, SignalStatus.SUPPRESSED)

    def test_signals_as_of_filters_on_information_cutoff_not_creation(self):
        # A signal built from OLD information must not appear when
        # replaying an even older moment, regardless of when written.
        self.repo.save(make_signal())
        self.assertEqual(len(self.repo.signals_as_of(NOW - timedelta(days=1))), 0)
        self.assertEqual(len(self.repo.signals_as_of(NOW + timedelta(days=1))), 1)

    def test_active_signals_can_filter_by_instrument(self):
        self.repo.save(make_signal())
        self.repo.save(make_signal(signal_id="sig-2", instrument_id="inst-msft"))
        self.assertEqual(len(self.repo.active_signals()), 2)
        self.assertEqual(len(self.repo.active_signals("inst-nvda")), 1)


class TestStrategyRegistry(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.db_path)
        initialize_signal_schema(self.conn)
        self.repo = SignalRepository(self.conn)

    def tearDown(self):
        self.conn.close()
        os.remove(self.db_path)

    def test_strategy_round_trip(self):
        definition = SignalStrategyDefinition(
            strategy_id="momentum", name="Momentum", version="v1",
            signal_type=SignalType.DIRECTIONAL, parameters={"threshold": 0.5},
            created_at=NOW)
        self.repo.save_strategy(definition)
        back = self.repo.get_strategy("momentum", "v1")
        self.assertEqual(back.name, "Momentum")
        self.assertEqual(back.parameters["threshold"], 0.5)
        self.assertEqual(back.qualified_id, "momentum:v1")

    def test_inactive_strategy_is_excluded_from_active_list(self):
        self.repo.save_strategy(SignalStrategyDefinition(
            strategy_id="a", name="A", version="v1",
            signal_type=SignalType.DIRECTIONAL, created_at=NOW))
        self.repo.save_strategy(SignalStrategyDefinition(
            strategy_id="b", name="B", version="v1", is_active=False,
            signal_type=SignalType.DIRECTIONAL, created_at=NOW))
        active = self.repo.active_strategies()
        self.assertEqual([s.strategy_id for s in active], ["a"])

    def test_two_versions_of_a_strategy_coexist(self):
        for version in ("v1", "v2"):
            self.repo.save_strategy(SignalStrategyDefinition(
                strategy_id="momentum", name="Momentum", version=version,
                signal_type=SignalType.DIRECTIONAL, created_at=NOW))
        self.assertIsNotNone(self.repo.get_strategy("momentum", "v1"))
        self.assertIsNotNone(self.repo.get_strategy("momentum", "v2"))


if __name__ == "__main__":
    unittest.main()
