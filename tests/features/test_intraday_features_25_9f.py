"""
tests/features/test_intraday_features_25_9f.py
-----------------------------------------------------------
Phase 25.9F — intraday features, labels and the research dataset.

EVERY GUARD IS TESTED BY TRYING TO BREAK IT. A test that only shows a
feature computing the right number on clean data proves nothing about
leakage: the interesting question is always what happens when the input
is a future bar, an unfinished minute, a gap, a revision, or a stale
feature state. §68 asks for exactly those, and they are the tests below
whose names begin `test_a_future`, `test_an_unfinished`, and so on.

NOTHING HERE TOUCHES D20. The intraday label family is computed in this
package and never through `src/impact/anchoring.py`; one test asserts
that by parsing the source.
"""

import os
import sqlite3
import sys
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.price_cache_schema import initialize_price_cache_schema
from src.features.intraday import (
    INTRADAY_FEATURE_VERSION, build_intraday_registry, intraday_feature_ids,
)
from src.marketdata.calendar import USEquityCalendar
from src.marketdata.intraday import (
    BarIndex, BarQuality, IntradayBar, bars_as_of, contiguous_runs,
    load_research_bars, trailing_run,
)
from src.research.intraday_context import (
    NewsRecord, cross_sectional_zscore, fit_scaler, news_features, winsorized,
)
from src.research.intraday_dataset import (
    INTRADAY_LABEL_VERSION, IntradayDatasetBuilder, LabelState, forward_return,
)

UTC = timezone.utc
#: A Tuesday, inside the regular US session (14:30 UTC = 10:30 ET).
BASE = datetime(2026, 9, 1, 14, 30, tzinfo=UTC)


def bar(instrument_id: str, start: datetime, close: float, *,
        volume: float = 1000.0, quality=(BarQuality.COMPLETE,),
        high=None, low=None) -> IntradayBar:
    return IntradayBar(
        instrument_id=instrument_id, bar_start=start, bar_end=start + timedelta(minutes=1),
        open=close, high=high if high is not None else close + 0.05,
        low=low if low is not None else close - 0.05, close=close,
        volume=volume, source="test", quality=tuple(quality))


def series(instrument_id: str, count: int, *, start: datetime = BASE,
           first: float = 100.0, step: float = 0.01,
           skip: set = frozenset()) -> list:
    """`count` consecutive minutes; anything in `skip` is simply absent."""
    out = []
    for index in range(count):
        if index in skip:
            continue
        out.append(bar(instrument_id, start + timedelta(minutes=index),
                       first + index * step))
    return out


def memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    initialize_price_cache_schema(conn)
    return conn


def store(conn: sqlite3.Connection, bars, interval: str = "1m") -> None:
    for b in bars:
        conn.execute(
            "INSERT OR REPLACE INTO price_candle_cache (instrument_id, interval, "
            "timestamp, open, high, low, close, adjusted_close, volume, source, "
            "fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (b.instrument_id, interval, b.bar_start.isoformat(), b.open, b.high,
             b.low, b.close, b.close, b.volume, "test", BASE.isoformat()))
    conn.commit()


def builder_for(conn, **kwargs) -> IntradayDatasetBuilder:
    return IntradayDatasetBuilder(conn, benchmark_id="benchmark-spy", **kwargs)


# ======================================================================
# Closed bars and point in time
# ======================================================================

class TestClosedBarSafety(unittest.TestCase):

    def test_an_unfinished_bar_is_not_visible_to_its_own_minute(self):
        """
        NEGATIVE CONTROL. The bar 14:30-14:31 must not exist for a
        decision taken at 14:30:30, which is inside it.
        """
        bars = series("x", 3)
        inside = BASE + timedelta(seconds=30)
        self.assertEqual(bars_as_of(bars, inside), [],
                         "a decision saw the minute it was standing in")

    def test_a_bar_becomes_visible_exactly_when_it_ends(self):
        bars = series("x", 3)
        self.assertEqual(len(bars_as_of(bars, BASE + timedelta(minutes=1))), 1)
        self.assertEqual(len(bars_as_of(bars, BASE + timedelta(minutes=3))), 3)

    def test_a_future_bar_is_never_visible(self):
        """NEGATIVE CONTROL: bars after the cutoff cannot reach a feature."""
        bars = series("x", 60)
        cutoff = BASE + timedelta(minutes=10)
        visible = bars_as_of(bars, cutoff)
        self.assertTrue(all(b.bar_end <= cutoff for b in visible))
        self.assertEqual(len(visible), 10)

    def test_the_timestamp_used_for_filtering_is_the_bar_end(self):
        one = bar("x", BASE, 100.0)
        self.assertEqual(one.timestamp, one.bar_end)
        self.assertNotEqual(one.timestamp, one.bar_start)


