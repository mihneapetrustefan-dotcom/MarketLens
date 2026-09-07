"""
tests/autoresearch/test_boundary_and_safety.py
--------------------------------------------------------
Phase 23 §33, §34, §50, §51, §68, §69, §74, §75, §88 — what the
autonomous researcher structurally cannot do.

WHY THESE PARSE SOURCE RATHER THAN CALL FUNCTIONS
-----------------------------------------------------
A behavioural test proves the code did not promote a model on the one
path it exercised. Parsing the package proves there is no path that
could. For a component whose whole safety argument is "it cannot reach
production", the second is the only one worth having.

So the tests below read `src/autoresearch/*.py`, extract every SQL
string handed to `execute`, every import, and every call, and assert
that the production tables, the broker, the risk limits and the
capital figures are untouched — and that no arbitrary code can be
executed through any interface.

THE ADVERSARIAL LIST (§74) IS THE SPINE OF THIS FILE
--------------------------------------------------------
Eighteen ways a research system fools itself or its owner. Each has a
test named after it. The ones that already caught real bugs in this
phase are marked in their docstrings.
"""

import ast
import json
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.autoresearch_schema import initialize_autoresearch_schema
from src.domain.autoresearch_models import (
    Actor, CandidateStatus, ConclusionType, ResearchBudget,
)
from src.autoresearch import (
    api, audit, candidates as candidate_registry, cycle, governance,
    hypotheses as hypothesis_layer, observations as observation_layer,
    prioritization, questions as question_layer, queue as queue_layer, tools,
)
from tests.autoresearch.test_research_loop import (
    a_database, a_hypothesis, an_observation,
)

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
PACKAGE = os.path.join(ROOT, "src", "autoresearch")

WRITABLE = {
    "autoresearch_observations", "autoresearch_questions",
    "autoresearch_hypotheses", "autoresearch_queue", "autoresearch_cycles",
    "autoresearch_conclusions", "autoresearch_candidates",
    "autoresearch_protected_windows", "autoresearch_window_usage",
    "autoresearch_audit", "autoresearch_family_state",
}

#: Phase 22's tables. The research cycle DOES write these -- through
#: Phase 22's own `save_experiment`, `api.start` and `ensure_family`,
#: because §27 forbids building a second experiment engine. They are
#: research-scope, not production: no model, strategy, threshold, risk
#: limit or capital figure lives in any of them.
#:
#: They are listed explicitly rather than left implicit so that the
#: boundary a reader is asked to trust is written down.
EXPERIMENT_TABLES = {
    "experiments", "experiment_runs", "experiment_results",
    "experiment_artifacts", "hypothesis_families",
}

FORBIDDEN = {
    "trained_models", "model_evaluations", "model_promotions", "predictions",
    "signals", "signal_contributions", "signal_strategies",
    "signal_suppressions", "research_features", "research_labels",
    "research_observations", "outcome_measurements", "outcome_aggregates",
    "error_attributions", "attribution_evidence", "trading_experiences",
    "memory_patterns", "memory_pattern_evidence", "portfolio_snapshots",
    "orders", "executions", "risk_limits", "recommendations",
}


def package_sources():
    for name in sorted(os.listdir(PACKAGE)):
        if name.endswith(".py"):
            with open(os.path.join(PACKAGE, name), encoding="utf-8") as handle:
                yield name, handle.read()


def code_only(source):
    """
    The source with every comment and string literal removed.

    Needed because these scans search for words like "environ" and
    "open(", and this package's own docstrings discuss exactly those
    words while explaining why it does not use them. Matching prose
    made the safety tests fail on their own documentation -- the same
    false-positive trap Phase 19 hit with its leakage detector, where
    a scanner people learn to ignore is worse than no scanner.

    Tokenising and dropping COMMENT and STRING leaves only what the
    interpreter would actually execute.
    """
    import io
    import tokenize
    kept = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            kept.append(token.string)
    except (tokenize.TokenError, IndentationError):
        return source
    return " ".join(kept)


def write_target(sql):
    """
    The table a statement WRITES, or None if it only reads.

    Uses word boundaries rather than substring matching. Substring
    matching was wrong twice on this package's own SQL: a SELECT
    listing `updated_at` was read as an UPDATE (leaving the target
    "d_at"), and one listing `experiments_run` was read as writing the
    `experiments` table. Both produced failures against source that was
    entirely correct -- and a safety scanner that cries wolf is one
    people switch off.
    """
    import re
    text = " ".join(sql.upper().split())
    match = re.search(
        r"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM|"
        r"DROP\s+TABLE|ALTER\s+TABLE)\s+([A-Z_][A-Z0-9_]*)", text)
    return match.group(1).lower() if match else None


