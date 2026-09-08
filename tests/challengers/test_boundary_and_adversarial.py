"""
tests/challengers/test_boundary_and_adversarial.py
------------------------------------------------------------
Phase 24 §70, §78, §82 — what the challenger system structurally cannot
do, and the sixteen ways it could fool its owner.

WHY THE BOUNDARY TEST COUNTS ROWS
-------------------------------------
Phase 23.5 found an AST scan that could not see what it certified: it
read SQL literals inside one package, so writes performed by calling
into another package were invisible, and a report quoted it as proof.

So the boundary here is measured the way that audit ended up measuring
it — count every row in every table before and after a full challenger
cycle, and assert only research-scope tables moved. Transitive calls
are covered because rows are counted, not source parsed.

The source scans are still present, for the things rows cannot show:
that no live order path exists, that no arbitrary code can be executed,
and that no second broker is named outside a prohibition.
"""

import ast
import io
import json
import os
import re
import sqlite3
import sys
import tokenize
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.challengers import api, evaluation, registry, workflow
from src.domain.challenger_models import (
    ChallengerDecision, ChallengerLimits, ChallengerStatus, ReviewOutcome,
    SliceResult, build_scorecard, decide, EvaluationPlan,
)
from tests.challengers.test_challenger_lifecycle import (
    a_candidate, a_challenger, a_database, a_result,
)

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
PACKAGE = os.path.join(ROOT, "src", "challengers")

#: The tables a challenger cycle is allowed to move. Phase 22's
#: experiment tables are not here: the challenger evaluator calls the
#: Phase 22 EVALUATORS directly rather than creating experiments, so a
#: cycle should touch none of them.
WRITABLE = {
    "challengers", "challenger_runs", "challenger_results",
    "challenger_reviews", "challenger_queue", "challenger_audit",
    # Phase 23's snooping ledger: every evaluation records the window it
    # used, which is the whole point of the ledger (§23 of Phase 23.5).
    "autoresearch_window_usage",
}

FORBIDDEN = {
    "trained_models", "model_evaluations", "model_promotions", "predictions",
    "signals", "signal_contributions", "signal_strategies",
    "signal_suppressions", "research_features", "research_labels",
    "research_observations", "outcome_measurements", "outcome_aggregates",
    "error_attributions", "attribution_evidence", "trading_experiences",
    "memory_patterns", "portfolio_snapshots", "orders", "executions",
    "risk_limits", "recommendations",
}


def package_sources():
    for name in sorted(os.listdir(PACKAGE)):
        if name.endswith(".py"):
            with open(os.path.join(PACKAGE, name), encoding="utf-8") as handle:
                yield name, handle.read()


def code_only(source):
    """
    Source with comments and string literals removed.

    Needed because this package's own docstrings discuss the very words
    the scans search for while explaining why it does not use them.
    Matching prose made Phase 23's safety tests fail on their own
    documentation, and a scanner that cries wolf gets switched off.
    """
    kept = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            kept.append(token.string)
    except (tokenize.TokenError, IndentationError):
        return source
    return " ".join(kept)


def executed_sql(source):
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
            elif isinstance(argument, (ast.BinOp, ast.Call)):
                for inner in ast.walk(argument):
                    if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                        yield inner.value


def write_target(sql):
    """
    The table a statement WRITES, or None if it only reads.

    Word boundaries, not substrings. Phase 23.5's first version read a
    SELECT listing `updated_at` as an UPDATE and one listing
    `experiments_run` as writing the experiments table — false alarms
    against entirely correct source.
    """
    text = " ".join(sql.upper().split())
    match = re.search(
        r"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM|"
        r"DROP\s+TABLE|ALTER\s+TABLE)\s+([A-Z_][A-Z0-9_]*)", text)
    return match.group(1).lower() if match else None


# ======================================================================
# §36, §70 — the production boundary
# ======================================================================