class TestContiguity(unittest.TestCase):

    def test_a_gap_splits_the_run(self):
        bars = series("x", 20, skip={10})
        runs = contiguous_runs(bars)
        self.assertEqual([len(r) for r in runs], [10, 9])

    def test_a_window_never_spans_a_gap(self):
        """
        NEGATIVE CONTROL. With a missing minute inside the lookback, a
        30-minute return must refuse rather than quietly measure 29.
        """
        bars = series("x", 40, skip={20})
        cutoff = bars[-1].bar_end
        run = trailing_run(bars, cutoff, minimum=31)
        self.assertEqual(run, [], "a window was computed across a gap")

    def test_a_stale_run_is_not_a_window(self):
        """A run that ended an hour ago does not describe this minute."""
        bars = series("x", 10)
        late = bars[-1].bar_end + timedelta(hours=1)
        self.assertEqual(trailing_run(bars, late, minimum=1), [])


class TestBarIndexAgreesWithTheScan(unittest.TestCase):
    """
    The batch path indexes; the operational path scans. If they ever
    disagreed, a research dataset and a live decision would be computed
    from different windows while both looked correct.
    """

    def test_the_index_returns_what_trailing_run_returns(self):
        bars = series("x", 60, skip={17, 40})
        index = BarIndex(bars)
        for candidate in bars:
            cutoff = candidate.bar_end
            self.assertEqual(index.trailing_run(cutoff), trailing_run(bars, cutoff),
                             cutoff.isoformat())

    def test_both_refuse_a_cutoff_with_no_bar_ending_on_it(self):
        bars = series("x", 10)
        gap_cutoff = bars[-1].bar_end + timedelta(minutes=30)
        self.assertEqual(BarIndex(bars).trailing_run(gap_cutoff), [])
        self.assertEqual(trailing_run(bars, gap_cutoff), [])

    def test_the_minimum_is_honoured_identically(self):
        bars = series("x", 20)
        index = BarIndex(bars)
        cutoff = bars[4].bar_end                       # only 5 minutes deep
        self.assertEqual(index.trailing_run(cutoff, minimum=31), [])
        self.assertEqual(trailing_run(bars, cutoff, minimum=31), [])


# ======================================================================
# Features
# ======================================================================

class FeatureCase(unittest.TestCase):

    def setUp(self):
        self.conn = memory_db()
        self.registry = build_intraday_registry()

    def tearDown(self):
        self.conn.close()

    def features_at(self, bars, cutoff, **kwargs):
        builder = builder_for(self.conn, registry=self.registry)
        builder.seed_bars("x", bars)
        builder.seed_bars("benchmark-spy", kwargs.pop("benchmark", []))
        row = builder.observation("x", cutoff, now=cutoff + timedelta(days=1),
                                  **kwargs)
        return row