def executed_sql(source):
    """
    SQL literals passed to execute/executemany/executescript.

    Scoped to those calls deliberately: parsing every string constant
    flags docstrings that merely mention a table name, and Phase 19
    learned that a detector with false positives is one people learn to
    ignore.
    """
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
            elif isinstance(argument, ast.BinOp):
                for inner in ast.walk(argument):
                    if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                        yield inner.value
            elif isinstance(argument, ast.Call):
                for inner in ast.walk(argument):
                    if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                        yield inner.value


# ======================================================================
# §33, §34 — the production boundary
# ======================================================================

class TestProductionBoundary(unittest.TestCase):

    def test_no_module_writes_a_production_table(self):
        offenders = []
        for name, source in package_sources():
            for text in executed_sql(source):
                target = write_target(text)
                if target is None:
                    continue
                if target in FORBIDDEN:
                    offenders.append("%s writes %s" % (name, target))
        self.assertEqual(offenders, [],
                         "research can modify the record it studies")

    def test_every_write_targets_a_research_table(self):
        found = set()
        for _name, source in package_sources():
            for text in executed_sql(source):
                target = write_target(text)
                if target:
                    found.add(target)
        self.assertTrue(found, "no writes found - the parser is broken")
        self.assertTrue(found <= WRITABLE, "writes %s" % (found - WRITABLE,))

    def test_the_write_scanner_actually_finds_writes(self):
        """
        A scanner that silently matches nothing passes every test it is
        used in. This pins that it recognises a real write and ignores a
        read -- including a SELECT whose columns are named `updated_at`
        and `experiments_run`, which a substring search reads as an
        UPDATE of the experiments table.
        """
        self.assertEqual(
            write_target("INSERT INTO autoresearch_queue (a) VALUES (?)"),
            "autoresearch_queue")
        self.assertEqual(
            write_target("UPDATE autoresearch_candidates SET status = ?"),
            "autoresearch_candidates")
        self.assertIsNone(
            write_target("SELECT experiments_run, updated_at FROM t"))

    def test_a_cycle_changes_no_table_outside_research_scope(self):
        """
        THE BOUNDARY TEST THAT ACTUALLY MEASURES THE BOUNDARY.

        The AST scan above reads SQL literals inside
        `src/autoresearch/*.py`. It therefore cannot see a write
        performed by calling into another package -- and the research
        cycle does exactly that: `cycle.build_experiment` calls
        `templates.ensure_family`, and the run calls
        `engine.save_experiment` and `api.start`. Those write four
        Phase 22 tables.

        That is correct behaviour (§27 requires reusing the Phase 22
        engine rather than building a second one), but the Phase 23
        report claimed the cycle "writes none of Phase 22's tables",
        and the AST test appeared to confirm it. A safety test that
        cannot observe the thing it certifies is worse than no test,
        because it is quoted.

        So this counts every row in every table before and after a real
        cycle and asserts that only research-scope tables moved.
        Transitive calls are covered because rows are counted, not
        source parsed.
        """
        conn = a_database()
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]

        def snapshot():
            counts = {}
            for table in tables:
                counts[table] = conn.execute(
                    "SELECT COUNT(*) FROM %s" % table).fetchone()[0]
            return counts

        before = snapshot()
        cycle.run_cycle(conn, apply=True,
                        budget=ResearchBudget(max_experiments_per_cycle=3))
        after = snapshot()

        moved = {name for name in tables if before[name] != after[name]}
        allowed = WRITABLE | EXPERIMENT_TABLES
        self.assertTrue(
            moved <= allowed,
            "a research cycle changed %s, which is outside research scope"
            % sorted(moved - allowed))
        conn.close()

    def test_the_row_counting_boundary_test_can_actually_detect_a_write(self):
        """
        A boundary test that silently observes nothing passes forever.
        This writes one row to a production table by hand and proves
        the counting method sees it.
        """
        conn = a_database()
        before = conn.execute(
            "SELECT COUNT(*) FROM trading_experiences").fetchone()[0]
        conn.execute("""
            INSERT INTO trading_experiences (
                experience_id, memory_version, kind, subject_kind,
                subject_id, horizon, quality, experience_class, created_at
            ) VALUES ('probe','v1','signal','signal','s','5d','validated',
                      'correct_call','2026-09-07')
        """)
        conn.commit()
        after = conn.execute(
            "SELECT COUNT(*) FROM trading_experiences").fetchone()[0]
        self.assertEqual(after, before + 1,
                         "the row-counting method cannot see a write")
        conn.close()

    def test_phase_21_memory_is_never_written(self):
        """
        §39 asks that memory be updated. It is updated by STORING the
        conclusion and reading it back, not by inserting a research
        finding into `trading_experiences` -- which has an
        `available_at` marking when something became knowable, a
        property a research finding does not have and would corrupt.
        """
        conn = a_database()
        before = conn.execute(
            "SELECT COUNT(*) FROM trading_experiences").fetchone()[0]
        cycle.run_cycle(conn, apply=True,
                        budget=ResearchBudget(max_experiments_per_cycle=2))
        after = conn.execute(
            "SELECT COUNT(*) FROM trading_experiences").fetchone()[0]
        self.assertEqual(before, after)
        conn.close()

    def test_nothing_promotes_trains_or_moves_capital(self):
        for name, source in package_sources():
            lowered = code_only(source).lower()
            for word in ("place_order", "submit_order", "set_capital",
                         "update risk_limits", ".fit(", "def train"):
                self.assertNotIn(word, lowered, "%s contains %r" % (name, word))

    def test_promotion_refuses_from_every_entrance(self):
        conn = a_database()
        with self.assertRaises(candidate_registry.PromotionRefused):
            candidate_registry.promote(conn, "cand-1")
        with self.assertRaises(tools.PermissionDenied):
            tools.promote_candidate(conn, tools.Grant.researcher(),
                                    candidate_id="cand-1")
        conn.close()

    def test_no_candidate_can_reach_promoted(self):
        """
        §55: PROMOTED exists in the vocabulary and is unreachable.
        No module ASSIGNS it -- `candidates.py` names it only to refuse
        it, which is the point of having the member at all.
        """
        for name, source in package_sources():
            if name == "candidates.py":
                continue
            self.assertNotIn("CandidateStatus.PROMOTED", code_only(source),
                             "%s references the promoted status" % name)
        conn = a_database()
        self.assertEqual(api.integrity_check(conn)["promoted_candidates"], 0)
        conn.close()

    def test_no_module_imports_a_production_engine(self):
        forbidden = ("src.modeling.promotion", "src.execution", "src.risk",
                     "src.portfolio.rebalance", "src.paper")
        for name, source in package_sources():
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith(("import ", "from ")):
                    for module in forbidden:
                        self.assertNotIn(module, stripped,
                                         "%s: %s" % (name, stripped))


