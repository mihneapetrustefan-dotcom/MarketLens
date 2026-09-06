"""
tests/memory/test_patterns_and_leakage.py
-----------------------------------------------------
Patterns, point-in-time memory, and the adversarial cases.

Covers §58 items 7-20 and all eleven of §59.

THE PROPERTY EVERYTHING ELSE DEPENDS ON
-------------------------------------------
`memory_as_of(T)` must return only experience knowable before T. Phase
22 and every later learning phase will use it to evaluate historical
decisions, and if it leaks, every result they produce will be
excellent and worthless.

Several tests below construct a record with experiences spread across
time and assert that an earlier query genuinely sees less — including
that patterns are RECOMPUTED rather than read, since a stored pattern
was aggregated over everything and carries the future in its averages.
"""

import ast
import json
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.attribution_schema import initialize_attribution_schema
from src.data_access.memory_schema import initialize_memory_schema
from src.data_access.outcome_schema import initialize_outcome_schema
from src.domain.memory_models import (
    MEMORY_METHOD_VERSION, MIN_PATTERN_SAMPLE, MIN_STABILITY_PERIODS,
    STABILITY_DIVERGENCE, MemoryConfidence, PatternPeriod, PatternQuality,
    assess_confidence, assess_stability, pattern_id_for,
)
from src.memory import api, patterns as pattern_layer, retrieval

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
PACKAGE = os.path.join(ROOT, "src", "memory")

WRITABLE = {"trading_experiences", "memory_patterns",
            "memory_pattern_evidence", "memory_snapshots"}

FORBIDDEN = {
    "research_features", "research_labels", "research_observations",
    "predictions", "signals", "signal_contributions", "trained_models",
    "model_evaluations", "model_promotions", "outcome_measurements",
    "outcome_aggregates", "error_attributions", "attribution_evidence",
    "price_candle_cache", "events", "canonical_events", "recommendations",
}

BASE = datetime(2026, 8, 1, tzinfo=timezone.utc)