class TestFeatureValues(FeatureCase):

    def test_a_one_minute_return_is_the_ratio_of_two_closes(self):
        bars = [bar("x", BASE, 100.0), bar("x", BASE + timedelta(minutes=1), 101.0)]
        row = self.features_at(bars, BASE + timedelta(minutes=2))
        self.assertAlmostEqual(row.features["market.return_1m"], 0.01)

    def test_too_little_history_is_none_not_a_shorter_window(self):
        """Missing is missing (§20): never a quietly truncated window."""
        row = self.features_at(series("x", 5), BASE + timedelta(minutes=5))
        self.assertIsNone(row.features["market.return_30m"])
        self.assertIsNone(row.features["volatility.realized_30m"])
        self.assertIsNotNone(row.features["market.return_1m"])

    def test_run_minutes_reports_the_honest_window(self):
        row = self.features_at(series("x", 7), BASE + timedelta(minutes=7))
        self.assertEqual(row.features["regime.run_minutes"], 7.0)

    def test_an_invalid_bar_is_not_computed_on(self):
        """NEGATIVE CONTROL: a bar whose high < low is corrupt, not thin."""
        broken = IntradayBar("x", BASE, BASE + timedelta(minutes=1),
                             open=100, high=90, low=110, close=100,
                             quality=(BarQuality.INVALID,))
        self.assertFalse(broken.is_usable)
        self.assertEqual(contiguous_runs([broken]), [])

    def test_session_position_features_agree_about_the_session(self):
        row = self.features_at(series("x", 3), BASE + timedelta(minutes=3))
        self.assertIsNotNone(row.features["regime.minutes_since_open"])
        self.assertIsNotNone(row.features["regime.minutes_to_close"])

        premarket = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)   # 08:00 ET
        early = self.features_at(series("x", 3, start=premarket),
                                 premarket + timedelta(minutes=3))
        self.assertIsNone(early.features["regime.minutes_since_open"])
        self.assertIsNone(early.features["regime.minutes_to_close"],
                          "one session-position feature answered outside the session")

    def test_a_holiday_has_no_session_position(self):
        holiday = datetime(2026, 11, 26, 15, 0, tzinfo=UTC)   # Thanksgiving
        row = self.features_at(series("x", 3, start=holiday),
                               holiday + timedelta(minutes=3))
        self.assertIsNone(row.features["regime.minutes_since_open"])

    def test_market_context_comes_from_the_benchmark_not_the_instrument(self):
        """§31: a stock's market feature must not be its own return."""
        own = series("x", 40, first=100.0, step=1.0)          # strong uptrend
        market = series("benchmark-spy", 40, first=50.0, step=0.0)  # flat
        row = self.features_at(own, own[-1].bar_end, benchmark=market)
        self.assertEqual(row.features["cross_sectional.market_return_5m"], 0.0)
        self.assertGreater(row.features["market.return_5m"], 0.0)


class TestDeterminismAndIdempotency(FeatureCase):

    def test_the_same_bars_give_exactly_the_same_values(self):
        bars = series("x", 45)
        cutoff = bars[-1].bar_end
        first = self.features_at(bars, cutoff).features
        second = self.features_at(list(bars), cutoff).features
        self.assertEqual(first, second)

    def test_recomputing_produces_one_identity_not_two(self):
        bars = series("x", 20)
        cutoff = bars[-1].bar_end
        a = self.features_at(bars, cutoff)
        b = self.features_at(bars, cutoff)
        self.assertEqual(a.observation_id, b.observation_id)

    def test_a_different_minute_is_a_different_observation(self):
        bars = series("x", 20)
        a = self.features_at(bars, bars[-1].bar_end)
        b = self.features_at(bars, bars[-2].bar_end)
        self.assertNotEqual(a.observation_id, b.observation_id)

    def test_incremental_matches_full_recompute(self):
        """
        §17: updating as one new bar arrives must equal recomputing the
        whole history. If these ever disagree, the incremental path is
        quietly producing a different dataset.
        """
        bars = series("x", 60)
        cutoff = bars[-1].bar_end
        full = self.features_at(bars, cutoff).features

        builder = builder_for(self.conn, registry=self.registry)
        for size in range(31, len(bars) + 1):          # arrive one at a time
            builder.seed_bars("x", bars[:size])
            builder.seed_bars("benchmark-spy", [])
            incremental = builder.observation(
                "x", bars[size - 1].bar_end,
                now=cutoff + timedelta(days=1)).features
        self.assertEqual(incremental, full)

    def test_restart_reproduces_the_uninterrupted_result(self):
        """
        §18, §53: a process that stops and reloads from storage must
        compute the same next row as one that never stopped.
        """
        bars = series("x", 50)
        cutoff = bars[-1].bar_end
        uninterrupted = self.features_at(bars, cutoff).features

        store(self.conn, bars)                          # "the process died"
        restarted = builder_for(self.conn, registry=self.registry)
        reloaded = restarted.observation(
            "x", cutoff, now=cutoff + timedelta(days=1)).features
        self.assertEqual(reloaded, uninterrupted)