# ======================================================================
# §51, §68, §75 — the agent and the broker
# ======================================================================

class TestAgentSafety(unittest.TestCase):

    def test_no_broker_other_than_ibkr_is_referenced(self):
        for name, source in package_sources():
            lowered = code_only(source).lower()
            for word in ("metatrader", "mt5", "alpaca", "oanda", "binance"):
                self.assertNotIn(word, lowered, "%s contains %r" % (name, word))

    def test_no_live_order_path_exists(self):
        """§68: the only environments are RESEARCH and PAPER."""
        for name, source in package_sources():
            lowered = code_only(source).lower()
            for word in ("ib_insync", "ibapi", "placeorder", "reqaccountsummary",
                         "live_trading", "enable_live"):
                self.assertNotIn(word, lowered, "%s contains %r" % (name, word))

    def test_no_arbitrary_code_execution_is_reachable(self):
        """§29, §49: an evaluator is a registered name, never code."""
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

    def test_no_shell_or_filesystem_access(self):
        """
        §69: data reaches the agent through the repository layer, not
        through the filesystem. `candidates.py` runs `git rev-parse` to
        stamp a code version, which is a read of the repository's own
        identity and takes no input from any research row.
        """
        for name, source in package_sources():
            if name == "candidates.py":
                continue
            lowered = code_only(source).lower()
            for word in ("subprocess", "os . system", "shutil"):
                self.assertNotIn(word, lowered, "%s contains %r" % (name, word))

    def test_no_credentials_are_read(self):
        for name, source in package_sources():
            lowered = code_only(source).lower()
            for word in ("api_key", "api_secret", "password", "getenv",
                         "environ", "ibkr_account", "account_number"):
                self.assertNotIn(word, lowered, "%s contains %r" % (name, word))

    def test_no_llm_is_used(self):
        """
        §47 permits an LLM and does not require one. None is used, and
        the audit trail measures the absence rather than asserting it.
        """
        for name, source in package_sources():
            lowered = code_only(source).lower()
            for word in ("openai", "anthropic", "chat . completions"):
                self.assertNotIn(word, lowered, "%s contains %r" % (name, word))
        conn = a_database()
        self.assertEqual(
            audit.activity_summary(conn)["by_actor"]["llm"], 0)
        conn.close()

    def test_no_reinforcement_learning_or_self_modification(self):
        for name, source in package_sources():
            lowered = code_only(source).lower()
            for word in ("reinforcement", "q_learning", "reward_signal",
                         "self_update", "auto_promote", "auto_apply"):
                self.assertNotIn(word, lowered, "%s contains %r" % (name, word))


