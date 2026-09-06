"""
tests/memory/test_experience.py
---------------------------------------
Experience creation, provenance, context, classification, eligibility.

Covers §58 items 1-6, 21-23.

THE ONE THAT MATTERS MOST
-----------------------------
`available_at` — when an experience became KNOWABLE, which is when its
outcome window closed. If it were dated by `created_at`, every
experience would appear at the same instant and `memory_as_of` would
silently return the future for every historical query.

Several tests below exist only to pin that distinction.
"""

import json
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.memory_schema import initialize_memory_schema
from src.data_access.outcome_schema import initialize_outcome_schema
from src.domain.memory_models import (
    CONTEXT_SCHEMA_VERSION, MEMORY_METHOD_VERSION, ExperienceClass,
    ExperienceContext, ExperienceKind, ExperienceQuality,
    classify_experience, experience_id_for,
)
from src.memory.experience import build_all, build_experience, save

CUTOFF = datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc)
CLOSE = CUTOFF + timedelta(days=5)


def outcome(**overrides):
    base = {
        "subject_kind": "signal", "subject_id": "sig-1", "horizon": "5d",
        "method_version": "v1", "status": "available",
        "information_cutoff": CUTOFF.isoformat(),
        "window_end": CLOSE.isoformat(),
        "simple_return": -0.03, "expected_return": 0.02,
        "expected_direction": "long", "realized_direction": "short",
        "direction_result": "miss", "mfe": 0.01, "mae": -0.05,
        "time_to_mfe_seconds": 86400.0, "instrument_id": "us_and_intl-aapl",
        "trained_model_id": "tm-1", "model_status": "evaluated",
        "strategy_id": "ml_directional", "market_regime": None,
        "event_type": "earnings", "confidence": 0.3, "strength": 0.5,
        "signal_status": "active",
    }
    base.update(overrides)
    return base


class TestExperienceCreation(unittest.TestCase):
    """§58.1, §58.2 — creation and provenance."""

    def test_an_experience_carries_every_provenance_reference(self):
        experience = build_experience(outcome())
        self.assertEqual(experience.subject_kind, "signal")
        self.assertEqual(experience.subject_id, "sig-1")
        self.assertEqual(experience.horizon, "5d")
        self.assertEqual(experience.outcome_method_version, "v1")
        self.assertEqual(experience.trained_model_id, "tm-1")
        self.assertTrue(experience.attribution_method_version)

    def test_the_id_is_deterministic_from_the_natural_key(self):
        """A rebuild must reproduce the same id, or memory duplicates."""
        first = experience_id_for("signal", "sig-1", "5d", "v1")
        second = experience_id_for("signal", "sig-1", "5d", "v1")
        self.assertEqual(first, second)
        self.assertNotEqual(first, experience_id_for("signal", "sig-1", "5d", "v2"))

    def test_a_prediction_and_a_signal_are_different_kinds(self):
        self.assertEqual(build_experience(outcome()).kind, ExperienceKind.SIGNAL)
        self.assertEqual(
            build_experience(outcome(subject_kind="prediction")).kind,
            ExperienceKind.PREDICTION)


class TestAvailableAt(unittest.TestCase):
    """
    §58.14's foundation. Every point-in-time guarantee rests here.
    """

    def test_it_is_the_window_close_not_the_information_cutoff(self):
        experience = build_experience(outcome())
        self.assertEqual(experience.available_at, CLOSE)
        self.assertEqual(experience.information_cutoff, CUTOFF)
        self.assertGreater(experience.available_at, experience.information_cutoff)

    def test_it_is_never_the_creation_time(self):
        """
        The failure this prevents: dating memory by when it was
        processed puts the whole record at one instant and makes
        historical retrieval return the future.
        """
        experience = build_experience(outcome())
        self.assertNotEqual(experience.available_at.date(),
                            experience.created_at.date())

    def test_an_outcome_with_no_window_close_has_no_availability(self):
        experience = build_experience(outcome(window_end=None,
                                              status="pending"))
        self.assertIsNone(experience.available_at)
        self.assertEqual(experience.quality, ExperienceQuality.INCOMPLETE)

    def test_age_is_measured_from_availability(self):
        experience = build_experience(outcome())
        age = experience.age_days(now=CLOSE + timedelta(days=10))
        self.assertAlmostEqual(age, 10.0, places=6)