class TestRestartContract(FeatureCase):
    """
    §17, §18. The restart guarantee holds for the path production uses
    (reload from storage) and its LIMIT is stated, not discovered later
    by someone whose numbers quietly moved.
    """

    def test_reloading_from_storage_reproduces_the_row_exactly(self):
        bars = series("x", 90)
        cutoff = bars[-1].bar_end
        uninterrupted = self.features_at(bars, cutoff).features
        store(self.conn, bars)
        reloaded = builder_for(self.conn, registry=self.registry).observation(
            "x", cutoff, now=cutoff + timedelta(days=1)).features
        self.assertEqual(reloaded, uninterrupted)

    def test_a_truncated_reload_is_visible_in_run_minutes(self):
        """
        NEGATIVE CONTROL. A caller that reloads too little does NOT get
        silently different numbers presented as equivalent: the run
        length it actually had is reported in the row.
        """
        bars = series("x", 90)
        cutoff = bars[-1].bar_end
        full = self.features_at(bars, cutoff)
        truncated = self.features_at(bars[-30:], cutoff)
        self.assertEqual(full.features["regime.run_minutes"], 90.0)
        self.assertEqual(truncated.features["regime.run_minutes"], 30.0)
        self.assertNotEqual(full.run_minutes, truncated.run_minutes)

    def test_the_indexed_previous_close_matches_a_plain_scan(self):
        """The optimisation must not change which close is 'previous'."""
        day_one = series("x", 30, start=BASE - timedelta(days=1), first=50.0)
        day_two = series("x", 30, start=BASE, first=100.0)
        builder = builder_for(self.conn, registry=self.registry)
        builder.seed_bars("x", day_one + day_two)

        indexed = builder.previous_session_close("x", BASE + timedelta(minutes=10))
        scanned = [b for b in bars_as_of(day_one + day_two, BASE + timedelta(minutes=10))
                   if b.session_date < (BASE + timedelta(minutes=10)).date()
                   and b.close is not None]
        self.assertEqual(indexed, scanned[-1].close)

    def test_no_previous_session_means_no_gap_feature(self):
        builder = builder_for(self.conn, registry=self.registry)
        builder.seed_bars("x", series("x", 10))
        self.assertIsNone(builder.previous_session_close("x", BASE + timedelta(minutes=5)))

    def test_the_required_history_contract_is_declared(self):
        contract = builder_for(self.conn, registry=self.registry).required_history()
        self.assertIn("contiguous_run", contract)
        self.assertIn("previous_session_close", contract)


class TestVersioning(FeatureCase):

    def test_every_intraday_feature_declares_its_version_and_lookback(self):
        registry = self.registry
        for feature_id in intraday_feature_ids(registry):
            definition = registry.get(feature_id)
            self.assertEqual(definition.version, INTRADAY_FEATURE_VERSION)
            self.assertTrue(definition.formula, feature_id)
            self.assertTrue(definition.source, feature_id)
            self.assertIn("@", definition.qualified_id)

    def test_intraday_names_never_collide_with_the_daily_ones(self):
        """`market.return_5m` must not be mistakable for `market.return_5d`."""
        registry = self.registry
        daily = {d.feature_id for d in registry.all() if d.source != "intraday_1m_bars"}
        intraday = set(intraday_feature_ids(registry))
        self.assertEqual(daily & intraday, set())
        self.assertIn("market.return_5d", daily)
        self.assertIn("market.return_5m", intraday)

    def test_a_version_change_changes_the_observation_identity(self):
        """NEGATIVE CONTROL: a v2 value must not inherit a v1 identity."""
        bars = series("x", 10)
        row = self.features_at(bars, bars[-1].bar_end)
        before = row.observation_id
        row.feature_version = "v2"
        self.assertNotEqual(row.observation_id, before)


# ======================================================================
# Labels
# ======================================================================

