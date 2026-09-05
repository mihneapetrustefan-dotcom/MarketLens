"""
tests/outcomes/test_pipeline.py
---------------------------------------
Batch measurement, aggregation, and the properties that make the
record trustworthy over time.

Covers §53 items 15-17 and 20-24: idempotency, reprocessing,
versioning, outcome lineage, model separation, confidence semantics,
strength semantics.

THE THREE PROPERTIES THAT MATTER MOST
-----------------------------------------
1. IDEMPOTENT — running twice cannot change the row count. Anything
   else and every aggregate silently double-counts whatever ran twice.
2. VERSIONED — a methodology change writes NEW rows. Anything else and
   a number somebody already read changes underneath them.
3. LAYERED — prediction outcomes and signal outcomes never pool. They
   are different claims, and averaging them answers no question.
"""

import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.outcome_schema import initialize_outcome_schema
from src.domain.outcome_models import (
    OUTCOME_METHOD_VERSION, DirectionResult, OutcomeStatus, SubjectKind,
)
from src.outcomes.analytics import (
    CONFIDENCE_EDGES, MIN_SAMPLE, STRENGTH_EDGES, bootstrap_mean_interval,
    bucket, build_cohorts, cohort_warning, decay_curve, load_measurements,
    percentile, save_aggregates,
)
from src.outcomes.pipeline import data_as_of, load_signal_subjects, run, save

CUTOFF = datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc)
LATER = CUTOFF + timedelta(days=400)