class MemoryCase(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        initialize_outcome_schema(self.conn)
        initialize_attribution_schema(self.conn)
        initialize_memory_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def add_experience(self, experience_id, *, day, direction="long",
                       horizon="5d", result="hit", actual=0.03,
                       quality="validated", regime=None, event="earnings",
                       instrument="i-1", asset_class="stock", model="tm-1",
                       primary_error="no_error", memory_version=MEMORY_METHOD_VERSION):
        available = (BASE + timedelta(days=day)).isoformat()
        self.conn.execute("""
            INSERT OR REPLACE INTO trading_experiences (
                experience_id, memory_version, kind, subject_kind, subject_id,
                horizon, outcome_method_version, attribution_method_version,
                trained_model_id, model_status, strategy_id, information_cutoff,
                available_at, expected_direction, expected_return,
                expected_horizon, signal_confidence, signal_strength,
                actual_return, actual_direction, direction_result, mfe, mae,
                primary_error, contributing_errors, attribution_confidence,
                evidence_count, context_schema_version, context_json,
                market_regime, event_type, instrument_id, asset_class,
                sector_id, experience_class, quality, notes_json, created_at
            ) VALUES (?,?,'signal','signal',?,?, 'v1','v1',?,'evaluated',
                      'ml_directional',?,?,?,0.02,?,0.3,0.5,?,?,?,0.04,-0.02,
                      ?,'[]','high',3,'ctx-v1','{}',?,?,?,?,'tech',
                      'expected_win',?,'[]','2026-09-05T00:00:00+00:00')
        """, (experience_id, memory_version, experience_id, horizon, model,
              (BASE + timedelta(days=day - 5)).isoformat(), available,
              direction, horizon, actual,
              "long" if (actual or 0) > 0 else "short", result,
              primary_error, regime, event, instrument, asset_class, quality))
        self.conn.commit()

    def seed(self, count, **kwargs):
        for index in range(count):
            self.add_experience(f"exp-{kwargs.get('tag','a')}-{index}",
                                day=kwargs.get("day", 1) + (index % 20),
                                **{k: v for k, v in kwargs.items()
                                   if k not in ("tag", "day")})


# ======================================================================
# §58.7-9, §23-§26 — patterns
# ======================================================================

class TestPatternCreation(MemoryCase):

    def test_patterns_are_built_from_stated_families(self):
        self.seed(40, tag="a")
        found, evidence = pattern_layer.build_all(self.conn)
        self.assertTrue(found)
        types = {p.pattern_type for p in found}
        self.assertIn("signal_direction_horizon", types)

    def test_every_pattern_points_at_the_experiences_that_formed_it(self):
        """§25: no orphaned knowledge."""
        self.seed(40, tag="a")
        found, evidence = pattern_layer.build_all(self.conn)
        for pattern in found:
            self.assertIn(pattern.pattern_id, evidence)
            self.assertEqual(len(evidence[pattern.pattern_id]),
                             pattern.sample_size)

    def test_a_pattern_id_is_deterministic(self):
        conditions = {"expected_direction": "long", "horizon": "5d"}
        self.assertEqual(pattern_id_for("x", conditions, "v1"),
                         pattern_id_for("x", conditions, "v1"))

    def test_a_small_pattern_is_weak_and_quotes_no_rate(self):
        """§26: do not manufacture confidence."""
        self.seed(3, tag="tiny")
        found, _ = pattern_layer.build_all(self.conn)
        small = [p for p in found if p.sample_size < MIN_PATTERN_SAMPLE]
        self.assertTrue(small)
        for pattern in small:
            self.assertEqual(pattern.quality, PatternQuality.WEAK)
            self.assertEqual(pattern.confidence,
                             MemoryConfidence.INSUFFICIENT_EVIDENCE)
            self.assertIn("too few", pattern.describe())

    def test_a_pattern_description_never_claims_causation(self):
        """§0: do not turn correlation into causation."""
        self.seed(40, tag="a")
        found, _ = pattern_layer.build_all(self.conn)
        for pattern in found:
            text = pattern.describe().lower()
            for forbidden in ("causes", "because of", "will be", "guarantees"):
                self.assertNotIn(forbidden, text)

    def test_a_large_pattern_says_what_happened_not_what_will(self):
        self.seed(40, tag="a")
        found, _ = pattern_layer.build_all(self.conn)
        big = max(found, key=lambda p: p.sample_size)
        self.assertIn("not what will happen", big.describe())

    def test_a_cohort_keyed_on_a_missing_dimension_is_skipped(self):
        """
        An 'unknown' cohort would be the largest pattern in the database
        and would mean nothing.
        """
        self.seed(40, tag="a", regime=None)
        found, _ = pattern_layer.build_all(self.conn)
        regime_patterns = [p for p in found
                           if p.pattern_type.startswith("regime")]
        self.assertEqual(regime_patterns, [])

    def test_experimental_experience_is_counted_separately(self):
        """§6: never silently pooled."""
        self.seed(20, tag="v", quality="validated")
        self.seed(20, tag="e", quality="experimental")
        found, _ = pattern_layer.build_all(self.conn)
        mixed = [p for p in found if p.experiment_count > 0]
        self.assertTrue(mixed)
        for pattern in mixed:
            self.assertTrue(any("unpromoted model" in note
                                for note in pattern.notes))


# ======================================================================
# §58.10-13, §28-§32
# ======================================================================

class TestStabilityRecencyAndConflict(MemoryCase):

    def test_stability_needs_at_least_two_populated_periods(self):
        verdict, notes = assess_stability([
            PatternPeriod("w1", sample_size=100, hit_rate=0.6)])
        self.assertEqual(verdict, "insufficient_history")
        self.assertTrue(notes)

    def test_diverging_periods_are_unstable_not_averaged(self):
        """
        §29: a pattern that worked then failed is unstable, not good
        with noise. An average over the two describes neither.
        """
        verdict, notes = assess_stability([
            PatternPeriod("w1", sample_size=100, hit_rate=0.75),
            PatternPeriod("w2", sample_size=100, hit_rate=0.35)])
        self.assertEqual(verdict, "unstable")
        self.assertIn("would describe none of them", notes[0])

    def test_consistent_periods_are_stable(self):
        verdict, _ = assess_stability([
            PatternPeriod("w1", sample_size=100, hit_rate=0.55),
            PatternPeriod("w2", sample_size=100, hit_rate=0.58)])
        self.assertEqual(verdict, "stable")

    def test_a_thin_period_does_not_count_toward_stability(self):
        verdict, _ = assess_stability([
            PatternPeriod("w1", sample_size=100, hit_rate=0.75),
            PatternPeriod("w2", sample_size=3, hit_rate=0.0)])
        self.assertEqual(verdict, "insufficient_history")

    def test_confidence_is_capped_by_instability(self):
        self.assertEqual(
            assess_confidence(sample_size=200, stability="unstable",
                              stdev_return=0.01, conflicting=False),
            MemoryConfidence.LOW)

    def test_conflicting_evidence_overrides_everything(self):
        """§31: do not average blindly."""
        self.assertEqual(
            assess_confidence(sample_size=10_000, stability="stable",
                              stdev_return=0.001, conflicting=True),
            MemoryConfidence.CONFLICTING_EVIDENCE)

    def test_a_small_sample_can_never_reach_high_confidence(self):
        """§59: a small sample must not become a high-confidence pattern."""
        for stability in ("stable", "unstable", "insufficient_history"):
            self.assertEqual(
                assess_confidence(sample_size=MIN_PATTERN_SAMPLE - 1,
                                  stability=stability, stdev_return=0.0,
                                  conflicting=False),
                MemoryConfidence.INSUFFICIENT_EVIDENCE)

    def test_a_short_history_caps_confidence_at_medium(self):
        """Enough observations, not enough calendar, is provisional."""
        self.assertEqual(
            assess_confidence(sample_size=500, stability="insufficient_history",
                              stdev_return=0.01, conflicting=False),
            MemoryConfidence.MEDIUM)

    def test_regime_breakdown_is_stored_beside_the_overall_numbers(self):
        """§30: never collapsed into one score."""
        self.seed(40, tag="lo", regime="low_vol", result="hit", actual=0.03)
        self.seed(40, tag="hi", regime="high_vol", result="miss", actual=-0.03)
        found, _ = pattern_layer.build_all(self.conn)
        with_regimes = [p for p in found if len(p.regime_breakdown) > 1]
        self.assertTrue(with_regimes)
        pattern = with_regimes[0]
        self.assertIn("low_vol", pattern.regime_breakdown)
        self.assertIn("high_vol", pattern.regime_breakdown)

    def test_opposing_regimes_make_a_pattern_conflicting(self):
        self.seed(40, tag="lo", regime="low_vol", result="hit", actual=0.03)
        self.seed(40, tag="hi", regime="high_vol", result="miss", actual=-0.03)
        found, _ = pattern_layer.build_all(self.conn)
        conflicting = [p for p in found
                       if p.quality == PatternQuality.CONFLICTING]
        self.assertTrue(conflicting)
        self.assertTrue(any("context-dependent" in c
                            for c in conflicting[0].contradictions))

    def test_contradictions_between_patterns_are_surfaced_not_resolved(self):
        """§32: contradictions are useful information."""
        self.seed(40, tag="up", direction="long", result="hit", actual=0.03)
        self.seed(40, tag="dn", direction="short", result="miss", actual=0.03)
        found, _ = pattern_layer.build_all(self.conn)
        contradictions = pattern_layer.find_contradictions(found)
        self.assertTrue(contradictions)
        self.assertIn("not a tie-break", contradictions[0]["note"])

    def test_a_pattern_records_when_it_was_first_and_last_seen(self):
        self.seed(40, tag="a")
        found, _ = pattern_layer.build_all(self.conn)
        big = max(found, key=lambda p: p.sample_size)
        self.assertIsNotNone(big.first_seen)
        self.assertIsNotNone(big.last_seen)
        self.assertLessEqual(big.first_seen, big.last_seen)


# ======================================================================
# §58.14, §58.15, §38, §71, §72 — point in time
# ======================================================================

class TestPointInTimeMemory(MemoryCase):

    def populate(self):
        for day in range(1, 31):
            self.add_experience(f"exp-day-{day}", day=day)

    def test_an_earlier_query_sees_strictly_less(self):
        self.populate()
        early = retrieval.memory_as_of(
            self.conn, (BASE + timedelta(days=10)).isoformat())
        late = retrieval.memory_as_of(
            self.conn, (BASE + timedelta(days=25)).isoformat())
        current = retrieval.memory_as_of(self.conn)
        self.assertLess(early["experience_count"], late["experience_count"])
        self.assertLess(late["experience_count"], current["experience_count"])

    def test_no_experience_from_after_the_cut_is_returned(self):
        """§39, §72: the future must be absent, not merely down-weighted."""
        self.populate()
        cut = (BASE + timedelta(days=10)).isoformat()
        rows = pattern_layer.load_experiences(self.conn, as_of=cut)
        for row in rows:
            self.assertLessEqual(row["available_at"], cut)

    def test_patterns_are_recomputed_not_read_from_storage(self):
        """
        A stored pattern was aggregated over the whole record and
        carries later evidence inside its averages. Serving it for a
        past date would leak in the most invisible way possible.

        Asserted on the SAMPLE SIZE rather than the pattern count. This
        fixture holds every condition constant, so the same cohorts
        exist at every date and only their sizes differ — the first
        version of this test compared counts and passed 9 == 9 for the
        wrong reason.
        """
        self.populate()
        found, evidence = pattern_layer.build_all(self.conn)
        pattern_layer.save(self.conn, found, evidence)
        stored_total = sum(row[0] for row in self.conn.execute(
            "SELECT sample_size FROM memory_patterns"))

        early = retrieval.memory_as_of(
            self.conn, (BASE + timedelta(days=8)).isoformat())
        early_total = sum(p.sample_size for p in early["patterns"])

        self.assertLess(early_total, stored_total,
                        "the as-of view aggregated evidence that did not "
                        "exist yet")
        self.assertEqual(early["experience_count"], 8)
        self.assertIn("recomputed", early["note"])

    def test_a_stored_pattern_is_larger_than_its_past_self(self):
        """The same cohort, seen at two dates, must not be the same size."""
        self.populate()
        early = retrieval.memory_as_of(
            self.conn, (BASE + timedelta(days=8)).isoformat())
        late = retrieval.memory_as_of(
            self.conn, (BASE + timedelta(days=28)).isoformat())
        biggest_early = max(p.sample_size for p in early["patterns"])
        biggest_late = max(p.sample_size for p in late["patterns"])
        self.assertLess(biggest_early, biggest_late)

    def test_an_experience_with_no_availability_never_enters_a_time_query(self):
        self.populate()
        self.conn.execute(
            "UPDATE trading_experiences SET available_at=NULL "
            "WHERE experience_id='exp-day-1'")
        self.conn.commit()
        rows = pattern_layer.load_experiences(
            self.conn, as_of=(BASE + timedelta(days=30)).isoformat())
        self.assertNotIn("exp-day-1", {r["experience_id"] for r in rows})

    def test_a_snapshot_records_only_what_was_knowable(self):
        self.populate()
        cut = (BASE + timedelta(days=10)).isoformat()
        api.write_snapshot(self.conn, cut)
        row = self.conn.execute("""
            SELECT experience_count FROM memory_snapshots WHERE as_of = ?
        """, (cut,)).fetchone()
        self.assertEqual(row[0], 10)

    def test_re_taking_a_snapshot_replaces_rather_than_duplicates(self):
        self.populate()
        cut = (BASE + timedelta(days=10)).isoformat()
        api.write_snapshot(self.conn, cut)
        api.write_snapshot(self.conn, cut)
        self.assertEqual(len(api.list_snapshots(self.conn)), 1)

    def test_a_later_experience_cannot_change_an_earlier_snapshot(self):
        """§39: newer evidence must not rewrite historical memory."""
        self.populate()
        cut = (BASE + timedelta(days=10)).isoformat()
        api.write_snapshot(self.conn, cut)
        before = self.conn.execute(
            "SELECT experience_count FROM memory_snapshots WHERE as_of=?",
            (cut,)).fetchone()[0]
        self.add_experience("exp-future", day=99)
        after = self.conn.execute(
            "SELECT experience_count FROM memory_snapshots WHERE as_of=?",
            (cut,)).fetchone()[0]
        self.assertEqual(before, after)

    def test_listing_experiences_honours_as_of(self):
        self.populate()
        cut = (BASE + timedelta(days=10)).isoformat()
        rows = api.list_experiences(self.conn, as_of=cut, limit=1000)
        self.assertEqual(len(rows), 10)


# ======================================================================
# §58.19, §58.20, §40-§43 — retrieval
# ======================================================================

class TestRetrieval(MemoryCase):

    def test_a_similarity_query_reports_its_sample_and_limitations(self):
        self.seed(40, tag="a", event="earnings", direction="long")
        response = retrieval.similar_experiences(
            self.conn, {"expected_direction": "long", "horizon": "5d",
                        "event_type": "earnings"})
        self.assertGreater(response.sample_size, 0)
        self.assertTrue(response.limitations)
        self.assertTrue(response.summary)

    def test_relaxation_is_recorded_when_a_query_is_too_specific(self):
        """
        "20 similar experiences" means something different when
        similarity was reduced to "any short signal".
        """
        self.seed(40, tag="a", event="earnings", instrument="i-1")
        response = retrieval.similar_experiences(
            self.conn, {"expected_direction": "long", "horizon": "5d",
                        "event_type": "earnings", "asset_class": "stock",
                        "instrument_id": "i-does-not-exist"})
        self.assertIn("instrument_id", response.relaxed_dimensions)

    def test_an_empty_result_says_so_rather_than_inventing_one(self):
        response = retrieval.similar_experiences(
            self.conn, {"expected_direction": "long"})
        self.assertEqual(response.sample_size, 0)
        self.assertIn("No comparable experience", response.summary)

    def test_a_small_result_refuses_to_describe_a_regularity(self):
        self.seed(3, tag="tiny")
        response = retrieval.similar_experiences(
            self.conn, {"expected_direction": "long", "horizon": "5d"})
        self.assertIn("Too few", response.summary)

    def test_the_summary_never_promises_the_future(self):
        """§43, §65: no unsupported claims."""
        self.seed(40, tag="a")
        response = retrieval.similar_experiences(
            self.conn, {"expected_direction": "long", "horizon": "5d"})
        self.assertIn("not what will happen", response.summary)

    def test_model_memory_states_it_is_not_a_verdict(self):
        """§16: do not declare a model good or bad from one sample."""
        self.seed(40, tag="a", model="tm-1")
        response = retrieval.model_memory(self.conn, "tm-1")
        self.assertTrue(any("not a verdict" in limit
                            for limit in response.limitations))

    def test_event_memory_disclaims_causation(self):
        self.seed(40, tag="a", event="earnings")
        response = retrieval.event_memory(self.conn, "earnings")
        self.assertTrue(any("caused" in limit for limit in response.limitations))

    def test_regime_memory_explains_why_it_is_empty(self):
        response = retrieval.regime_memory(self.conn, "high_vol")
        self.assertEqual(response.sample_size, 0)
        self.assertTrue(any("empty by" in limit for limit in response.limitations))

    def test_risk_memory_labels_neither_side_a_mistake(self):
        """§21."""
        self.seed(10, tag="a")
        result = retrieval.risk_memory(self.conn)
        self.assertIn("neither is labelled a mistake", result["note"])

    def test_execution_and_portfolio_memory_are_defined_and_empty(self):
        """§20, §22: the shape exists so the first real one has a home."""
        execution = retrieval.execution_memory(self.conn)
        portfolio = retrieval.portfolio_memory(self.conn)
        self.assertFalse(execution["available"])
        self.assertTrue(execution["missing_tables"])
        self.assertEqual(execution["records"], [])
        self.assertFalse(portfolio["available"])

    def test_a_response_carries_the_ids_that_support_it(self):
        """§43: provenance is retained."""
        self.seed(40, tag="a")
        response = retrieval.similar_experiences(
            self.conn, {"expected_direction": "long", "horizon": "5d"})
        self.assertTrue(response.as_dict()["supporting_experience_ids"])


class TestApiAndExport(MemoryCase):

    def test_pattern_detail_always_carries_its_evidence(self):
        self.seed(40, tag="a")
        found, evidence = pattern_layer.build_all(self.conn)
        pattern_layer.save(self.conn, found, evidence)
        biggest = max(found, key=lambda p: p.sample_size)
        detail = api.pattern_detail(self.conn, biggest.pattern_id)
        self.assertTrue(detail["evidence"])
        self.assertEqual(detail["evidence_total"], biggest.sample_size)

    def test_experience_detail_shows_which_patterns_it_supports(self):
        self.seed(40, tag="a")
        found, evidence = pattern_layer.build_all(self.conn)
        pattern_layer.save(self.conn, found, evidence)
        detail = api.experience_detail(self.conn, "exp-a-0")
        self.assertTrue(detail["supports_patterns"])

    def test_the_timeline_is_built_from_availability(self):
        for day in range(1, 6):
            self.add_experience(f"exp-{day}", day=day)
        timeline = api.timeline(self.conn)
        self.assertEqual(len(timeline), 5)
        self.assertEqual(timeline[-1]["cumulative"], 5)

    def test_exports_write_files_with_headers(self):
        import tempfile
        self.seed(10, tag="a")
        found, evidence = pattern_layer.build_all(self.conn)
        pattern_layer.save(self.conn, found, evidence)
        with tempfile.TemporaryDirectory() as directory:
            experiences = api.export_experiences_csv(
                self.conn, os.path.join(directory, "e.csv"))
            written = api.export_patterns_csv(
                self.conn, os.path.join(directory, "p.csv"))
            links = api.export_pattern_evidence_csv(
                self.conn, os.path.join(directory, "l.csv"))
            api.export_json(self.conn, os.path.join(directory, "m.json"))
            self.assertEqual(experiences, 10)
            self.assertGreater(written, 0)
            self.assertGreater(links, 0)
            with open(os.path.join(directory, "m.json"), encoding="utf-8") as f:
                document = json.load(f)
            for key in ("memory_version", "context_schema_version",
                        "outcome_method_version", "attribution_method_version"):
                self.assertIn(key, document)

    def test_the_integrity_check_passes_on_a_clean_build(self):
        self.seed(10, tag="a")
        found, evidence = pattern_layer.build_all(self.conn)
        pattern_layer.save(self.conn, found, evidence)
        for name, count in api.integrity_check(self.conn).items():
            if name == "experiences_without_an_outcome":
                continue   # the fixture writes experiences directly
            self.assertEqual(count, 0, name)

    def test_the_integrity_check_notices_a_pattern_without_evidence(self):
        self.seed(10, tag="a")
        found, evidence = pattern_layer.build_all(self.conn)
        pattern_layer.save(self.conn, found, evidence)
        self.conn.execute("DELETE FROM memory_pattern_evidence")
        self.conn.commit()
        self.assertGreater(
            api.integrity_check(self.conn)["patterns_without_evidence"], 0)


# ======================================================================
# §59 — adversarial, and §63 — memory changes nothing
# ======================================================================

def package_sources():
    for name in sorted(os.listdir(PACKAGE)):
        if name.endswith(".py"):
            with open(os.path.join(PACKAGE, name), encoding="utf-8") as handle:
                yield name, handle.read()


def executed_sql(source: str):
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", None) not in (
                "execute", "executemany", "executescript"):
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                yield argument.value
            elif isinstance(argument, ast.JoinedStr):
                for piece in argument.values:
                    if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                        yield piece.value
            elif isinstance(argument, ast.Call):
                for inner in ast.walk(argument):
                    if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                        yield inner.value