class TestLabels(unittest.TestCase):

    def test_a_resolved_label_is_the_forward_return(self):
        # Flat at 100 except the bar ending at BASE+6m, which closes 110.
        # From the bar ending at BASE+1m, the 5-minute forward return is
        # therefore exactly +10%.
        bars = series("x", 10, first=100.0, step=0.0)
        bars[5] = bar("x", BASE + timedelta(minutes=5), 110.0)
        label = forward_return(bars, BASE + timedelta(minutes=1), 5,
                               now=BASE + timedelta(days=1))
        self.assertIs(label.state, LabelState.RESOLVED)
        self.assertAlmostEqual(label.value, 0.10)

    def test_a_label_whose_future_has_not_happened_is_unresolved(self):
        """NEGATIVE CONTROL: never trained on, never waited on as missing."""
        bars = series("x", 10)
        cutoff = bars[-1].bar_end
        label = forward_return(bars, cutoff, 30, now=cutoff)
        self.assertIs(label.state, LabelState.UNRESOLVED)
        self.assertIsNone(label.value)

    def test_a_label_whose_future_is_absent_is_missing_not_unresolved(self):
        bars = series("x", 10)
        cutoff = bars[-1].bar_end
        label = forward_return(bars, cutoff, 30, now=cutoff + timedelta(days=5))
        self.assertIs(label.state, LabelState.MISSING_FUTURE_DATA)

    def test_a_forward_window_may_not_span_a_gap(self):
        """
        NEGATIVE CONTROL. Both endpoints exist but a minute between them
        does not: that is an overnight or cross-event jump, not a
        thirty-minute intraday return.
        """
        bars = series("x", 40, skip={20})
        label = forward_return(bars, BASE + timedelta(minutes=10), 30,
                               now=BASE + timedelta(days=1))
        self.assertIs(label.state, LabelState.MISSING_FUTURE_DATA)

    def test_only_resolved_labels_reach_training(self):
        conn = memory_db()
        bars = series("x", 40)
        store(conn, bars)
        builder = builder_for(conn)
        data = builder.build(["x"], now=BASE + timedelta(minutes=25))
        states = data.label_states(30)
        self.assertGreater(states.get("unresolved", 0), 0)
        for row in data.training_rows(30):
            self.assertIs(row.labels["grid_30m"].state, LabelState.RESOLVED)
        conn.close()


# ======================================================================
# Dataset
# ======================================================================

class TestDataset(unittest.TestCase):

    def setUp(self):
        self.conn = memory_db()

    def tearDown(self):
        self.conn.close()

    def test_the_fingerprint_changes_when_a_value_changes(self):
        store(self.conn, series("x", 40))
        first = builder_for(self.conn).build(["x"], now=BASE + timedelta(days=1))
        before = first.fingerprint()

        # one revised close, everything else identical
        store(self.conn, [bar("x", BASE + timedelta(minutes=5), 999.0)])
        second = builder_for(self.conn).build(["x"], now=BASE + timedelta(days=1))
        self.assertNotEqual(second.fingerprint(), before,
                            "a revised bar left the dataset identity unchanged")

    def test_the_fingerprint_is_stable_for_unchanged_data(self):
        store(self.conn, series("x", 30))
        a = builder_for(self.conn).build(["x"], now=BASE + timedelta(days=1))
        b = builder_for(self.conn).build(["x"], now=BASE + timedelta(days=1))
        self.assertEqual(a.fingerprint(), b.fingerprint())

    def test_a_cross_section_groups_one_minute_across_instruments(self):
        store(self.conn, series("x", 20))
        store(self.conn, series("y", 20))
        data = builder_for(self.conn).build(["x", "y"], now=BASE + timedelta(days=1))
        cutoff = BASE + timedelta(minutes=10)
        rows = data.cross_section(cutoff)
        self.assertEqual({r.instrument_id for r in rows}, {"x", "y"})
        self.assertTrue(all(r.cutoff == cutoff for r in rows))

    def test_an_instrument_without_a_closed_bar_is_absent_not_null(self):
        """§37, §55: universe membership is decided by what happened."""
        store(self.conn, series("x", 20))
        store(self.conn, series("y", 5))               # stops early
        data = builder_for(self.conn).build(["x", "y"], now=BASE + timedelta(days=1))
        late = data.cross_section(BASE + timedelta(minutes=15))
        self.assertEqual({r.instrument_id for r in late}, {"x"})

    def test_instruments_do_not_contaminate_each_other(self):
        """§54: a missing bar in one instrument is not the other's problem."""
        store(self.conn, series("x", 40))
        store(self.conn, series("y", 40, skip={20}, first=200.0))
        data = builder_for(self.conn).build(["x", "y"], now=BASE + timedelta(days=1))
        cutoff = BASE + timedelta(minutes=40)
        rows = {r.instrument_id: r for r in data.cross_section(cutoff)}
        self.assertIsNotNone(rows["x"].features["market.return_30m"])
        self.assertIsNone(rows["y"].features["market.return_30m"],
                          "a gap in y did not stop y's own 30-minute window")

    def test_building_per_instrument_matches_a_shared_grid(self):
        """
        The builder walks each instrument's own bar ends. That must give
        exactly the rows a full shared grid would, or the optimisation
        has quietly changed the dataset.
        """
        store(self.conn, series("x", 40))
        store(self.conn, series("y", 40, start=BASE + timedelta(minutes=5),
                                first=200.0))
        now = BASE + timedelta(days=1)
        fast = builder_for(self.conn).build(["x", "y"], now=now)

        every_moment = sorted({b.bar_end for i in ("x", "y")
                               for b in load_research_bars(self.conn, i)})
        slow = builder_for(self.conn).build(["x", "y"], cutoffs=every_moment, now=now)

        self.assertEqual(
            [(r.instrument_id, r.cutoff) for r in fast.rows],
            [(r.instrument_id, r.cutoff) for r in slow.rows])
        self.assertEqual(fast.fingerprint(), slow.fingerprint())

    def test_the_matrix_returns_aligned_x_y_and_keys(self):
        store(self.conn, series("x", 60))
        data = builder_for(self.conn).build(["x"], now=BASE + timedelta(days=1))
        X, y, cutoffs, instruments = data.matrix(5)
        self.assertEqual(len(X), len(y))
        self.assertEqual(len(y), len(cutoffs))
        self.assertEqual(len(cutoffs), len(instruments))
        self.assertTrue(all(i == "x" for i in instruments))