class PipelineCase(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript("""
            CREATE TABLE signals (signal_id TEXT PRIMARY KEY, instrument_id TEXT,
                direction TEXT, status TEXT, strength REAL, confidence REAL,
                expected_return REAL, source_information_cutoff TEXT,
                strategy_id TEXT, market_regime TEXT, event_type TEXT,
                observation_id TEXT);
            CREATE TABLE signal_contributions (signal_id TEXT, prediction_id TEXT,
                trained_model_id TEXT, model_qualified_id TEXT, predicted_value REAL,
                probability_up REAL, confidence REAL, weight REAL, reliability REAL,
                is_abstention INTEGER, note TEXT);
            CREATE TABLE trained_models (trained_model_id TEXT PRIMARY KEY,
                status TEXT, label_name TEXT);
            CREATE TABLE predictions (prediction_id TEXT PRIMARY KEY,
                observation_id TEXT, predicted_value REAL, confidence REAL,
                information_cutoff TEXT, trained_model_id TEXT,
                is_abstention INTEGER DEFAULT 0);
            CREATE TABLE research_observations (observation_id TEXT PRIMARY KEY,
                instrument_id TEXT, market_regime TEXT, event_type TEXT);
            CREATE TABLE price_candle_cache (instrument_id TEXT, interval TEXT,
                timestamp TEXT, open REAL, high REAL, low REAL, close REAL,
                adjusted_close REAL, volume REAL, source TEXT, fetched_at TEXT);
        """)
        initialize_outcome_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    # ---------------- fixtures ----------------

    def add_candles(self, instrument_id="i-1", days=30, start_price=100.0,
                    step=1.0, interval="1d"):
        for day in range(days):
            price = start_price + day * step
            self.conn.execute(
                "INSERT INTO price_candle_cache (instrument_id, interval, "
                "timestamp, open, high, low, close, adjusted_close, volume) "
                "VALUES (?,?,?,?,?,?,?,NULL,1000)",
                (instrument_id, interval,
                 (CUTOFF + timedelta(days=day)).isoformat(),
                 price, price + 1, price - 1, price))
        self.conn.commit()

    def add_signal(self, signal_id="sig-1", instrument_id="i-1",
                   direction="long", status="active", strength=0.5,
                   confidence=0.3, expected_return=0.01, offset_days=0,
                   trained_model_id="tm-1", model_status="evaluated"):
        self.conn.execute(
            "INSERT INTO signals VALUES (?,?,?,?,?,?,?,?,'ml_directional',"
            "'bull','earnings','obs-1')",
            (signal_id, instrument_id, direction, status, strength, confidence,
             expected_return, (CUTOFF + timedelta(days=offset_days)).isoformat()))
        if trained_model_id:
            self.conn.execute(
                "INSERT OR IGNORE INTO trained_models VALUES (?,?,'d5.abnormal_return')",
                (trained_model_id, model_status))
            self.conn.execute(
                "INSERT INTO signal_contributions VALUES (?,'p-1',?,'q',0.01,"
                "NULL,NULL,1.0,NULL,0,'')", (signal_id, trained_model_id))
        self.conn.commit()

    def add_prediction(self, prediction_id="p-1", instrument_id="i-1",
                       predicted_value=0.02, abstention=0):
        self.conn.execute(
            "INSERT OR IGNORE INTO research_observations VALUES "
            "('obs-1',?, 'bull','earnings')", (instrument_id,))
        self.conn.execute(
            "INSERT INTO predictions VALUES (?,'obs-1',?,0.3,?,'tm-1',?)",
            (prediction_id, predicted_value, CUTOFF.isoformat(), abstention))
        self.conn.commit()

    def rows(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM outcome_measurements").fetchone()[0]

    def measure_all(self, **kwargs):
        options = dict(horizons=("1d", "5d"), subject_kinds=(SubjectKind.SIGNAL,))
        options.update(kwargs)
        outcomes, report = run(self.conn, **options)
        save(self.conn, outcomes)
        return report


# ======================================================================
# §53.15 — idempotency
# ======================================================================

class TestIdempotency(PipelineCase):

    def test_running_twice_does_not_change_the_row_count(self):
        self.add_candles()
        self.add_signal()
        self.measure_all()
        first = self.rows()
        self.measure_all()
        self.assertEqual(self.rows(), first)

    def test_running_five_times_does_not_change_the_row_count(self):
        self.add_candles()
        self.add_signal()
        self.measure_all()
        first = self.rows()
        for _ in range(4):
            self.measure_all()
        self.assertEqual(self.rows(), first)

    def test_a_settled_measurement_is_skipped_rather_than_recomputed(self):
        self.add_candles()
        self.add_signal()
        self.measure_all()
        report = self.measure_all()
        self.assertGreater(report.skipped_existing, 0)

    def test_a_pending_measurement_is_always_revisited(self):
        """
        PENDING exists precisely to be looked at again once the data
        catches up. Skipping it would freeze it forever.
        """
        self.add_candles(days=2)
        self.add_signal()
        self.measure_all(horizons=("10d",))
        pending = self.conn.execute(
            "SELECT COUNT(*) FROM outcome_measurements WHERE status='pending'"
        ).fetchone()[0]
        self.assertEqual(pending, 1)
        report = self.measure_all(horizons=("10d",))
        self.assertEqual(report.skipped_existing, 0)

    def test_a_pending_row_becomes_available_when_the_data_arrives(self):
        self.add_candles(days=2)
        self.add_signal()
        self.measure_all(horizons=("5d",))
        self.assertEqual(self.conn.execute(
            "SELECT status FROM outcome_measurements").fetchone()[0], "pending")
        self.add_candles(days=30)
        self.measure_all(horizons=("5d",))
        self.assertEqual(self.conn.execute(
            "SELECT status FROM outcome_measurements").fetchone()[0], "available")
        self.assertEqual(self.rows(), 1, "the correction added a row")


# ======================================================================
# §53.16, §53.17 — reprocessing and versioning
# ======================================================================

class TestVersioning(PipelineCase):

    def test_a_new_method_version_adds_rows_beside_the_old_ones(self):
        self.add_candles()
        self.add_signal()
        self.measure_all()
        before = self.rows()
        self.measure_all(method_version="v2")
        self.assertEqual(self.rows(), before * 2)

    def test_the_old_version_is_left_completely_untouched(self):
        """
        §31: do not overwrite historical methodology. A number somebody
        has already read must not change under them.
        """
        self.add_candles()
        self.add_signal()
        self.measure_all()
        before = self.conn.execute(
            "SELECT subject_id, horizon, simple_return FROM outcome_measurements "
            "WHERE method_version = ? ORDER BY horizon", (OUTCOME_METHOD_VERSION,)
        ).fetchall()
        self.measure_all(method_version="v2")
        after = self.conn.execute(
            "SELECT subject_id, horizon, simple_return FROM outcome_measurements "
            "WHERE method_version = ? ORDER BY horizon", (OUTCOME_METHOD_VERSION,)
        ).fetchall()
        self.assertEqual(before, after)

    def test_the_method_version_is_part_of_the_primary_key(self):
        columns = self.conn.execute(
            "SELECT name FROM pragma_table_info('outcome_measurements') WHERE pk > 0"
        ).fetchall()
        self.assertIn(("method_version",), columns)

    def test_rescore_replaces_rather_than_appends(self):
        self.add_candles()
        self.add_signal()
        self.measure_all()
        before = self.rows()
        self.measure_all(rescore=True)
        self.assertEqual(self.rows(), before)

    def test_every_row_records_the_version_that_produced_it(self):
        self.add_candles()
        self.add_signal()
        self.measure_all()
        versions = {row[0] for row in self.conn.execute(
            "SELECT DISTINCT method_version FROM outcome_measurements")}
        self.assertEqual(versions, {OUTCOME_METHOD_VERSION})


# ======================================================================
# §32, §33 — status
# ======================================================================

class TestOutcomeStatus(PipelineCase):

    def test_an_open_window_is_pending(self):
        self.add_candles(days=3)
        self.add_signal()
        self.measure_all(horizons=("10d",))
        self.assertEqual(self.conn.execute(
            "SELECT status FROM outcome_measurements").fetchone()[0], "pending")

    def test_an_instrument_with_no_prices_is_insufficient_data(self):
        self.add_signal(instrument_id="i-missing")
        self.measure_all()
        statuses = {row[0] for row in self.conn.execute(
            "SELECT status FROM outcome_measurements")}
        self.assertEqual(statuses, {"insufficient_data"})

    def test_insufficient_data_never_carries_a_return(self):
        """The default that would poison every aggregate downstream."""
        self.add_signal(instrument_id="i-missing")
        self.measure_all()
        for (value,) in self.conn.execute(
                "SELECT simple_return FROM outcome_measurements "
                "WHERE status='insufficient_data'"):
            self.assertIsNone(value)

    def test_insufficient_data_never_counts_as_a_miss(self):
        self.add_signal(instrument_id="i-missing")
        self.measure_all()
        for (result,) in self.conn.execute(
                "SELECT direction_result FROM outcome_measurements"):
            self.assertEqual(result, DirectionResult.INSUFFICIENT_DATA.value)

    def test_an_abstaining_signal_is_never_scored(self):
        self.add_candles()
        self.add_signal(direction="no_signal")
        report = self.measure_all()
        self.assertEqual(report.skipped_no_direction, 1)
        self.assertEqual(self.rows(), 0)

    def test_a_suppressed_signal_is_still_measured(self):
        """
        §38: a suppressed signal still made a claim, and measuring it is
        the only way to learn whether the suppression rule is any good.
        It is an outcome, never a trade — no order existed.
        """
        self.add_candles()
        self.add_signal(status="suppressed")
        self.measure_all()
        self.assertGreater(self.rows(), 0)
        self.assertEqual(self.conn.execute(
            "SELECT DISTINCT signal_status FROM outcome_measurements"
        ).fetchone()[0], "suppressed")


# ======================================================================
# §53.21, §53.22, §37, §49 — lineage and layer separation
# ======================================================================

class TestLineageAndLayers(PipelineCase):

    def test_a_signal_outcome_carries_the_model_that_produced_it(self):
        self.add_candles()
        self.add_signal()
        self.measure_all()
        self.assertEqual(self.conn.execute(
            "SELECT DISTINCT trained_model_id FROM outcome_measurements"
        ).fetchone()[0], "tm-1")

    def test_the_model_status_travels_with_the_outcome(self):
        """
        Phase 18's experimental/validated distinction must survive into
        the outcome record, or an analysis would pool research output
        with production output (§38, §59).
        """
        self.add_candles()
        self.add_signal(model_status="evaluated")
        self.measure_all()
        self.assertEqual(self.conn.execute(
            "SELECT DISTINCT model_status FROM outcome_measurements"
        ).fetchone()[0], "evaluated")

    def test_predictions_and_signals_are_stored_as_different_subjects(self):
        self.add_candles()
        self.add_signal()
        self.add_prediction()
        self.measure_all(subject_kinds=(SubjectKind.SIGNAL, SubjectKind.PREDICTION))
        kinds = {row[0] for row in self.conn.execute(
            "SELECT DISTINCT subject_kind FROM outcome_measurements")}
        self.assertEqual(kinds, {"signal", "prediction"})

    def test_a_prediction_and_a_signal_never_share_a_row(self):
        """
        §2: they are different claims. The primary key includes
        `subject_kind`, so one cannot overwrite the other even when the
        ids collide.
        """
        self.add_candles()
        self.add_signal(signal_id="x-1")
        self.add_prediction(prediction_id="x-1")
        self.measure_all(subject_kinds=(SubjectKind.SIGNAL, SubjectKind.PREDICTION))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM outcome_measurements WHERE subject_id='x-1' "
            "AND horizon='1d'").fetchone()[0], 2)

    def test_an_abstaining_prediction_is_not_measured(self):
        self.add_candles()
        self.add_prediction(abstention=1)
        self.measure_all(subject_kinds=(SubjectKind.PREDICTION,))
        self.assertEqual(self.rows(), 0)

    def test_signal_outcomes_do_not_require_an_order_to_exist(self):
        """
        §37: a signal may have an outcome even when no order existed.
        There are no order tables in this fixture at all, and the
        measurement is unaffected.
        """
        self.add_candles()
        self.add_signal()
        self.measure_all()
        self.assertGreater(self.rows(), 0)

    def test_subjects_load_without_an_instrument_registry(self):
        """
        The Phase 17.5 lesson: an optional table joined into the main
        query becomes a hard dependency, and a missing one silently
        returns nothing at all.
        """
        self.add_candles()
        self.add_signal()
        self.conn.execute("DROP TABLE signal_contributions")
        self.conn.commit()
        subjects = load_signal_subjects(self.conn)
        self.assertEqual(len(subjects), 1)
        self.assertIsNone(subjects[0]["trained_model_id"])