class TestMemoryChangesNothing(unittest.TestCase):
    """§63: memory must not automatically change anything."""

    def test_no_module_writes_outside_the_memory_tables(self):
        offenders = []
        for name, source in package_sources():
            for text in executed_sql(source):
                upper = " ".join(text.upper().split())
                if not any(verb in upper for verb in
                           ("INSERT", "UPDATE", "DELETE", "REPLACE", "DROP", "ALTER")):
                    continue
                for table in FORBIDDEN:
                    if table.upper() in upper:
                        offenders.append(f"{name}: writes {table}")
        self.assertEqual(offenders, [],
                         "memory can modify the record it remembers")

    def test_every_write_targets_a_memory_table(self):
        found = set()
        for _, source in package_sources():
            for text in executed_sql(source):
                upper = " ".join(text.upper().split())
                for verb in ("INSERT OR REPLACE INTO", "INSERT INTO",
                             "UPDATE", "DELETE FROM"):
                    if verb in upper:
                        tail = upper.split(verb, 1)[1].strip()
                        found.add(tail.split()[0].strip("( ").lower())
        self.assertTrue(found, "no writes found — the parser is broken")
        self.assertTrue(found <= WRITABLE, f"writes {found - WRITABLE}")

    def test_no_module_imports_a_decision_making_engine(self):
        forbidden = ("src.modeling.engine", "src.modeling.inference",
                     "src.modeling.promotion", "src.features.engine",
                     "src.signals.engine", "src.execution", "src.risk")
        for name, source in package_sources():
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith(("import ", "from ")):
                    for module in forbidden:
                        self.assertNotIn(module, stripped, f"{name}: {stripped}")

    def test_nothing_promotes_trains_or_adjusts_a_threshold(self):
        for name, source in package_sources():
            lowered = source.lower()
            for word in ("def promote", "promote(", "def train", ".fit(",
                         "update signals", "update trained_models"):
                self.assertNotIn(word, lowered, f"{name} contains {word!r}")

    def test_no_llm_is_used_anywhere_in_the_package(self):
        """§66: LLM use is optional, and this package uses none."""
        for name, source in package_sources():
            lowered = source.lower()
            for word in ("openai", "anthropic", "llm(", "completion(",
                         "chat.completions"):
                self.assertNotIn(word, lowered, f"{name} contains {word!r}")

    def test_no_earlier_pipeline_script_reads_the_memory_tables(self):
        scripts = os.path.join(ROOT, "scripts")
        offenders = []
        for name in sorted(os.listdir(scripts)):
            if not name.endswith(".py") or name == "build_memory.py":
                continue
            with open(os.path.join(scripts, name), encoding="utf-8") as handle:
                body = handle.read()
            if "trading_experiences" in body or "memory_patterns" in body:
                offenders.append(name)
        self.assertEqual(offenders, [])

    def test_memory_runs_after_attribution_in_the_pipeline(self):
        path = os.path.join(ROOT, ".github", "workflows", "pipeline.yml")
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("build_memory.py", body)
        self.assertLess(body.index("attribute_errors.py"),
                        body.index("build_memory.py"))

    def test_building_memory_does_not_modify_its_sources(self):
        conn = sqlite3.connect(":memory:")
        initialize_outcome_schema(conn)
        initialize_attribution_schema(conn)
        initialize_memory_schema(conn)
        conn.execute("""
            INSERT INTO outcome_measurements (
                subject_kind, subject_id, horizon, method_version,
                horizon_value, horizon_unit, status, reference_rule,
                information_cutoff, window_end, direction_result,
                expected_direction, simple_return, computed_at
            ) VALUES ('signal','s','5d','v1',5.0,'d','available',
                      'first_close_at_or_after_cutoff','2026-08-01T00:00:00+00:00',
                      '2026-08-06T00:00:00+00:00','miss','long',-0.02,
                      '2026-09-05T00:00:00+00:00')
        """)
        conn.commit()
        before = conn.execute("SELECT * FROM outcome_measurements").fetchall()
        from src.memory.experience import build_all, save as save_experiences
        save_experiences(conn, build_all(conn))
        self.assertEqual(conn.execute(
            "SELECT * FROM outcome_measurements").fetchall(), before)
        conn.close()