# ======================================================================
# News, normalization, outliers
# ======================================================================

class TestTemporalSplit(unittest.TestCase):
    """
    §36: the split a scaler is fitted on must not contain the boundary
    minute, or the fit has seen validation data.
    """

    def setUp(self):
        self.conn = memory_db()
        store(self.conn, series("x", 40))
        store(self.conn, series("y", 40, first=200.0))
        self.data = builder_for(self.conn).build(
            ["x", "y"], now=BASE + timedelta(days=1))

    def tearDown(self):
        self.conn.close()

    def test_a_minute_is_never_on_both_sides(self):
        training, validation = self.data.temporal_split(0.7)
        self.assertTrue(training and validation)
        self.assertEqual({r.cutoff for r in training} & {r.cutoff for r in validation},
                         set(), "a cutoff appeared in both halves")

    def test_a_cross_section_stays_together(self):
        """Both instruments of one minute land on the same side."""
        training, validation = self.data.temporal_split(0.7)
        by_minute = {}
        for row in training:
            by_minute.setdefault(row.cutoff, set()).add("train")
        for row in validation:
            by_minute.setdefault(row.cutoff, set()).add("validate")
        self.assertTrue(all(len(v) == 1 for v in by_minute.values()))

    def test_the_embargo_drops_rows_whose_label_crosses_the_boundary(self):
        plain, _ = self.data.temporal_split(0.7)
        embargoed, _ = self.data.temporal_split(0.7, horizon=15)
        self.assertLess(len(embargoed), len(plain))

    def test_a_scaler_fitted_on_the_split_stops_before_validation(self):
        training, validation = self.data.temporal_split(0.7)
        scaler = fit_scaler(training, self.data.feature_ids)
        first_validation = min(r.cutoff for r in validation).isoformat()
        self.assertLess(scaler.fitted_through, first_validation)


class TestNewsPointInTime(unittest.TestCase):

    def records(self):
        return [
            NewsRecord("a1", "x", BASE - timedelta(hours=2)),
            NewsRecord("a2", "x", BASE - timedelta(minutes=30)),
            NewsRecord("a3", "x", BASE + timedelta(minutes=30)),   # the future
        ]

    def test_a_future_article_cannot_reach_a_feature(self):
        """NEGATIVE CONTROL: availability, not publication, decides."""
        values = news_features(self.records(), BASE)
        self.assertEqual(values["news.count_24h"], 2.0)

    def test_recency_uses_the_newest_available_article(self):
        values = news_features(self.records(), BASE)
        self.assertAlmostEqual(values["news.minutes_since_last"], 30.0)

    def test_no_news_is_zero_count_but_no_recency(self):
        values = news_features([], BASE)
        self.assertEqual(values["news.count_24h"], 0.0)
        self.assertIsNone(values["news.minutes_since_last"])

    def test_articles_outside_the_window_do_not_count(self):
        old = [NewsRecord("old", "x", BASE - timedelta(days=3))]
        self.assertEqual(news_features(old, BASE)["news.count_24h"], 0.0)