# ======================================================================
# §15, §23, §40, §41 — aggregation
# ======================================================================

class TestAggregation(PipelineCase):

    def populate(self, count=40):
        self.add_candles(days=40)
        for index in range(count):
            self.add_signal(signal_id=f"sig-{index}",
                            direction="long" if index % 2 == 0 else "short",
                            confidence=0.3, strength=index / count)
        self.measure_all(horizons=("1d", "5d"))
        rows = load_measurements(self.conn)
        cohorts = build_cohorts(rows)
        save_aggregates(self.conn, cohorts)
        return cohorts

    def test_every_aggregate_exposes_its_sample_size(self):
        self.populate()
        for (size,) in self.conn.execute(
                "SELECT sample_size FROM outcome_aggregates"):
            self.assertIsNotNone(size)

    def test_a_small_cohort_is_flagged_small(self):
        self.populate(count=5)
        flags = {row[0] for row in self.conn.execute(
            "SELECT small_sample FROM outcome_aggregates")}
        self.assertEqual(flags, {1})

    def test_the_small_sample_threshold_is_the_evaluators_not_a_new_one(self):
        from src.domain.model_models import ModelEvaluation
        self.assertEqual(MIN_SAMPLE, ModelEvaluation.MIN_EFFECTIVE_SAMPLE)

    def test_distributions_are_stored_not_just_a_win_rate(self):
        """
        §15. A win rate is compatible with a strategy that makes a penny
        51 times and loses a pound once.
        """
        self.populate()
        row = self.conn.execute("""
            SELECT mean_return, median_return, stdev_return, min_return,
                   max_return, p10_return, p25_return, p75_return, p90_return
            FROM outcome_aggregates WHERE cohort_kind='overall' AND horizon='1d'
        """).fetchone()
        self.assertTrue(all(value is not None for value in row))

    def test_neutrals_are_excluded_from_accuracy_and_reported_separately(self):
        """
        A market that did not move is not evidence for or against a
        directional claim. Counting it as a loss would make hit rate a
        measure of volatility.
        """
        cohorts = self.populate()
        overall = [c for c in cohorts
                   if c.cohort_kind == "overall" and c.horizon == "1d"][0]
        self.assertEqual(overall.decided, overall.hits + overall.misses)
        if overall.decided:
            self.assertAlmostEqual(overall.directional_accuracy,
                                   overall.hits / overall.decided, places=9)

    def test_an_empty_cohort_reports_none_rather_than_zero(self):
        cohorts = self.populate(count=1)
        for cohort in cohorts:
            if not cohort.returns:
                self.assertIsNone(cohort.directional_accuracy
                                  if cohort.decided == 0 else 0.0)

    def test_a_confidence_interval_is_absent_below_the_threshold(self):
        """
        §40: NULL means "not calculated", never "zero width". An
        interval on eleven observations is an invitation to read eleven
        observations as evidence.
        """
        low, high, method = bootstrap_mean_interval([0.01] * (MIN_SAMPLE - 1))
        self.assertIsNone(low)
        self.assertIsNone(high)
        self.assertEqual(method, "")

    def test_a_confidence_interval_is_deterministic(self):
        """A research number that changes when you look at it twice is not one."""
        values = [0.01 * (index % 7) - 0.02 for index in range(MIN_SAMPLE + 20)]
        first = bootstrap_mean_interval(values, iterations=200)
        second = bootstrap_mean_interval(values, iterations=200)
        self.assertEqual(first, second)

    def test_the_interval_brackets_the_sample_mean(self):
        values = [0.01 * (index % 5) for index in range(MIN_SAMPLE + 30)]
        low, high, _ = bootstrap_mean_interval(values, iterations=400)
        mean = sum(values) / len(values)
        self.assertLessEqual(low, mean)
        self.assertGreaterEqual(high, mean)

    def test_the_ci_method_is_named_where_one_was_computed(self):
        values = [0.01 * (index % 5) for index in range(MIN_SAMPLE + 5)]
        _, _, method = bootstrap_mean_interval(values, iterations=100)
        self.assertIn("bootstrap", method)

    def test_the_multiple_testing_warning_states_the_cohort_count(self):
        """§41: the caveat must reach the same page as the number."""
        warning = cohort_warning(1778)
        self.assertIn("1778", warning)
        self.assertIn("chance", warning)
        self.assertIn("No significance test was run", warning)

    def test_every_cohort_column_survives_the_round_trip(self):
        """
        `build_cohorts` keys its slices on columns that `load_measurements`
        must actually select. Omitting one did not fail — it silently
        made every direction cohort 'unknown', which is precisely the
        quiet wrongness this phase exists to prevent. So the contract
        between the two functions is pinned here.
        """
        from src.outcomes.analytics import COHORTS
        self.add_candles(days=10)
        self.add_signal(direction="short")
        self.measure_all(horizons=("1d",))
        rows = load_measurements(self.conn)
        self.assertTrue(rows)
        for _, column in COHORTS:
            if column is None:
                continue
            self.assertIn(column, rows[0],
                          f"COHORTS slices on {column!r} but load_measurements "
                          f"does not select it — that cohort would be silently "
                          f"'unknown' for every row")

    def test_the_direction_cohort_reports_the_real_direction(self):
        self.add_candles(days=10)
        self.add_signal(signal_id="s-long", direction="long")
        self.add_signal(signal_id="s-short", direction="short")
        self.measure_all(horizons=("1d",))
        cohorts = build_cohorts(load_measurements(self.conn))
        values = {c.cohort_value for c in cohorts if c.cohort_kind == "direction"}
        self.assertEqual(values, {"long", "short"})

    def test_no_aggregate_claims_statistical_significance(self):
        self.populate()
        columns = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(outcome_aggregates)")}
        for forbidden in ("p_value", "pvalue", "significant", "significance"):
            self.assertNotIn(forbidden, columns)