class TestProductionBoundary(unittest.TestCase):

    def test_a_full_cycle_changes_no_table_outside_research_scope(self):
        """
        Measured by counting rows, so transitive writes are covered.
        An AST scan cannot see a write performed by calling into
        another package — the Phase 23.5 lesson.
        """
        conn = a_database()
        challenger = a_challenger(conn)
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]

        def snapshot():
            return {name: conn.execute("SELECT COUNT(*) FROM %s" % name
                                       ).fetchone()[0] for name in tables}

        before = snapshot()
        workflow.enqueue(conn, challenger.challenger_id, challenger.version)
        workflow.run_queued(conn)
        workflow.review(conn, challenger.challenger_id, challenger.version,
                        outcome=ReviewOutcome.APPROVED_FOR_PAPER,
                        reviewer="a.person", reason="worth paper")
        after = snapshot()

        moved = {name for name in tables if before[name] != after[name]}
        self.assertTrue(
            moved <= WRITABLE,
            "a challenger cycle changed %s, outside research scope"
            % sorted(moved - WRITABLE))
        conn.close()

    def test_the_row_counting_method_can_detect_a_write(self):
        """A boundary test that observes nothing passes forever."""
        conn = a_database()
        before = conn.execute(
            "SELECT COUNT(*) FROM trading_experiences").fetchone()[0]
        conn.execute("""
            INSERT INTO trading_experiences (
                experience_id, memory_version, kind, subject_kind, subject_id,
                horizon, quality, experience_class, created_at
            ) VALUES ('probe','v1','signal','signal','s','5d','validated',
                      'correct_call','2026-09-07')
        """)
        conn.commit()
        after = conn.execute(
            "SELECT COUNT(*) FROM trading_experiences").fetchone()[0]
        self.assertEqual(after, before + 1)
        conn.close()

    def test_no_module_writes_a_production_table(self):
        offenders = []
        for name, source in package_sources():
            for text in executed_sql(source):
                target = write_target(text)
                if target and target in FORBIDDEN:
                    offenders.append("%s writes %s" % (name, target))
        self.assertEqual(offenders, [])

    def test_every_write_targets_a_challenger_table(self):
        found = set()
        for _name, source in package_sources():
            for text in executed_sql(source):
                target = write_target(text)
                if target:
                    found.add(target)
        self.assertTrue(found, "no writes found - the parser is broken")
        self.assertTrue(found <= WRITABLE, "writes %s" % (found - WRITABLE,))

    def test_no_module_imports_a_production_engine(self):
        forbidden = ("src.modeling.promotion", "src.execution", "src.risk",
                     "src.portfolio.rebalance")
        for name, source in package_sources():
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith(("import ", "from ")):
                    for module in forbidden:
                        self.assertNotIn(module, stripped,
                                         "%s: %s" % (name, stripped))

    def test_promotion_to_production_always_refuses(self):
        """§35: the refusal is findable on purpose."""
        conn = a_database()
        with self.assertRaises(workflow.PromotionRefused):
            workflow.promote_to_production(conn, "chl-anything")
        conn.close()

    def test_no_production_status_exists_in_the_vocabulary(self):
        values = {status.value for status in ChallengerStatus}
        self.assertFalse(values & {"production", "active", "promoted",
                                   "deployed", "live"})

    def test_no_review_outcome_approves_production(self):
        values = {outcome.value for outcome in ReviewOutcome}
        self.assertEqual(values, {"approved_for_paper", "rejected", "deferred"})


# ======================================================================
# §61, §70 — execution safety
# ======================================================================

class TestExecutionSafety(unittest.TestCase):

    def test_no_live_order_path_exists(self):
        for name, source in package_sources():
            lowered = code_only(source).lower()
            for word in ("ib_insync", "ibapi", "placeorder", "submit_order",
                         "live_trading", "enable_live", "reqaccountsummary"):
                self.assertNotIn(word, lowered, "%s contains %r" % (name, word))

    def test_no_broker_other_than_ibkr_is_referenced(self):
        for name, source in package_sources():
            lowered = code_only(source).lower()
            for word in ("metatrader", "mt5", "alpaca", "oanda", "binance"):
                self.assertNotIn(word, lowered, "%s contains %r" % (name, word))

    def test_no_arbitrary_code_execution(self):
        """§72: components are registered names, never generated code."""
        for name, source in package_sources():
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Call):
                    called = getattr(node.func, "id", None)
                    self.assertNotIn(called, ("eval", "exec", "compile",
                                              "__import__"),
                                     "%s calls %s" % (name, called))
                    attribute = getattr(node.func, "attr", None)
                    self.assertNotIn(attribute, ("import_module", "system",
                                                 "popen", "check_output"),
                                     "%s calls %s" % (name, attribute))

    def test_no_credentials_are_read(self):
        for name, source in package_sources():
            lowered = code_only(source).lower()
            # Precise tokens, not substrings. A bare "environ" matches
            # the legitimate identifier `RunEnvironment`, and a scan
            # that fires on correct code is one people switch off --
            # the same false-positive trap Phase 19 hit with its
            # leakage detector and Phase 23.5 hit with its SQL parser.
            for word in ("api_key", "api_secret", "password",
                         "os . environ", "os . getenv", "getenv (",
                         "account_number"):
                self.assertNotIn(word, lowered, "%s contains %r" % (name, word))

    def test_the_paper_environment_is_never_the_default(self):
        """§31: environments are never confused or silently widened."""
        conn = a_database()
        challenger = a_challenger(conn)
        run, _result = evaluation.evaluate(conn, challenger)
        self.assertEqual(run["environment"], "research")
        conn.close()

    def test_no_llm_is_required_or_used(self):
        """§71."""
        for name, source in package_sources():
            lowered = code_only(source).lower()
            for word in ("openai", "anthropic", "chat . completions"):
                self.assertNotIn(word, lowered, "%s contains %r" % (name, word))