class TestContext(unittest.TestCase):
    """§58.3 — decision-time context only."""

    def test_context_captures_decision_time_dimensions(self):
        experience = build_experience(
            outcome(),
            signal_context={"signal_type": "directional",
                            "volatility_percentile": 0.8,
                            "data_quality_level": "high",
                            "strategy_version": "v2"},
            instrument_context={"asset_class": "stock", "sector_id": "tech"})
        context = experience.context
        self.assertEqual(context.asset_class, "stock")
        self.assertEqual(context.sector_id, "tech")
        self.assertEqual(context.volatility_percentile, 0.8)
        self.assertEqual(context.data_quality, "high")

    def test_the_context_carries_its_own_schema_version(self):
        """§9: context definitions change for different reasons than
        aggregation rules, so they version separately."""
        self.assertEqual(build_experience(outcome()).context.schema_version,
                         CONTEXT_SCHEMA_VERSION)

    def test_no_outcome_field_appears_in_the_context(self):
        """
        §8: use only information available at decision time. A realised
        return wearing a context label would be leakage that every
        later phase inherits.
        """
        fields = set(ExperienceContext().as_dict())
        for forbidden in ("actual_return", "simple_return", "mfe", "mae",
                          "direction_result", "realized_direction",
                          "primary_error"):
            self.assertNotIn(forbidden, fields)


class TestClassification(unittest.TestCase):
    """§58.4, §13 — driven by attribution, never by profit."""

    def test_no_error_and_a_gain_is_an_expected_win(self):
        self.assertEqual(
            classify_experience(direction_result="hit", primary_error="no_error",
                                actual_return=0.03, unexpected=False),
            ExperienceClass.EXPECTED_WIN)

    def test_no_error_and_an_unusual_gain_is_an_unexpected_win(self):
        """§27's concern: a surprising win does not validate the model."""
        self.assertEqual(
            classify_experience(direction_result="hit", primary_error="no_error",
                                actual_return=0.30, unexpected=True),
            ExperienceClass.UNEXPECTED_WIN)

    def test_an_attributed_expected_loss_stays_an_expected_loss(self):
        self.assertEqual(
            classify_experience(direction_result="neutral",
                                primary_error="expected_loss",
                                actual_return=-0.004, unexpected=False),
            ExperienceClass.EXPECTED_LOSS)

    def test_a_profitable_result_with_a_faulty_reason_is_mixed(self):
        """
        §13 exactly: a profitable result can still be a weak decision.
        Calling this successful would teach the system that being right
        by accident is being right.
        """
        self.assertEqual(
            classify_experience(direction_result="hit",
                                primary_error="magnitude_error",
                                actual_return=0.05, unexpected=False),
            ExperienceClass.MIXED)

    def test_an_unknown_attribution_yields_no_clear_result(self):
        self.assertEqual(
            classify_experience(direction_result="miss", primary_error="unknown",
                                actual_return=-0.02, unexpected=False),
            ExperienceClass.NO_CLEAR_RESULT)

    def test_a_missing_attribution_yields_no_clear_result(self):
        self.assertEqual(
            classify_experience(direction_result="miss", primary_error=None,
                                actual_return=-0.02, unexpected=None),
            ExperienceClass.NO_CLEAR_RESULT)

    def test_an_unusual_loss_with_a_cause_is_an_unexpected_loss(self):
        self.assertEqual(
            classify_experience(direction_result="miss",
                                primary_error="prediction_error",
                                actual_return=-0.30, unexpected=True),
            ExperienceClass.UNEXPECTED_LOSS)


class TestEligibilityAndQuality(unittest.TestCase):
    """§58.22, §6 — experimental stays distinguishable, nothing is discarded."""

    def test_a_promoted_model_produces_validated_experience(self):
        experience = build_experience(
            outcome(model_status="active"),
            attribution={"primary": "no_error", "contributing": []})
        self.assertEqual(experience.quality, ExperienceQuality.VALIDATED)

    def test_an_unpromoted_model_produces_experimental_experience(self):
        experience = build_experience(
            outcome(model_status="evaluated"),
            attribution={"primary": "prediction_error", "contributing": []})
        self.assertEqual(experience.quality, ExperienceQuality.EXPERIMENTAL)

    def test_experimental_experience_is_kept_not_discarded(self):
        """§6: do not discard useful experimental data."""
        experience = build_experience(
            outcome(model_status="evaluated"),
            attribution={"primary": "prediction_error", "contributing": []})
        self.assertTrue(experience.is_usable)

    def test_an_unmeasured_outcome_is_incomplete(self):
        experience = build_experience(outcome(status="pending", window_end=None))
        self.assertEqual(experience.quality, ExperienceQuality.INCOMPLETE)
        self.assertFalse(experience.is_usable)

    def test_an_experience_with_no_attribution_is_incomplete(self):
        experience = build_experience(outcome(), attribution=None)
        self.assertEqual(experience.quality, ExperienceQuality.INCOMPLETE)

    def test_incomplete_experience_records_why_and_is_still_kept(self):
        experience = build_experience(outcome(status="pending", window_end=None))
        self.assertTrue(experience.notes)
        self.assertTrue(any("worth remembering" in note
                            for note in experience.notes))