class TestDistributionHelpers(unittest.TestCase):

    def test_the_percentile_interpolates_between_neighbours(self):
        self.assertAlmostEqual(percentile([0.0, 1.0], 0.5), 0.5, places=9)
        self.assertAlmostEqual(percentile([0.0, 10.0, 20.0], 0.25), 5.0, places=9)

    def test_a_single_observation_is_its_own_percentile(self):
        self.assertEqual(percentile([0.42], 0.9), 0.42)

    def test_an_empty_series_has_no_percentile(self):
        self.assertIsNone(percentile([], 0.5))


class TestConfidenceAndStrengthStaySeparate(unittest.TestCase):
    """
    §18, §19. Strength is the size of the expected move; confidence is
    how much the system trusts it. They are bucketed with different
    labels so a cohort can never silently mix them.
    """

    def test_the_two_ladders_use_different_labels(self):
        confidence_labels = {label for _, _, label in CONFIDENCE_EDGES}
        strength_labels = {label for _, _, label in STRENGTH_EDGES}
        self.assertEqual(confidence_labels & strength_labels, set())

    def test_a_missing_score_is_its_own_bucket_not_a_number(self):
        self.assertEqual(bucket(None, CONFIDENCE_EDGES), "unknown")
        self.assertEqual(bucket(None, STRENGTH_EDGES), "unknown")

    def test_the_production_confidence_value_lands_in_one_bucket(self):
        """
        Phase 18 measured 403 of 408 signals at exactly 0.30. Confidence
        analysis is therefore expected to be degenerate, and §18 says to
        report that limitation rather than manufacture diversity.
        """
        self.assertEqual(bucket(0.30, CONFIDENCE_EDGES), "low")
        self.assertEqual(bucket(0.15, CONFIDENCE_EDGES), "very_low")