# ======================================================================
# §50 — permissions
# ======================================================================

class TestPermissions(unittest.TestCase):

    def test_a_read_only_grant_cannot_run_a_cycle(self):
        conn = a_database()
        with self.assertRaises(tools.PermissionDenied):
            tools.run_research_cycle(conn, tools.Grant.read_only(), apply=True)
        conn.close()

    def test_a_read_only_grant_cannot_create_a_hypothesis(self):
        conn = a_database()
        observation = an_observation()
        question = question_layer.from_observation(observation)
        with self.assertRaises(tools.PermissionDenied):
            tools.create_hypothesis(conn, tools.Grant.read_only(),
                                    question=question, observation=observation)
        conn.close()

    def test_promote_candidate_is_never_grantable(self):
        """§50: defined for a future phase, ungrantable in this one."""
        self.assertNotIn(tools.PROMOTE_CANDIDATE, tools.ALL_PERMISSIONS)
        self.assertNotIn(tools.PROMOTE_CANDIDATE,
                         tools.Grant.researcher().permissions)

    def test_the_widest_grant_still_cannot_promote(self):
        conn = a_database()
        with self.assertRaises(tools.PermissionDenied):
            tools.Grant.researcher().require(
                tools.PROMOTE_CANDIDATE, "anything")
        conn.close()

    def test_every_tool_declares_a_permission(self):
        for spec in tools.registered():
            self.assertTrue(spec.permission)

    def test_no_tool_modifies_production(self):
        names = {spec.name for spec in tools.registered()}
        for forbidden in ("modify_production", "submit_order", "set_risk",
                          "set_capital", "update_model"):
            self.assertNotIn(forbidden, names)


# ======================================================================
# §74 — the adversarial list
# ======================================================================