# ======================================================================
# §78 — the adversarial list
# ======================================================================

class TestAdversarial(unittest.TestCase):

    def setUp(self):
        self.conn = a_database()

    def tearDown(self):
        self.conn.close()

    def test_challenger_sees_future_data(self):
        """
        The cohort is loaded through Phase 22's `load_cohort`, which
        filters on Phase 21's `available_at` — when a row became
        KNOWABLE, not when it was written.
        """
        challenger = a_challenger(self.conn)
        rows = evaluation.load_cohort(self.conn, challenger)
        stamps = [r["available_at"] for r in rows]
        self.assertEqual(stamps, sorted(stamps))
        self.assertTrue(all(s <= challenger.dataset_cutoff for s in stamps))

    def test_challenger_uses_a_protected_test_repeatedly(self):
        from src.autoresearch import governance
        governance.declare_window(self.conn, label="final",
                                  starts_at="2020-01-01", ends_at="2099-01-01")
        challenger = a_challenger(self.conn)
        _run, result = evaluation.evaluate(self.conn, challenger,
                                           allow_cache=False)
        self.assertIsNone(result)

    def test_challenger_changes_after_evaluation_starts(self):
        challenger = a_challenger(self.conn)
        registry.set_status(self.conn, challenger.challenger_id,
                            challenger.version, ChallengerStatus.VALIDATING)
        challenger.change.parameters["horizon"] = "10d"
        with self.assertRaises(evaluation.EvaluationRefused):
            evaluation.evaluate(self.conn, challenger)

    def test_a_candidate_cannot_silently_become_production(self):
        self.assertEqual(
            api.integrity_check(self.conn)["challengers_in_a_production_state"],
            0)

    def test_a_challenger_cannot_silently_become_production(self):
        challenger = a_challenger(self.conn)
        workflow.enqueue(self.conn, challenger.challenger_id,
                         challenger.version)
        workflow.run_queued(self.conn)
        row = api.challengers(self.conn)[0]
        self.assertIn(row["status"],
                      {s.value for s in ChallengerStatus})
        self.assertNotIn(row["status"], {"production", "active", "promoted"})

    def test_a_failed_challenger_is_not_deleted(self):
        for _name, source in package_sources():
            for text in executed_sql(source):
                target = write_target(text)
                if target and text.strip().upper().startswith("DELETE"):
                    self.fail("%s deletes from %s" % (_name, target))

    def test_a_winner_cannot_be_selected_without_multiple_testing_context(self):
        """§15, §24: the family count travels with every verdict."""
        challenger = a_challenger(self.conn)
        _run, result = evaluation.evaluate(self.conn, challenger)
        self.assertGreaterEqual(result.family_challenger_count, 1)
        self.assertTrue(any("family" in text.lower()
                            for text in result.limitations)
                        or result.family_challenger_count == 1)

    def test_one_regime_cannot_produce_a_false_universal_winner(self):
        """
        `market_regime` is NULL throughout this database, so every
        result carries `single_regime` and the evidence dimension can
        never read "better" on that basis alone.
        """
        challenger = a_challenger(self.conn)
        _run, result = evaluation.evaluate(self.conn, challenger)
        self.assertIn("single_regime", result.warnings)

    def test_one_instrument_cannot_produce_a_false_universal_winner(self):
        result = a_result(
            challenger_out_of_sample={"sample_size": 200,
                                      "instrument_count": 1})
        from src.domain.autoresearch_models import overfitting_warnings
        self.assertIn("single_instrument",
                      overfitting_warnings(effect=0.1, effect_in_sample=0.1,
                                           instrument_count=1))

    def test_a_tiny_sample_cannot_become_a_winner(self):
        plan = EvaluationPlan()
        result = a_result(effect=0.5,
                          challenger_out_of_sample={"sample_size": 4})
        result.scorecard = build_scorecard(result, plan)
        verdict, _reasons = decide(result, plan)
        self.assertEqual(verdict, ChallengerDecision.INCONCLUSIVE)

    def test_the_cache_cannot_hide_new_data(self):
        """The Phase 23.5 defect: the cutoff is inside the fingerprint."""
        challenger = a_challenger(self.conn)
        before = challenger.fingerprint
        challenger.dataset_cutoff = "2099-01-01T00:00:00+00:00"
        self.assertNotEqual(before, challenger.fingerprint)

    def test_a_duplicate_run_is_prevented_by_the_claim(self):
        challenger = a_challenger(self.conn)
        queue_id = workflow.enqueue(self.conn, challenger.challenger_id,
                                    challenger.version)
        self.assertTrue(workflow.claim(self.conn, queue_id))
        self.assertFalse(workflow.claim(self.conn, queue_id))

    def test_a_concurrent_queue_race_has_one_winner(self):
        challenger = a_challenger(self.conn)
        queue_id = workflow.enqueue(self.conn, challenger.challenger_id,
                                    challenger.version)
        claims = [workflow.claim(self.conn, queue_id) for _ in range(5)]
        self.assertEqual(sum(1 for c in claims if c), 1)

    def test_a_worker_crash_is_recovered(self):
        challenger = a_challenger(self.conn)
        queue_id = workflow.enqueue(self.conn, challenger.challenger_id,
                                    challenger.version)
        workflow.claim(self.conn, queue_id)
        self.conn.execute(
            "UPDATE challenger_queue SET started_at=? WHERE queue_id=?",
            ("2020-01-01T00:00:00+00:00", queue_id))
        self.conn.commit()
        self.assertEqual(len(workflow.reclaim_stale(self.conn)), 1)

    def test_an_evaluation_that_produced_nothing_is_not_a_verdict(self):
        """
        The Phase 23 lesson: an absent result interpreted as a
        measurement reads as "we tested and could not tell".
        """
        from src.autoresearch import governance
        governance.declare_window(self.conn, label="all",
                                  starts_at="2020-01-01", ends_at="2099-01-01")
        challenger = a_challenger(self.conn)
        workflow.enqueue(self.conn, challenger.challenger_id,
                         challenger.version)
        report = workflow.run_queued(self.conn)
        self.assertEqual(report["evaluated"], [])
        self.assertTrue(report["failed"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM challenger_results"
                              ).fetchone()[0], 0)

    def test_a_paper_candidate_requires_a_review(self):
        """§34: no automatic promotion, even to paper."""
        challenger = a_challenger(self.conn)
        workflow.enqueue(self.conn, challenger.challenger_id,
                         challenger.version)
        workflow.run_queued(self.conn)
        row = api.challengers(self.conn)[0]
        self.assertNotEqual(row["status"], "paper_candidate")
        self.assertEqual(
            api.integrity_check(self.conn)["paper_candidates_without_a_review"],
            0)