class TestNormalization(unittest.TestCase):

    class _Row:
        def __init__(self, value, cutoff):
            self.features = {"f": value}
            self.cutoff = cutoff

    def test_a_scaler_is_fitted_only_on_the_rows_it_is_given(self):
        training = [self._Row(v, BASE + timedelta(minutes=i))
                    for i, v in enumerate([1.0, 2.0, 3.0])]
        scaler = fit_scaler(training, ["f"])
        self.assertEqual(scaler.fitted_rows, 3)
        self.assertAlmostEqual(scaler.means["f"], 2.0)

    def test_the_scaler_records_how_far_it_saw(self):
        """A scaler fitted through a validation fold is auditable (§36)."""
        training = [self._Row(v, BASE + timedelta(minutes=i))
                    for i, v in enumerate([1.0, 2.0, 3.0])]
        scaler = fit_scaler(training, ["f"])
        self.assertEqual(scaler.fitted_through,
                         (BASE + timedelta(minutes=2)).isoformat())

    def test_cross_sectional_zscore_uses_only_the_given_minute(self):
        values = {"a": 1.0, "b": 2.0, "c": 3.0, "d": None}
        scored = cross_sectional_zscore(values)
        self.assertAlmostEqual(scored["b"], 0.0)
        self.assertIsNone(scored["d"])

    def test_an_outlier_is_clipped_and_reported_never_deleted(self):
        history = [0.01 * (i % 5) for i in range(20)]      # real dispersion
        clipped, was_clipped = winsorized(5.0, history)
        self.assertTrue(was_clipped)
        self.assertLess(clipped, 5.0)
        _unchanged, flag = winsorized(0.02, history)
        self.assertFalse(flag)

    def test_a_constant_history_gives_no_threshold_to_clip_against(self):
        """
        Zero dispersion means the rule is undefined, so the value passes
        through UNCHANGED rather than being clipped against a fabricated
        threshold. Stated because it is a real edge, not an oversight.
        """
        value, flag = winsorized(5.0, [0.01] * 20)
        self.assertEqual(value, 5.0)
        self.assertFalse(flag)


# ======================================================================
# Boundaries this phase must not cross
# ======================================================================

class TestD20AndOperationalIsolation(unittest.TestCase):

    def read(self, relative):
        path = os.path.join(os.path.dirname(__file__), "..", "..", relative)
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_the_intraday_label_family_never_uses_the_d20_anchor(self):
        """§24: a separate method family, asserted rather than promised."""
        for module in ("src/research/intraday_dataset.py",
                       "src/features/intraday.py",
                       "src/marketdata/intraday.py"):
            source = self.read(module)
            self.assertNotIn("impact.anchoring", source, module)
            self.assertNotIn("anchor_v2", source, module)
            self.assertNotIn("validate_d20", source, module)

    def test_the_intraday_layer_never_writes_research_labels(self):
        """The D20 corpus is not extended by this phase."""
        for module in ("src/research/intraday_dataset.py",
                       "src/features/intraday.py"):
            source = self.read(module).upper()
            for verb in ("INSERT INTO RESEARCH_LABELS",
                         "UPDATE RESEARCH_LABELS",
                         "INSERT INTO RESEARCH_OBSERVATIONS"):
                self.assertNotIn(verb, " ".join(source.split()), module)

    def test_the_intraday_layer_never_writes_operational_state(self):
        """§5: research construction must not touch the trading price state."""
        source = " ".join(self.read("src/research/intraday_dataset.py").upper().split())
        self.assertNotIn("MARKET_DATA_STATE", source)

    def test_the_label_version_is_declared(self):
        self.assertEqual(INTRADAY_LABEL_VERSION, "v1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