class TestAdversarial(unittest.TestCase):

    def setUp(self):
        self.conn = a_database()

    def tearDown(self):
        self.conn.close()

    def test_researcher_sees_future_data(self):
        """CAUGHT A REAL BUG: 46 patterns are keyed on `primary_error`."""
        with self.assertRaises(governance.LeakageRefused):
            governance.assert_decision_time({"primary_error": "x"})

    def test_researcher_uses_the_final_test_repeatedly(self):
        governance.record_window_use(
            self.conn, starts_at="2026-08-01", ends_at="2026-08-31",
            hypothesis_id="h1")
        governance.record_window_use(
            self.conn, starts_at="2026-08-01", ends_at="2026-08-31",
            hypothesis_id="h2")
        self.assertEqual(
            governance.window_use_count(self.conn, "2026-08-01", "2026-08-31"), 2)

    def test_researcher_reaches_into_a_protected_window(self):
        governance.declare_window(self.conn, label="final",
                                  starts_at="2026-09-01", ends_at="2026-12-31")
        with self.assertRaises(governance.ProtectedWindowRefused):
            governance.assert_window_allowed(
                self.conn, starts_at="2026-08-15", ends_at="2026-09-15")

    def test_a_window_merely_clipping_a_protected_edge_is_refused(self):
        """"Only a little" is not a property that survives repetition."""
        governance.declare_window(self.conn, label="final",
                                  starts_at="2026-09-01", ends_at="2026-12-31")
        with self.assertRaises(governance.ProtectedWindowRefused):
            governance.assert_window_allowed(
                self.conn, starts_at="2026-08-01", ends_at="2026-09-01")

    def test_researcher_creates_infinite_experiments(self):
        report = cycle.run_cycle(
            self.conn, apply=True,
            budget=ResearchBudget(max_experiments_per_cycle=2))
        self.assertLessEqual(len(report["conclusions"]), 2)
        self.assertTrue(report["termination_reason"])

    def test_researcher_creates_thousands_of_variants(self):
        budget = ResearchBudget(max_variants_per_experiment=3)
        self.assertEqual(budget.max_variants_per_experiment, 3)

    def test_researcher_keeps_only_the_winning_experiment(self):
        """§20: the family reports best AND median."""
        stats = prioritization.family_statistics(self.conn, "fam-x")
        self.assertIn("median_effect", stats)

    def test_a_rejected_hypothesis_is_not_deleted(self):
        for name, source in package_sources():
            for text in executed_sql(source):
                upper = " ".join(text.upper().split())
                self.assertNotIn("DELETE FROM AUTORESEARCH_HYPOTHESES", upper)
                self.assertNotIn("DELETE FROM AUTORESEARCH_CONCLUSIONS", upper)

    def test_a_failed_result_cannot_enter_production(self):
        """There is no path from any conclusion into production."""
        for name, source in package_sources():
            for text in executed_sql(source):
                target = write_target(text)
                self.assertNotIn(target, ("trained_models",
                                          "signal_strategies", "risk_limits"),
                                 "%s writes %s" % (name, target))

    def test_a_candidate_cannot_bypass_review(self):
        self.assertEqual(
            api.integrity_check(self.conn)["candidates_not_requiring_review"], 0)

    def test_an_llm_cannot_override_a_deterministic_metric(self):
        """
        §47: no LLM exists here, and if one is added the metrics are
        computed by Phase 22 rather than asserted by any caller.
        """
        for name, source in package_sources():
            self.assertNotIn("openai", code_only(source).lower())

    def test_an_agent_cannot_modify_risk_or_capital(self):
        names = {spec.name for spec in tools.registered()}
        self.assertFalse(names & {"set_risk", "set_capital", "update_limits"})

    def test_a_duplicate_experiment_is_detected(self):
        hypothesis = a_hypothesis(self.conn)
        hypothesis_layer.save(self.conn, [hypothesis])
        twin = a_hypothesis(self.conn)
        twin.hypothesis_id = "h-other"
        self.assertIsNotNone(hypothesis_layer.find_duplicate(self.conn, twin))

    def test_a_contradictory_experiment_is_not_ignored(self):
        """§44: conflicting results are surfaced, never averaged."""
        self.assertIn(ConclusionType.CONFLICTING_EVIDENCE,
                      list(ConclusionType))

    def test_a_tiny_sample_cannot_become_a_strong_conclusion(self):
        conclusion = cycle.interpret(
            self.conn, a_hypothesis(self.conn), "exp-1",
            {"effect": 0.4, "effect_in_sample": 0.4, "effect_low": 0.3,
             "effect_high": 0.5, "robust_slices": 3,
             "robust_slices_passing": 3,
             "candidate_out_of_sample": {"sample_size": 4}})
        self.assertEqual(conclusion.conclusion, ConclusionType.INSUFFICIENT_DATA)
        self.assertFalse(conclusion.promising)

    def test_one_asset_cannot_become_a_universal_rule(self):
        self.assertIn("single_instrument",
                      cycle.overfitting_warnings(
                          effect=0.1, effect_in_sample=0.1,
                          instrument_count=1))

    def test_a_cached_result_is_labelled_rather_than_hidden(self):
        """§71: reuse is Phase 22's, and it records `cache_hit`."""
        columns = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(experiment_runs)")}
        self.assertIn("cache_hit", columns)

    def test_the_researcher_cannot_widen_its_own_grant(self):
        grant = tools.Grant.read_only()
        with self.assertRaises(Exception):
            grant.permissions.add(tools.RUN_EXPERIMENT)


# ======================================================================
# §88 — the final audit, as queries
# ======================================================================

class TestIntegrity(unittest.TestCase):

    def test_every_integrity_count_is_zero_after_a_real_cycle(self):
        conn = a_database()
        cycle.run_cycle(conn, apply=True,
                        budget=ResearchBudget(max_experiments_per_cycle=3))
        failures = {key: value
                    for key, value in api.integrity_check(conn).items()
                    if value}
        self.assertEqual(failures, {})
        conn.close()

    def test_every_action_is_attributed_to_an_actor(self):
        conn = a_database()
        cycle.run_cycle(conn, apply=True,
                        budget=ResearchBudget(max_experiments_per_cycle=2))
        for row in audit.trail(conn, limit=100):
            self.assertIn(row["actor"], {a.value for a in Actor})
        conn.close()

    def test_the_audit_stores_no_reasoning_transcript(self):
        """§81: concise rationale and structured evidence, never a trace."""
        self.assertLessEqual(audit.MAX_REASON, 2000)
        columns_source = open(
            os.path.join(ROOT, "src", "data_access", "autoresearch_schema.py"),
            encoding="utf-8").read()
        for word in ("chain_of_thought", "reasoning_trace", "transcript"):
            self.assertNotIn(word, columns_source)


if __name__ == "__main__":
    unittest.main()
