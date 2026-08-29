"""
tests/signals/test_signal_engine.py
-----------------------------------------------------------
Tests for the Phase 10 signal engine.

The engine's job is orchestration, so these tests defend the
orchestration invariants: that no candidate is ever lost, that
duplicates and supersessions are distinguished, that a suppressed
signal never silences a valid earlier one, and that one broken
strategy cannot take down a batch.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.signal_schema import initialize_signal_schema
from src.data_access.signal_repository import SignalRepository
from src.domain.signal_models import (
    SignalCandidate, SignalContext, SignalStatus, SignalStrategyDefinition, SignalType,
)
from src.signals.engine import GenerationReport, SignalEngine
from src.signals.strategy import GenerationContext, MLDirectionalStrategy, SignalStrategy
from src.signals.validator import SignalValidator, ValidationConfig

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


@dataclass
class FakePrediction:
    prediction_id: str
    trained_model_id: str
    model_qualified_id: str
    predicted_value: Optional[float]
    confidence: Optional[float] = None
    class_probabilities: Optional[Dict] = None
    is_abstention: bool = False
    abstention_reason: Optional[str] = None


def make_strategy(**parameters):
    params = {"strength_scale": 0.05, "horizon_days": 5}
    params.update(parameters)
    return MLDirectionalStrategy(SignalStrategyDefinition(
        strategy_id="ml_dir", name="ML Directional", version="v1",
        signal_type=SignalType.DIRECTIONAL, parameters=params, created_at=NOW))


def make_context(instrument="inst-nvda", cutoff=NOW, value=0.03, confidence=0.8):
    return GenerationContext(
        instrument_id=instrument, information_cutoff=cutoff,
        predictions=[
            FakePrediction("pr-1", "tm-1", "m1:v1", value, confidence),
            FakePrediction("pr-2", "tm-2", "m2:v1", value * 0.9, confidence),
        ],
        observation_id="obs-1",
        context=SignalContext(data_quality_level="high"))


class BrokenStrategy(SignalStrategy):
    def generate(self, context):
        raise RuntimeError("deliberately broken")


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.db_path)
        initialize_signal_schema(self.conn)
        self.repo = SignalRepository(self.conn)
        self.validator = SignalValidator(ValidationConfig(
            min_confidence=0.05, min_strength=0.01, max_prediction_age_days=3650))
        self.engine = SignalEngine(self.repo, self.validator)

    def tearDown(self):
        self.conn.close()
        os.remove(self.db_path)


class TestAccounting(EngineTestCase):
    def test_every_candidate_is_accounted_for(self):
        report = self.engine.run([make_strategy()], [make_context()], now=NOW)
        self.assertTrue(report.is_balanced)
        self.assertEqual(report.candidates_generated, report.accounted_for)

    def test_suppressed_candidates_are_counted_not_lost(self):
        strict = SignalEngine(self.repo, SignalValidator(
            ValidationConfig(min_confidence=0.99, max_prediction_age_days=3650)))
        report = strict.run([make_strategy()], [make_context()], now=NOW)
        self.assertEqual(report.signals_created, 0)
        self.assertEqual(report.signals_suppressed, 1)
        self.assertTrue(report.is_balanced)

    def test_suppression_reasons_are_tallied(self):
        strict = SignalEngine(self.repo, SignalValidator(
            ValidationConfig(min_confidence=0.99, max_prediction_age_days=3650)))
        report = strict.run([make_strategy()], [make_context()], now=NOW)
        self.assertIn("low_confidence", report.suppression_reasons)


class TestDeduplication(EngineTestCase):
    def test_same_information_state_produces_one_signal(self):
        context = make_context()
        self.engine.run([make_strategy()], [context], now=NOW)
        second = self.engine.run([make_strategy()], [context], now=NOW)
        self.assertEqual(second.duplicates_skipped, 1)
        self.assertEqual(second.signals_created, 0)

    def test_repeated_runs_do_not_grow_the_table(self):
        context = make_context()
        for _ in range(5):
            self.engine.run([make_strategy()], [context], now=NOW)
        count = self.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        self.assertEqual(count, 1)

    def test_new_information_state_produces_a_new_signal(self):
        self.engine.run([make_strategy()], [make_context()], now=NOW)
        later = make_context(cutoff=NOW + timedelta(days=1))
        report = self.engine.run([make_strategy()], [later], now=NOW + timedelta(days=1))
        self.assertEqual(report.signals_created, 1)
        self.assertEqual(report.duplicates_skipped, 0)


class TestSuperseding(EngineTestCase):
    def test_newer_signal_supersedes_the_older_one(self):
        self.engine.run([make_strategy()], [make_context()], now=NOW)
        later = make_context(cutoff=NOW + timedelta(days=1))
        report = self.engine.run([make_strategy()], [later], now=NOW + timedelta(days=1))
        self.assertEqual(report.signals_superseded, 1)
        self.assertEqual(len(self.repo.active_signals("inst-nvda")), 1)

    def test_superseded_signal_keeps_its_original_claim(self):
        self.engine.run([make_strategy()], [make_context(value=0.03)], now=NOW)
        original = self.repo.active_signals("inst-nvda")[0]
        original_strength = original.strength

        later = make_context(cutoff=NOW + timedelta(days=1), value=-0.04)
        self.engine.run([make_strategy()], [later], now=NOW + timedelta(days=1))

        back = self.repo.get(original.signal_id)
        self.assertEqual(back.status, SignalStatus.SUPERSEDED)
        self.assertEqual(back.strength, original_strength)  # claim untouched
        self.assertIsNotNone(back.superseded_by)

    def test_a_suppressed_signal_does_not_supersede_a_valid_one(self):
        # The system declining to speak must not silence an earlier view.
        self.engine.run([make_strategy()], [make_context()], now=NOW)
        active_before = len(self.repo.active_signals("inst-nvda"))

        strict = SignalEngine(self.repo, SignalValidator(
            ValidationConfig(min_confidence=0.99, max_prediction_age_days=3650)))
        strict.run([make_strategy()],
                   [make_context(cutoff=NOW + timedelta(days=1))],
                   now=NOW + timedelta(days=1))

        self.assertEqual(len(self.repo.active_signals("inst-nvda")), active_before)

    def test_older_information_does_not_supersede_newer(self):
        # Backfilling out of order must not rewrite the present.
        recent = make_context(cutoff=NOW + timedelta(days=5))
        self.engine.run([make_strategy()], [recent], now=NOW + timedelta(days=5))
        report = self.engine.run([make_strategy()], [make_context(cutoff=NOW)], now=NOW)
        self.assertEqual(report.signals_superseded, 0)


class TestIsolation(EngineTestCase):
    def test_a_broken_strategy_does_not_abort_the_batch(self):
        broken = BrokenStrategy(SignalStrategyDefinition(
            strategy_id="broken", name="Broken", version="v1",
            signal_type=SignalType.DIRECTIONAL, created_at=NOW))
        report = self.engine.run([broken, make_strategy()], [make_context()], now=NOW)
        self.assertEqual(report.signals_created, 1)
        self.assertTrue(any("failed" in note for note in report.notes))

    def test_inactive_strategy_is_skipped_with_a_note(self):
        definition = SignalStrategyDefinition(
            strategy_id="ml_dir", name="ML", version="v1", is_active=False,
            signal_type=SignalType.DIRECTIONAL,
            parameters={"strength_scale": 0.05}, created_at=NOW)
        report = self.engine.run([MLDirectionalStrategy(definition)],
                                 [make_context()], now=NOW)
        self.assertEqual(report.candidates_generated, 0)
        self.assertTrue(any("inactive" in note for note in report.notes))


class TestDryRun(EngineTestCase):
    def test_apply_false_writes_nothing(self):
        report = self.engine.run([make_strategy()], [make_context()], now=NOW, apply=False)
        self.assertEqual(report.signals_created, 1)
        count = self.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        self.assertEqual(count, 0)


class TestSuppressedArePersisted(EngineTestCase):
    def test_suppressed_signals_are_stored_by_default(self):
        strict = SignalEngine(self.repo, SignalValidator(
            ValidationConfig(min_confidence=0.99, max_prediction_age_days=3650)))
        strict.run([make_strategy()], [make_context()], now=NOW)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM signals WHERE status = ?",
            (SignalStatus.SUPPRESSED.value,)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_suppression_reasons_are_queryable_in_sql(self):
        strict = SignalEngine(self.repo, SignalValidator(
            ValidationConfig(min_confidence=0.99, max_prediction_age_days=3650)))
        strict.run([make_strategy()], [make_context()], now=NOW)
        rows = self.conn.execute(
            "SELECT reason, COUNT(*) FROM signal_suppressions GROUP BY reason").fetchall()
        self.assertTrue(rows)


class TestExpiry(EngineTestCase):
    def test_expire_stale_moves_past_validity_to_expired(self):
        self.engine.run([make_strategy()], [make_context()], now=NOW)
        changed = self.engine.expire_stale(NOW + timedelta(days=30))
        self.assertEqual(changed, 1)
        self.assertEqual(len(self.repo.active_signals()), 0)


if __name__ == "__main__":
    unittest.main()