# ======================================================================
# §82 — the final audit, as queries
# ======================================================================

class TestIntegrity(unittest.TestCase):

    def test_every_count_is_zero_after_a_full_cycle(self):
        conn = a_database()
        challenger = a_challenger(conn)
        workflow.enqueue(conn, challenger.challenger_id, challenger.version)
        workflow.run_queued(conn)
        workflow.review(conn, challenger.challenger_id, challenger.version,
                        outcome=ReviewOutcome.APPROVED_FOR_PAPER,
                        reviewer="a.person", reason="approved for paper")
        failures = {key: value
                    for key, value in api.integrity_check(conn).items()
                    if value}
        self.assertEqual(failures, {})
        conn.close()

    def test_every_action_is_attributed_to_an_actor(self):
        conn = a_database()
        challenger = a_challenger(conn)
        workflow.enqueue(conn, challenger.challenger_id, challenger.version)
        workflow.run_queued(conn)
        for row in workflow.trail(conn):
            self.assertIn(row["actor"], ("human", "system"))
        conn.close()

    def test_a_review_is_recorded_as_a_human_action(self):
        """§53 of Phase 23.5: never record a human act as the system's."""
        conn = a_database()
        challenger = a_challenger(conn)
        workflow.enqueue(conn, challenger.challenger_id, challenger.version)
        workflow.run_queued(conn)
        workflow.review(conn, challenger.challenger_id, challenger.version,
                        outcome=ReviewOutcome.DEFERRED,
                        reviewer="a.person", reason="thinking about it")
        reviews = [row for row in workflow.trail(conn)
                   if row["action"] == "review"]
        self.assertTrue(reviews)
        self.assertEqual(reviews[0]["actor"], "human")
        conn.close()


if __name__ == "__main__":
    unittest.main()