class TestAdversarial(MemoryCase):
    """§59, the remaining entries."""

    def test_a_duplicate_experience_cannot_be_created(self):
        for _ in range(3):
            self.add_experience("exp-1", day=1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM trading_experiences").fetchone()[0], 1)

    def test_a_duplicate_pattern_cannot_be_created(self):
        self.seed(40, tag="a")
        found, evidence = pattern_layer.build_all(self.conn)
        pattern_layer.save(self.conn, found, evidence)
        first = self.conn.execute(
            "SELECT COUNT(*) FROM memory_patterns").fetchone()[0]
        found, evidence = pattern_layer.build_all(self.conn)
        pattern_layer.save(self.conn, found, evidence)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM memory_patterns").fetchone()[0], first)

    def test_contradictory_experiences_are_not_averaged_into_certainty(self):
        self.seed(40, tag="lo", regime="low_vol", result="hit", actual=0.03)
        self.seed(40, tag="hi", regime="high_vol", result="miss", actual=-0.03)
        found, _ = pattern_layer.build_all(self.conn)
        conflicting = [p for p in found
                       if p.quality == PatternQuality.CONFLICTING]
        self.assertTrue(conflicting)
        for pattern in conflicting:
            self.assertEqual(pattern.confidence,
                             MemoryConfidence.CONFLICTING_EVIDENCE)
            self.assertIn("conflicts", pattern.describe())

    def test_an_experimental_experience_is_never_marked_validated(self):
        self.seed(20, tag="e", quality="experimental")
        rows = pattern_layer.load_experiences(self.conn, qualities=("validated",))
        self.assertEqual(rows, [])

    def test_a_stale_pattern_is_labelled_rather_than_deleted(self):
        """§49: historical existence and current relevance differ."""
        for index in range(40):
            self.add_experience(f"exp-old-{index}", day=index % 10)
        found, _ = pattern_layer.build_all(
            self.conn, now=BASE + timedelta(days=400))
        stale = [p for p in found if p.quality == PatternQuality.STALE]
        self.assertTrue(stale)
        self.assertTrue(any("not deleted" in note for note in stale[0].notes))

    def test_deleting_evidence_is_visible_rather_than_silent(self):
        self.seed(40, tag="a")
        found, evidence = pattern_layer.build_all(self.conn)
        pattern_layer.save(self.conn, found, evidence)
        self.conn.execute("DELETE FROM trading_experiences WHERE experience_id='exp-a-0'")
        self.conn.commit()
        self.assertGreater(
            api.integrity_check(self.conn)["evidence_without_an_experience"], 0)

    def test_a_memory_version_change_does_not_rewrite_the_old_one(self):
        self.seed(10, tag="a")
        found, evidence = pattern_layer.build_all(self.conn)
        pattern_layer.save(self.conn, found, evidence)
        before = self.conn.execute("""
            SELECT pattern_id, sample_size, quality FROM memory_patterns
            WHERE memory_version='v1' ORDER BY pattern_id
        """).fetchall()
        found2, evidence2 = pattern_layer.build_all(self.conn,
                                                    memory_version="v2")
        pattern_layer.save(self.conn, found2, evidence2, memory_version="v2")
        after = self.conn.execute("""
            SELECT pattern_id, sample_size, quality FROM memory_patterns
            WHERE memory_version='v1' ORDER BY pattern_id
        """).fetchall()
        self.assertEqual(before, after)

    def test_retrieval_never_returns_a_conclusion_without_a_sample_size(self):
        self.seed(40, tag="a")
        response = retrieval.similar_experiences(
            self.conn, {"expected_direction": "long"})
        payload = response.as_dict()
        self.assertIn("sample_size", payload)
        self.assertIn("limitations", payload)
        self.assertTrue(payload["limitations"])


if __name__ == "__main__":
    unittest.main()