class TestDecayCurve(PipelineCase):
    """§14: does predictive power fall away with time?"""

    def test_the_curve_is_ordered_by_time_not_alphabetically(self):
        """
        '10d' sorts before '5d' as text. A decay curve on string order
        is wrong in a way that looks entirely plausible on a chart.
        """
        self.add_candles(days=40)
        for index in range(5):
            self.add_signal(signal_id=f"sig-{index}")
        self.measure_all(horizons=("1h", "1d", "5d", "10d"))
        rows = load_measurements(self.conn)
        save_aggregates(self.conn, build_cohorts(rows))
        curve = decay_curve(self.conn)
        keys = [point["horizon"] for point in curve]
        self.assertEqual(keys, [k for k in ("1h", "1d", "5d", "10d") if k in keys])

    def test_each_point_carries_its_own_sample_size(self):
        self.add_candles(days=40)
        self.add_signal()
        self.measure_all(horizons=("1d", "5d"))
        save_aggregates(self.conn, build_cohorts(load_measurements(self.conn)))
        for point in decay_curve(self.conn):
            self.assertIn("sample_size", point)
            self.assertIn("small_sample", point)


class TestDataAsOf(PipelineCase):
    """
    The clock that decides PENDING versus INSUFFICIENT_DATA is the
    DATA's clock, not the wall clock. The question is not "has enough
    time passed in the world" but "has enough time passed in the data we
    hold", and those differ by however stale the price cache is.
    """

    def test_it_reports_the_newest_bar_we_hold(self):
        self.add_candles(days=10)
        self.assertEqual(data_as_of(self.conn),
                         CUTOFF + timedelta(days=9))

    def test_an_empty_cache_reports_nothing_rather_than_now(self):
        self.assertIsNone(data_as_of(self.conn))


if __name__ == "__main__":
    unittest.main()