class TestAttributionLinkage(unittest.TestCase):
    """§58.5, §12 — links, never duplicates the logic."""

    def test_primary_and_contributing_stay_distinct(self):
        experience = build_experience(
            outcome(),
            attribution={"primary": "prediction_error",
                         "contributing": ["timing_error", "horizon_mismatch"],
                         "confidence": "high", "severity": "medium"})
        self.assertEqual(experience.primary_error, "prediction_error")
        self.assertEqual(experience.contributing_errors,
                         ["timing_error", "horizon_mismatch"])

    def test_the_evidence_count_travels_but_not_the_evidence_text(self):
        """
        §52: nothing Phase 20 already stores is stored again. Only the
        count comes across, so a consumer knows how much backs it.
        """
        experience = build_experience(
            outcome(), attribution={"primary": "no_error", "contributing": []},
            evidence_count=5)
        self.assertEqual(experience.evidence_count, 5)
        self.assertFalse(hasattr(experience, "evidence_text"))


class PersistenceCase(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        initialize_outcome_schema(self.conn)
        initialize_memory_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def add_outcome(self, subject_id="sig-1", horizon="5d", status="available",
                    window_end=None, direction_result="miss",
                    simple_return=-0.03, model_status="evaluated"):
        self.conn.execute("""
            INSERT OR REPLACE INTO outcome_measurements (
                subject_kind, subject_id, horizon, method_version,
                horizon_value, horizon_unit, status, reference_rule,
                information_cutoff, window_end, direction_result,
                expected_direction, expected_return, simple_return, mfe, mae,
                instrument_id, trained_model_id, model_status, event_type,
                confidence, strength, signal_status, computed_at
            ) VALUES ('signal',?,?,'v1',5.0,'d',?,'first_close_at_or_after_cutoff',
                      ?,?,?,'long',0.02,?,0.01,-0.05,'i-1','tm-1',?,'earnings',
                      0.3,0.5,'active','2026-09-05T00:00:00+00:00')
        """, (subject_id, horizon, status, CUTOFF.isoformat(),
              window_end if window_end is not None else CLOSE.isoformat(),
              direction_result, simple_return, model_status))
        self.conn.execute("""
            INSERT OR REPLACE INTO error_attributions (
                subject_kind, subject_id, horizon, method_version, error_type,
                role, confidence, severity, status, observability,
                attributed_at
            ) VALUES ('signal',?,?,'v1','prediction_error','primary','high',
                      'medium','attributed','observed','2026-09-05T00:00:00+00:00')
        """, (subject_id, horizon))
        self.conn.commit()

    def count(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM trading_experiences").fetchone()[0]


class TestPersistenceAndIdempotency(PersistenceCase):
    """§58.18, §54."""

    def setUp(self):
        super().setUp()
        from src.data_access.attribution_schema import initialize_attribution_schema
        initialize_attribution_schema(self.conn)

    def test_building_twice_does_not_duplicate(self):
        self.add_outcome()
        save(self.conn, build_all(self.conn))
        first = self.count()
        save(self.conn, build_all(self.conn))
        self.assertEqual(self.count(), first)

    def test_a_new_memory_version_adds_rows_beside_the_old(self):
        self.add_outcome()
        save(self.conn, build_all(self.conn))
        first = self.count()
        save(self.conn, build_all(self.conn, memory_version="v2"))
        self.assertEqual(self.count(), first * 2)

    def test_the_old_version_is_untouched(self):
        """§35: do not silently change memory semantics."""
        self.add_outcome()
        save(self.conn, build_all(self.conn))
        before = self.conn.execute("""
            SELECT experience_id, experience_class, quality
            FROM trading_experiences WHERE memory_version='v1'
        """).fetchall()
        save(self.conn, build_all(self.conn, memory_version="v2"))
        after = self.conn.execute("""
            SELECT experience_id, experience_class, quality
            FROM trading_experiences WHERE memory_version='v1'
        """).fetchall()
        self.assertEqual(before, after)

    def test_an_incremental_build_only_sees_recent_outcomes(self):
        """§55: do not rebuild the whole history every time."""
        self.add_outcome("sig-old", window_end="2026-08-01T00:00:00+00:00")
        self.add_outcome("sig-new", window_end="2026-09-01T00:00:00+00:00")
        recent = build_all(self.conn, since="2026-08-15T00:00:00+00:00")
        self.assertEqual({e.subject_id for e in recent}, {"sig-new"})

    def test_the_context_is_stored_as_json_with_its_version(self):
        self.add_outcome()
        save(self.conn, build_all(self.conn))
        raw, version = self.conn.execute(
            "SELECT context_json, context_schema_version FROM trading_experiences"
        ).fetchone()
        self.assertEqual(version, CONTEXT_SCHEMA_VERSION)
        self.assertIn("schema_version", json.loads(raw))


if __name__ == "__main__":
    unittest.main()
