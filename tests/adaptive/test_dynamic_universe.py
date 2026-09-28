"""
tests/adaptive/test_dynamic_universe.py
----------------------------------------------------
Phase 25.9I-B: selection integrity, not selection quality.

Every test here attacks one of the rules the phase exists to enforce.
The fixtures are synthetic and deterministic: a selector that depends on
today's database is a selector nobody can test twice.

The most important tests in this file are the lookahead ones. A selector
that can see the close it is supposed to predict will look excellent and
be worthless, and that failure is invisible unless something explicitly
checks for it.
"""

import json
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.adaptive.admission import (
    AdmissionConfig, CandidateAssessment, ChurnPolicy, OpportunityAdmitter,
    expired_assessments,
)
from src.adaptive.eligibility import EligibilityConfig, EligibilityEvaluator
from src.adaptive.scoring import ScoringConfig, SessionScorer, UNAVAILABLE_COMPONENTS
from src.adaptive.selector import SessionUniverseSelector, compare_with_fixed
from src.adaptive.universe import (
    MarketHistory, ParentUniverse, ParentUniverseError, ParentUniverseMember,
)
from src.domain.adaptive_models import (
    AdmissionVerdict, EligibilityStatus, OpportunityScore, OpportunityValidity,
)

UTC = timezone.utc
ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def at(day=27, hour=13, minute=25, month=8):
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


# ======================================================================
# Deterministic synthetic market
# ======================================================================


def build_market(path, instruments, days=120, start=datetime(2026, 5, 1, tzinfo=UTC),
                 close=100.0, volume=2_000_000):
    """A cache with one predictable candle per day per instrument."""
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE IF NOT EXISTS price_candle_cache (
        instrument_id TEXT, interval TEXT, timestamp TEXT, open REAL, high REAL,
        low REAL, close REAL, adjusted_close REAL, volume REAL, source TEXT,
        fetched_at TEXT, PRIMARY KEY (instrument_id, interval, timestamp))""")
    for instrument_id, spec in instruments.items():
        price = spec.get("close", close)
        vol = spec.get("volume", volume)
        for offset in range(spec.get("days", days)):
            stamp = (start + timedelta(days=offset)).isoformat()
            drift = spec.get("daily_drift", 0.0)
            bar_close = price * (1.0 + drift) ** offset
            conn.execute(
                "INSERT OR REPLACE INTO price_candle_cache (instrument_id, interval,"
                " timestamp, open, high, low, close, volume, source, fetched_at) "
                "VALUES (?,'1d',?,?,?,?,?,?,'test','x')",
                (instrument_id, stamp, bar_close, bar_close, bar_close,
                 bar_close, vol))
    conn.commit()
    conn.close()
    return path


def parent_of(*instrument_ids, sectors=None, benchmark=True):
    sectors = sectors or {}
    members = tuple(ParentUniverseMember(
        instrument_id=i, ticker=i.split("-")[-1].upper(),
        sector_id=sectors.get(i, "technology")) for i in instrument_ids)
    benchmarks = (ParentUniverseMember(
        instrument_id="benchmark-spy", ticker="SPY", sector_id="benchmark",
        asset_class="etf", role="benchmark"),) if benchmark else ()
    return ParentUniverse(version="test-v1", as_of=at(), rule="test rule",
                          members=members, benchmarks=benchmarks,
                          source="synthetic")


class _Case(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "market.db")

    def market(self, instruments, with_benchmark=True, **kwargs):
        """
        Build the fixture market, including the benchmark by default.

        Without a benchmark series `relative_strength` is unavailable,
        which drops score completeness from 0.417 to 0.333 -- under the
        0.40 default -- and every candidate becomes INSUFFICIENT_DATA.
        That is correct behaviour and it caught these fixtures, but it
        is not what most of these tests are about.
        """
        if with_benchmark:
            instruments = dict(instruments)
            instruments.setdefault("benchmark-spy", {"volume": 50_000_000})
        return build_market(self.db, instruments, **kwargs)

    def select(self, parent, as_of=None, **config):
        as_of = as_of or at()
        selector = SessionUniverseSelector(
            parent, admission=AdmissionConfig(**config.pop("admission", {})),
            eligibility=EligibilityConfig(**config.pop("eligibility", {})),
            scoring=ScoringConfig(**config.pop("scoring", {})))
        with MarketHistory(self.db, as_of) as history:
            return selector.select(history, as_of, **config)


# ======================================================================


class TestParentUniverseArtifact(unittest.TestCase):

    def test_the_real_artifact_loads_and_carries_provenance(self):
        parent = ParentUniverse.load()
        self.assertEqual(parent.version, "v1")
        self.assertGreaterEqual(len(parent.members), 75)
        self.assertLessEqual(len(parent.members), 150)
        self.assertTrue(parent.rule)
        self.assertTrue(parent.source)
        self.assertEqual(len(parent.members_sha256), 64)
        self.assertTrue(parent.as_of)

    def test_an_edited_artifact_is_refused(self):
        with open(os.path.join(ROOT, "config", "parent_universe_v1.json"),
                  encoding="utf-8") as handle:
            raw = json.load(handle)
        raw["members"] = raw["members"][:-1]          # tamper
        with self.assertRaises(ParentUniverseError) as caught:
            ParentUniverse.from_dict(raw)
        self.assertIn("sha256", str(caught.exception))

    def test_the_capture_universe_artifact_is_untouched(self):
        # Byte-identical to what was registered on 2026-09-19.
        import hashlib
        path = os.path.join(ROOT, "config", "capture_universe_v1.json")
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        self.assertTrue(digest.startswith("66b2c3098f3c93a7"),
                        "capture_universe_v1.json changed: %s" % digest[:16])

    def test_a_benchmark_may_not_also_be_a_candidate(self):
        member = ParentUniverseMember(instrument_id="benchmark-spy", ticker="SPY",
                                      sector_id="benchmark")
        with self.assertRaises(ParentUniverseError):
            ParentUniverse(version="v", as_of=at(), rule="r", members=(member,),
                           benchmarks=(member,))

    def test_a_member_without_sector_metadata_is_refused(self):
        with self.assertRaises(ParentUniverseError):
            ParentUniverseMember(instrument_id="x", ticker="X", sector_id="")

    def test_parent_universe_selection_is_deterministic(self):
        # The builder's own functions, run twice over one database.
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import build_parent_universe as builder
        conn = sqlite3.connect("file:%s?mode=ro"
                               % os.path.join(ROOT, "data", "marketlens.db")
                               .replace("\\", "/"), uri=True)
        try:
            first, _ = builder.candidates(conn, "2026-08-27T23:59:59+00:00", 150, 60)
            second, _ = builder.candidates(conn, "2026-08-27T23:59:59+00:00", 150, 60)
        finally:
            conn.close()
        self.assertEqual(first, second)
        self.assertEqual([m["instrument_id"] for m in builder.apply_sector_cap(first, 10)[0]],
                         [m["instrument_id"] for m in builder.apply_sector_cap(second, 10)[0]])


class TestCapacityIsACeiling(_Case):

    def test_zero_candidates_gives_an_empty_session(self):
        self.market({"a": {"volume": 1}})            # far below the liquidity floor
        selection = self.select(parent_of("a"), admission={"max_capacity": 31})
        self.assertEqual(selection.universe.occupied, 0)
        self.assertEqual(selection.unused_capacity, 31)
        self.assertTrue(selection.universe.is_empty)

    def test_fewer_than_capacity_leaves_slots_empty(self):
        self.market({"a": {}, "b": {}, "c": {}})
        selection = self.select(parent_of("a", "b", "c"),
                                admission={"max_capacity": 31})
        self.assertLessEqual(selection.universe.occupied, 3)
        self.assertEqual(selection.unused_capacity,
                         31 - selection.universe.occupied)

    def test_weak_candidates_are_never_promoted_to_fill_capacity(self):
        # One strong name, four that sit below the pass threshold. The
        # session takes the one, and leaves thirty slots empty.
        instruments = {"strong": {"volume": 40_000_000}}
        for name in ("w1", "w2", "w3", "w4"):
            instruments[name] = {"volume": 120_000}
        self.market(instruments)
        selection = self.select(
            parent_of(*instruments), admission={"max_capacity": 31},
            eligibility={"min_median_dollar_volume": 1000.0})
        verdicts = selection.verdict_counts()
        self.assertGreater(verdicts["pass"] + verdicts["watch"], 0)
        for instrument_id in selection.universe.members:
            assessment = selection.assessment(instrument_id)
            self.assertIs(assessment.verdict, AdmissionVerdict.PASS)
        self.assertGreater(selection.unused_capacity, 0)

    def test_over_capacity_is_impossible_and_the_rest_are_waitlisted(self):
        instruments = {"n%02d" % i: {"volume": 5_000_000 + i} for i in range(12)}
        self.market(instruments)
        selection = self.select(parent_of(*instruments),
                                admission={"max_capacity": 4,
                                           "max_per_sector": None})
        self.assertEqual(selection.universe.occupied, 4)
        self.assertEqual(selection.unused_capacity, 0)
        self.assertEqual(len(selection.waitlist), 8)
        # Waitlisted names passed quality; they lost a capacity contest.
        for instrument_id in selection.waitlist:
            self.assertIs(selection.assessment(instrument_id).verdict,
                          AdmissionVerdict.PASS)

    def test_capacity_zero_selects_nothing(self):
        self.market({"a": {}})
        selection = self.select(parent_of("a"), admission={"max_capacity": 0})
        self.assertEqual(selection.universe.occupied, 0)


class TestEligibility(_Case):

    def test_no_data_is_insufficient_not_eligible(self):
        self.market({"a": {}})
        evaluator = EligibilityEvaluator()
        member = ParentUniverseMember("ghost", "GHOST", "technology")
        with MarketHistory(self.db, at()) as history:
            decision = evaluator.evaluate(member, history.history("ghost"), at())
        self.assertIs(decision.status, EligibilityStatus.INSUFFICIENT_DATA)
        self.assertFalse(decision.is_eligible)
        self.assertTrue(decision.reasons)

    def test_insufficient_data_becomes_its_own_verdict(self):
        self.market({"a": {}})
        selection = self.select(parent_of("a", "ghost"))
        self.assertIs(selection.assessment("ghost").verdict,
                      AdmissionVerdict.INSUFFICIENT_DATA)
        self.assertNotIn("ghost", selection.universe.members)

    def test_thin_liquidity_is_ineligible_with_a_reason(self):
        self.market({"thin": {"volume": 10}})
        selection = self.select(parent_of("thin"))
        assessment = selection.assessment("thin")
        self.assertIs(assessment.eligibility.status, EligibilityStatus.INELIGIBLE)
        self.assertTrue(any("dollar volume" in r for r in assessment.reasons))

    def test_a_stale_cache_is_caught_when_the_bound_is_configured(self):
        self.market({"a": {}}, start=datetime(2026, 1, 1, tzinfo=UTC), days=200)
        selection = self.select(parent_of("a"), as_of=at(month=12, day=1),
                                eligibility={"max_data_age_days": 5.0})
        assessment = selection.assessment("a")
        self.assertIs(assessment.eligibility.status, EligibilityStatus.INELIGIBLE)
        self.assertTrue(any("days old" in r for r in assessment.reasons))

    def test_unsupported_security_types_are_refused(self):
        self.market({"a": {}})
        member = ParentUniverseMember("a", "A", "technology", sec_type="FUT")
        with MarketHistory(self.db, at()) as history:
            decision = EligibilityEvaluator().evaluate(member, history.history("a"), at())
        self.assertIs(decision.status, EligibilityStatus.INELIGIBLE)


class TestScoring(_Case):

    def test_components_are_preserved_and_weighted(self):
        self.market({"a": {}})
        with MarketHistory(self.db, at()) as history:
            score = SessionScorer().score(
                ParentUniverseMember("a", "A", "technology"),
                history.history("a"), at())
        self.assertTrue(score.components)
        self.assertTrue(score.weights)
        self.assertAlmostEqual(sum(score.weights.values()), 1.0, places=6)

    def test_model_components_are_unavailable_not_zero(self):
        self.market({"a": {}})
        with MarketHistory(self.db, at()) as history:
            score = SessionScorer().score(
                ParentUniverseMember("a", "A", "technology"),
                history.history("a"), at())
        self.assertIn("expected_return", score.unavailable)
        self.assertIn("model_confidence", score.unavailable)
        self.assertNotIn("expected_return", score.components)
        self.assertLess(score.completeness, 1.0)

    def test_a_component_cannot_be_measured_and_unavailable_at_once(self):
        with self.assertRaises(ValueError):
            OpportunityScore(overall=0.5, components={"liquidity": 0.5},
                             unavailable=("liquidity",))

    def test_relative_strength_needs_a_benchmark(self):
        self.market({"a": {}, "benchmark-spy": {}})
        member = ParentUniverseMember("a", "A", "technology")
        with MarketHistory(self.db, at()) as history:
            without = SessionScorer().score(member, history.history("a"), at())
            with_bench = SessionScorer().score(
                member, history.history("a"), at(),
                benchmark=history.history("benchmark-spy"))
        self.assertIn("relative_strength", without.unavailable)
        self.assertIn("relative_strength", with_bench.components)

    def test_every_declared_unavailable_component_has_a_reason(self):
        for name, reason in UNAVAILABLE_COMPONENTS.items():
            self.assertTrue(reason, "%s has no stated reason" % name)


class TestAdmissionSeparateFromScoring(_Case):

    def admitter(self, **config):
        return OpportunityAdmitter(AdmissionConfig(**config))

    def assessment(self, overall, completeness_components=4):
        from src.domain.adaptive_models import EligibilityDecision
        score = OpportunityScore(
            overall=overall,
            components={"c%d" % i: overall for i in range(completeness_components)},
            unavailable=tuple("u%d" % i for i in range(1)))
        eligibility = EligibilityDecision(
            instrument_id="a", status=EligibilityStatus.ELIGIBLE, as_of=at())
        return self.admitter().judge(
            ParentUniverseMember("a", "A", "technology"), eligibility, score, at())

    def test_a_high_score_is_not_automatically_admission(self):
        admitter = self.admitter(pass_threshold=0.9)
        from src.domain.adaptive_models import EligibilityDecision
        eligibility = EligibilityDecision("a", EligibilityStatus.ELIGIBLE, at())
        score = OpportunityScore(overall=0.85, components={"a": 0.85, "b": 0.85})
        judged = admitter.judge(ParentUniverseMember("a", "A", "technology"),
                                eligibility, score, at())
        self.assertIs(judged.verdict, AdmissionVerdict.WATCH)
        self.assertTrue(judged.reasons)

    def test_every_non_pass_verdict_carries_reasons(self):
        for overall in (0.0, 0.45, 0.9):
            assessment = self.assessment(overall)
            if assessment.verdict is not AdmissionVerdict.PASS:
                self.assertTrue(assessment.reasons, overall)

    def test_low_completeness_is_insufficient_data_not_a_low_score(self):
        from src.domain.adaptive_models import EligibilityDecision
        admitter = self.admitter(min_completeness=0.9)
        score = OpportunityScore(overall=0.99, components={"a": 0.99},
                                 unavailable=("b", "c", "d"))
        judged = admitter.judge(
            ParentUniverseMember("a", "A", "technology"),
            EligibilityDecision("a", EligibilityStatus.ELIGIBLE, at()), score, at())
        self.assertIs(judged.verdict, AdmissionVerdict.INSUFFICIENT_DATA)
        self.assertIn("completeness", judged.reasons[0])

    def test_pass_maps_to_the_phase_i_a_lifecycle_status(self):
        from src.domain.adaptive_models import OpportunityStatus
        self.assertIs(self.assessment(0.99).status, OpportunityStatus.QUALIFIED)
        self.assertIs(self.assessment(0.0).status, OpportunityStatus.REJECTED)


class TestSectorConcentration(_Case):

    def test_a_sector_cap_limits_without_forcing_other_sectors(self):
        instruments = {"t%d" % i: {"volume": 9_000_000 - i} for i in range(8)}
        instruments["weak"] = {"volume": 150_000}
        sectors = {name: "technology" for name in instruments}
        sectors["weak"] = "healthcare"
        self.market(instruments)
        selection = self.select(
            parent_of(*instruments, sectors=sectors),
            admission={"max_capacity": 31, "max_per_sector": 3},
            eligibility={"min_median_dollar_volume": 1_000_000.0})
        distribution = selection.sector_distribution()
        self.assertLessEqual(distribution.get("technology", 0), 3)
        # Healthcare had nothing above the bar, so healthcare contributes
        # nothing. The empty slots stay empty.
        self.assertNotIn("healthcare", distribution)
        self.assertGreater(selection.unused_capacity, 0)

    def test_sector_overflow_is_recorded_as_a_reason(self):
        instruments = {"t%d" % i: {"volume": 9_000_000 - i} for i in range(5)}
        self.market(instruments)
        selection = self.select(parent_of(*instruments),
                                admission={"max_capacity": 31, "max_per_sector": 2})
        declined = [d for d in selection.admissions
                    if d.outcome.value == "declined"]
        self.assertTrue(declined)
        self.assertTrue(any("sector" in r for d in declined for r in d.reasons))

    def test_the_tighter_of_the_two_sector_limits_applies(self):
        config = AdmissionConfig(max_capacity=30, max_per_sector=10,
                                 max_sector_fraction=0.1)
        self.assertEqual(config.sector_limit(), 3)


class TestDeterminismAndNoLookahead(_Case):

    def test_same_inputs_and_as_of_give_the_same_selection(self):
        self.market({"a": {}, "b": {"volume": 3_000_000}, "c": {}})
        first = self.select(parent_of("a", "b", "c"))
        second = self.select(parent_of("a", "b", "c"))
        self.assertEqual(first.universe.members, second.universe.members)
        self.assertEqual(first.content_hash(), second.content_hash())
        self.assertEqual(first.snapshot_id, second.snapshot_id)

    def test_future_candles_cannot_change_an_earlier_selection(self):
        # One database holds the future; the other stops at as_of. The
        # selection must be identical, which is only true if every read
        # is bounded.
        as_of = at()
        self.market({"a": {}, "b": {"volume": 3_000_000}},
                    start=datetime(2026, 5, 1, tzinfo=UTC), days=200)
        with_future = self.select(parent_of("a", "b"), as_of=as_of)

        truncated = os.path.join(self.dir, "truncated.db")
        source = sqlite3.connect(self.db)
        source.execute("ATTACH DATABASE ? AS trimmed", (truncated,))
        source.execute("""CREATE TABLE trimmed.price_candle_cache AS
                          SELECT * FROM price_candle_cache WHERE timestamp <= ?""",
                       (as_of.isoformat(),))
        source.commit()
        source.close()
        selector = SessionUniverseSelector(parent_of("a", "b"))
        with MarketHistory(truncated, as_of) as history:
            without_future = selector.select(history, as_of)

        self.assertEqual(with_future.universe.members,
                         without_future.universe.members)
        self.assertEqual(with_future.content_hash(), without_future.content_hash())

    def test_todays_close_cannot_influence_a_pre_open_selection(self):
        # A pre-open decision at 13:25 UTC must not read the 20:00 bar.
        pre_open = datetime(2026, 8, 27, 13, 25, tzinfo=UTC)
        self.market({"a": {}}, start=datetime(2026, 5, 1, tzinfo=UTC), days=120)
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT OR REPLACE INTO price_candle_cache (instrument_id, "
                     "interval, timestamp, open, high, low, close, volume, source, "
                     "fetched_at) VALUES ('a','1d',?,1,1,1,999999,99999999,'x','x')",
                     (datetime(2026, 8, 27, 20, 0, tzinfo=UTC).isoformat(),))
        conn.commit()
        conn.close()
        with MarketHistory(self.db, pre_open) as history:
            candles = history.history("a")
        self.assertNotIn(999999.0, candles.closes)
        self.assertLessEqual(max(candles.timestamps), pre_open.isoformat())

    def test_history_reads_are_bounded_even_for_the_benchmark(self):
        self.market({"benchmark-spy": {}}, start=datetime(2026, 5, 1, tzinfo=UTC),
                    days=200)
        with MarketHistory(self.db, at()) as history:
            candles = history.history("benchmark-spy")
        self.assertTrue(all(t <= at().isoformat() for t in candles.timestamps))


class TestExpiryAndIntradayAdmission(_Case):

    def test_expiry_is_deterministic_and_reasoned(self):
        self.market({"a": {}})
        selection = self.select(parent_of("a"),
                                admission={"validity": timedelta(minutes=30)})
        if not selection.universe.members:
            self.skipTest("fixture produced no admission")
        later = at(hour=14, minute=30)
        expired = expired_assessments(selection, later)
        self.assertEqual([i for i, _ in expired], list(selection.universe.members))
        self.assertTrue(all(reason for _, reason in expired))

    def test_an_expired_member_does_not_keep_its_slot_through_a_revision(self):
        self.market({"a": {}, "b": {"volume": 4_000_000}})
        parent = parent_of("a", "b")
        selector = SessionUniverseSelector(
            parent, admission=AdmissionConfig(validity=timedelta(minutes=10)))
        with MarketHistory(self.db, at()) as history:
            first = selector.select(history, at())
        self.assertTrue(first.universe.members)
        later = at(hour=15)
        with MarketHistory(self.db, later) as history:
            revised = selector.revise(first, history, later, candidates=())
        self.assertEqual(revised.universe.members, ())

    def test_intraday_admission_uses_free_capacity_and_flags_data_activation(self):
        self.market({"a": {}, "late": {"volume": 6_000_000}})
        parent = parent_of("a", "late")
        selector = SessionUniverseSelector(parent,
                                           admission=AdmissionConfig(max_capacity=31))
        with MarketHistory(self.db, at()) as history:
            first = selector.select(history, at(), session_date="2026-08-27")
        before = set(first.universe.members)
        later = at(hour=18)
        with MarketHistory(self.db, later) as history:
            revised = selector.revise(first, history, later, candidates=("late",))
        self.assertEqual(revised.revision, 1)
        self.assertEqual(revised.previous_snapshot_id, first.snapshot_id)
        newly = set(revised.universe.members) - before
        for instrument_id in newly:
            self.assertTrue(
                revised.assessment(instrument_id).requires_market_data_activation,
                "a newly admitted instrument must declare it needs data")

    def test_intraday_admission_touches_no_subscription(self):
        # The selector holds no transport and no gateway: nothing it can
        # call could open a subscription.
        selector = SessionUniverseSelector(parent_of("a"))
        for forbidden in ("transport", "gateway", "broker", "subscribe"):
            self.assertFalse(hasattr(selector, forbidden))

    def test_free_capacity_stays_free_when_nothing_qualifies(self):
        self.market({"a": {}, "weak": {"volume": 50_000}})
        parent = parent_of("a", "weak")
        selector = SessionUniverseSelector(parent)
        with MarketHistory(self.db, at()) as history:
            first = selector.select(history, at())
        later = at(hour=18)
        with MarketHistory(self.db, later) as history:
            revised = selector.revise(first, history, later, candidates=("weak",))
        self.assertNotIn("weak", revised.universe.members)
        self.assertGreater(revised.unused_capacity, 0)


class TestChurnPolicy(unittest.TestCase):

    def test_default_policy_permits_a_strictly_better_challenger(self):
        permitted, _ = ChurnPolicy().permits_replacement(
            incumbent_score=0.50, challenger_score=0.51)
        self.assertTrue(permitted)

    def test_a_margin_blocks_a_marginal_swap(self):
        policy = ChurnPolicy(replacement_margin=0.05)
        self.assertFalse(policy.permits_replacement(
            incumbent_score=0.50, challenger_score=0.52)[0])
        self.assertTrue(policy.permits_replacement(
            incumbent_score=0.50, challenger_score=0.60)[0])

    def test_minimum_residency_fails_closed_when_unknown(self):
        policy = ChurnPolicy(replacement_margin=0.0,
                             min_residency=timedelta(minutes=30))
        permitted, reason = policy.permits_replacement(
            incumbent_score=0.1, challenger_score=0.9, incumbent_held_for=None)
        self.assertFalse(permitted)
        self.assertIn("unknown", reason)

    def test_cooldown_blocks_a_recently_evicted_candidate(self):
        policy = ChurnPolicy(cooldown=timedelta(minutes=30))
        self.assertFalse(policy.permits_replacement(
            incumbent_score=0.1, challenger_score=0.9,
            incumbent_held_for=timedelta(hours=2), evicted_recently=True)[0])


class TestBenchmarkSemantics(_Case):

    def test_the_benchmark_never_consumes_a_slot(self):
        self.market({"a": {}, "benchmark-spy": {"volume": 90_000_000}})
        selection = self.select(parent_of("a"))
        self.assertNotIn("benchmark-spy", selection.universe.members)
        self.assertIn("benchmark-spy", selection.benchmarks)

    def test_the_repository_treats_spy_as_a_reference_instrument(self):
        # Evidence, not assertion: SPY is absent from the instrument
        # master, which is why it cannot be a trade candidate here.
        conn = sqlite3.connect("file:%s?mode=ro" % os.path.join(
            ROOT, "data", "marketlens.db").replace("\\", "/"), uri=True)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM instruments WHERE instrument_id LIKE "
                "'benchmark%'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)

    def test_the_real_parent_universe_carries_spy_as_a_benchmark_only(self):
        parent = ParentUniverse.load()
        self.assertEqual([b.instrument_id for b in parent.benchmarks],
                         ["benchmark-spy"])
        self.assertNotIn("benchmark-spy", parent.instrument_ids)


class TestShadowComparison(_Case):

    def test_shadow_reports_differences_without_claiming_performance(self):
        self.market({"a": {}, "b": {"volume": 5_000_000}})
        selection = self.select(parent_of("a", "b"))
        comparison = compare_with_fixed(selection, ["a", "zzz"])
        self.assertEqual(comparison["overlap"], ["a"] if "a" in
                         selection.universe.members else [])
        self.assertIn("zzz", comparison["dropped_by_dynamic"])
        self.assertIn("no claim", comparison["note"])
        self.assertNotIn("return", json.dumps(comparison).lower().split("reasons")[0])


if __name__ == "__main__":
    unittest.main()
