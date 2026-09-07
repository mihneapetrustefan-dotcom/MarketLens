"""
src/autoresearch/cycle.py
-----------------------------------
Phase 23 §3, §16-§18, §32-§39, §52 — one bounded pass of the research
loop.

    observe -> question -> hypothesis -> proposal -> experiment
        -> result -> conclusion -> memory

Every arrow is a stored row, so §37's trace is a join rather than a
narrative: an observation id leads to a question id leads to a
hypothesis id leads to a Phase 22 experiment id leads to a conclusion
id, and each carries the evidence that produced the next.

THE LOOP ENDS AT MEMORY, DELIBERATELY (§34, §85)
----------------------------------------------------
There is no step after "memory". A conclusion may create a
`ResearchCandidate`, and a candidate is a record with
`requires_review=1`. Nothing here promotes a model, changes a
threshold, edits a strategy, touches risk or moves capital, and
`tests/autoresearch/test_boundary_and_safety.py` proves it by parsing
this package rather than by trusting this sentence.

HOW MEMORY IS "UPDATED" (§39) WITHOUT PHASE 23 WRITING PHASE 21's TABLES
----------------------------------------------------------------------------
§39 asks that Trading Memory be updated with the result, conclusion,
evidence, confidence and methodology version. It is tempting to write
those into `trading_experiences` or `memory_patterns`.

That would be wrong twice. A `trading_experience` is what happened
after a decision, with an `available_at` marking when it became
knowable — a research finding has neither, and inserting one would
corrupt the point-in-time property every later phase depends on.
And a phase that writes another phase's tables makes both
unauditable.

So the conclusion row IS the memory update, and it stores the
`memory_pattern_id` it concerns. `memory_feedback()` reads back, for
any pattern, every conclusion about it. Memory gains the knowledge;
Phase 21 keeps its record. Nothing is deleted, ever (§39).

WHY EVERY CYCLE RECORDS WHY IT STOPPED (§52)
------------------------------------------------
`termination_reason` is never blank. A loop that ends without saying
why is indistinguishable from one that crashed, and an autonomous
process nobody can distinguish from a crashed process is not one you
can leave running.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.data_access.autoresearch_schema import initialize_autoresearch_schema
from src.domain.autoresearch_models import (
    MIN_RESEARCH_SAMPLE, RESEARCH_METHOD_VERSION, Actor, BudgetExceeded,
    CandidateStatus, CandidateType, ConclusionType, Evidence, QueueState,
    ResearchBudget, ResearchCandidate, ResearchConclusion, ResearchConfidence,
    ResearchHypothesis, TriageState, _digest, assess_confidence,
    overfitting_warnings, research_quality_gate, utcnow,
)
from src.autoresearch import (
    candidates as candidate_registry, governance, hypotheses as hypothesis_layer,
    observations as observation_layer, prioritization,
    questions as question_layer, queue as queue_layer,
)
from src.autoresearch.audit import record as audit_record


def _cycle_id() -> str:
    return "cyc-" + _digest({"t": utcnow()})[:18]


def _conclusion_id(hypothesis_id: str, experiment_id: str) -> str:
    return "con-" + _digest({"h": hypothesis_id, "e": experiment_id})[:18]


# ======================================================================
# Turning a hypothesis into a Phase 22 experiment
# ======================================================================

def build_experiment(conn: sqlite3.Connection,
                     hypothesis: ResearchHypothesis):
    """
    A Phase 22 `Experiment` expressing this hypothesis (§27, §28).

    THERE IS NO SECOND EXPERIMENT ENGINE. This constructs a Phase 22
    object and hands it to Phase 22 to run. The falsifiability criteria
    fixed when the hypothesis was written become the Phase 22
    `AcceptanceCriteria`, which lives inside the experiment fingerprint
    — so once the run starts, what counts as success cannot move, and
    the guarantee is Phase 22's rather than a new one.
    """
    from src.domain.experiment_models import (
        AcceptanceCriteria, ArmSpec, DatasetSnapshot, EvaluationProtocol,
        Experiment, ExperimentType, Hypothesis as ExperimentHypothesis,
        HypothesisSource, ResourceLimits,
    )
    from src.experiments import (
        engine as experiment_engine, evaluators, templates,
    )

    source_map = {
        "memory_pattern": HypothesisSource.MEMORY_PATTERN,
        "error_pattern": HypothesisSource.ERROR_ATTRIBUTION,
        "signal_analysis": HypothesisSource.MEMORY_PATTERN,
        "model_analysis": HypothesisSource.RESEARCHER,
        "experiment_result": HypothesisSource.RESEARCHER,
        "human_input": HypothesisSource.RESEARCHER,
    }
    source = source_map.get(hypothesis.source.value, HypothesisSource.RESEARCHER)

    falsifiability = hypothesis.falsifiability
    criteria = AcceptanceCriteria(
        min_effect=falsifiability.minimum_effect,
        min_sample=falsifiability.minimum_sample,
        require_interval_excludes_zero=(
            falsifiability.require_interval_excludes_zero),
    )

    # The experiment identity includes HOW MUCH RECORD it saw.
    #
    # Deriving it from the hypothesis alone asserted that one claim maps
    # to exactly one experiment forever. It does not: the same claim
    # tested on a longer record is a different experiment, and treating
    # the two as one caused a stale cached result to be returned as
    # current research (see `engine.current_data_cutoff`).
    cutoff = experiment_engine.current_data_cutoff(conn)
    experiment = Experiment(
        experiment_id="exp-" + _digest({
            "hypothesis": hypothesis.hypothesis_id,
            "cutoff": cutoff})[:20],
        name=hypothesis.statement[:110],
        experiment_type=ExperimentType.SIGNAL,
        hypothesis=ExperimentHypothesis(
            statement=hypothesis.statement,
            mechanism=hypothesis.mechanism,
            expected_effect=falsifiability.expected_result,
            population=hypothesis.population,
            conditions=dict(hypothesis.condition),
            metric=falsifiability.evaluation_metric,
            source=source,
            source_reference=hypothesis.source_reference,
            family_id=hypothesis.family_id),
        baseline=evaluators.baseline(hypothesis.baseline),
        candidate=ArmSpec(
            name=hypothesis.statement[:90],
            evaluator=hypothesis.evaluator,
            parameters=dict(hypothesis.parameters),
            description=("Proposed by the Phase 23 research loop from "
                         "question %s." % hypothesis.question_id),
            complexity=1 + len(hypothesis.parameters)),
        dataset=DatasetSnapshot(data_cutoff=cutoff),
        protocol=EvaluationProtocol(),
        criteria=criteria,
        limits=ResourceLimits())

    if hypothesis.family_id:
        templates.ensure_family(
            conn, hypothesis.family_id,
            hypothesis.family_name or hypothesis.family_id,
            hypothesis.statement[:160])
    return experiment


# ======================================================================
# Interpreting a result
# ======================================================================

def evaluation_window(conn: sqlite3.Connection, experiment
                      ) -> "tuple[str, str]":
    """
    The out-of-sample period an experiment will actually be judged on.

    Needed for a REAL data-snooping ledger (§22). The first version
    recorded every use under the key ".." because no window was
    computed, which meant the ledger counted "some experiment ran"
    rather than "this period was tested again" -- it would have flagged
    unrelated windows as reuse and never distinguished two genuinely
    different periods.

    Derived the same way Phase 22 derives it: order the cohort by when
    each row became knowable, cut at the holdout fraction, and take the
    tail. Same inputs, same boundary, no second definition of what
    "out of sample" means.
    """
    from src.experiments import engine as experiment_engine
    try:
        rows = experiment_engine.load_cohort(conn, experiment)
    except Exception:
        return ("", "")
    if not rows:
        return ("", "")
    holdout = getattr(experiment.protocol, "holdout_fraction", 0.3)
    cut = int(len(rows) * (1.0 - holdout))
    test = rows[cut:]
    if not test:
        return ("", "")
    return (str(test[0].get("available_at") or ""),
            str(test[-1].get("available_at") or ""))


def _record_span_days(conn: sqlite3.Connection) -> float:
    """
    How many days of record a conclusion rests on.

    Feeds §19's `narrow_time_period` warning. Measured rather than
    passed in, because the caller that would pass it is the one with an
    interest in it being large -- and because on this database the
    honest answer (about four weeks) makes the warning fire on every
    single result, which is the correct outcome and one nobody would
    have chosen to hard-code.
    """
    try:
        row = conn.execute(
            "SELECT MIN(available_at), MAX(available_at) "
            "FROM trading_experiences WHERE available_at IS NOT NULL"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0.0
    if not row or not row[0] or not row[1]:
        return 0.0
    try:
        start = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(row[1]).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0.0
    return max((end - start).total_seconds() / 86400.0, 0.0)


def interpret(conn: sqlite3.Connection, hypothesis: ResearchHypothesis,
              experiment_id: str, result: Dict[str, Any], *,
              cycle_id: Optional[str] = None) -> ResearchConclusion:
    """
    Turn a Phase 22 result into a research conclusion (§17, §18, §19).

    The metrics are NOT recomputed. Phase 22 measured the effect, the
    interval and the robustness; recomputing any of them here would
    eventually produce a second answer, and then neither could be
    trusted. What this adds is interpretation: which of §19's
    overfitting shapes the result has, how much weight it can bear,
    and which of the six conclusion types fits.

    INCONCLUSIVE is reached before REJECTED whenever the evidence could
    not decide. "We could not tell" and "it does not work" are
    different findings and collapsing them loses the more useful one.
    """
    effect = result.get("effect")
    in_sample = result.get("effect_in_sample")
    low = result.get("effect_low")
    high = result.get("effect_high")
    slices_total = int(result.get("robust_slices") or 0)
    slices_passing = int(result.get("robust_slices_passing") or 0)
    complexity_ratio = result.get("complexity_ratio")

    # Phase 22 names this key `candidate_out_of_sample`. Reading a
    # key that does not exist gave every conclusion a sample of zero
    # and therefore INSUFFICIENT_DATA -- including one with a +0.15
    # effect, which is how a silent key mismatch turns into a research
    # record full of confident-looking non-findings.
    candidate_metrics = result.get("candidate_out_of_sample") or {}
    sample = int(candidate_metrics.get("sample_size") or 0)

    # Sensitivity is not part of a single run's result: it comes from a
    # deliberate sweep (`engine.sensitivity`). Absent means "not swept",
    # which is why `single_parameter_peak` cannot fire from one run --
    # it needs the neighbours measured.
    sensitivity = result.get("sensitivity") or {}
    shape = str(sensitivity.get("shape") or "")

    reuse = governance.window_use_count(
        conn, result.get("window_start"), result.get("window_end"))

    warnings = overfitting_warnings(
        effect=effect, effect_in_sample=in_sample,
        sensitivity_shape=shape,
        instrument_count=int(candidate_metrics.get("instrument_count") or 0),
        regime_count=1,  # regime is NULL throughout; see §4 blind spots
        variants=int(result.get("variants") or 0),
        window_reuse_count=reuse,
        span_days=_record_span_days(conn),
        slices_total=slices_total, slices_passing=slices_passing)

    falsifiability = hypothesis.falsifiability
    reasons: List[str] = []
    limitations = list(result.get("limitations") or [])

    # --- which of the six -----------------------------------------
    if sample < falsifiability.minimum_sample:
        conclusion_type = ConclusionType.INSUFFICIENT_DATA
        reasons.append(
            "the candidate arm holds %d observations out of sample, below "
            "the %d fixed before the test. Nothing can be concluded from it "
            "in either direction." % (sample, falsifiability.minimum_sample))
    elif effect is None:
        conclusion_type = ConclusionType.INCONCLUSIVE
        reasons.append("no out-of-sample effect could be measured")
    elif low is not None and high is not None and low <= 0 <= high:
        # The interval spans zero. That is not a rejection: it says the
        # data cannot separate the candidate from its baseline.
        conclusion_type = ConclusionType.INCONCLUSIVE
        reasons.append(
            "the 95%% bootstrap interval [%+0.4f, %+0.4f] includes zero, so "
            "the data does not distinguish the candidate from the baseline. "
            "This is not a rejection of the mechanism; it is an absence of "
            "measurable evidence either way." % (low, high))
    elif effect >= falsifiability.minimum_effect:
        conclusion_type = ConclusionType.SUPPORTED
        reasons.append(
            "out-of-sample effect %+0.4f meets the %+0.4f required, with an "
            "interval that excludes zero"
            % (effect, falsifiability.minimum_effect))
    elif effect > 0:
        conclusion_type = ConclusionType.PARTIALLY_SUPPORTED
        reasons.append(
            "out-of-sample effect %+0.4f is positive and below the %+0.4f "
            "required. The direction matches the hypothesis; the size does "
            "not clear the bar set before the test."
            % (effect, falsifiability.minimum_effect))
    else:
        conclusion_type = ConclusionType.REJECTED
        reasons.append(
            "out-of-sample effect %+0.4f is in the opposite direction to the "
            "hypothesis, against %+0.4f required"
            % (effect, falsifiability.minimum_effect))

    # --- conflict with what the record already concluded (§44) ----
    conflicting = _conflicting_conclusions(conn, hypothesis, conclusion_type)
    if conflicting:
        conclusion_type = ConclusionType.CONFLICTING_EVIDENCE
        reasons.append(
            "a previous conclusion on the same claim disagrees with this "
            "one (%s). The two are NOT averaged: they are reported as "
            "conflicting so the difference can be investigated by period, "
            "regime, instrument or methodology." % ", ".join(conflicting))

    for warning in warnings:
        reasons.append("overfitting shape present: " + warning)

    confidence = assess_confidence(
        sample_size=sample, effect_low=low, effect_high=high,
        slices_total=slices_total, slices_passing=slices_passing,
        warnings=warnings)

    promising, gate_reasons = research_quality_gate(
        conclusion=conclusion_type, effect=effect,
        falsifiability=falsifiability, sample_size=sample,
        effect_low=low, effect_high=high, slices_total=slices_total,
        slices_passing=slices_passing, complexity_ratio=complexity_ratio,
        warnings=warnings)
    reasons.extend(gate_reasons)

    if reuse > 1:
        limitations.append(
            "this evaluation window has now been tested %d times; a result "
            "found on repeated passes over the same period deserves less "
            "weight than one found on the first." % reuse)

    family_count = governance.multiple_testing_state(
        conn, hypothesis.family_id).get("hypotheses", 1)
    if family_count > 1:
        limitations.append(
            "%d hypotheses have been tested in this family; with that many "
            "attempts one apparently significant result is expected by "
            "chance." % family_count)

    conclusion = ResearchConclusion(
        conclusion_id=_conclusion_id(hypothesis.hypothesis_id, experiment_id),
        hypothesis_id=hypothesis.hypothesis_id,
        question_id=hypothesis.question_id,
        experiment_id=experiment_id,
        conclusion=conclusion_type,
        confidence=confidence,
        effect=effect, effect_in_sample=in_sample,
        effect_low=low, effect_high=high,
        sample_size=sample,
        reasons=reasons, limitations=limitations, warnings=warnings,
        evidence=list(hypothesis.evidence) + [
            Evidence(kind="experiment_results", reference=experiment_id,
                     detail="effect %s out of sample"
                            % ("unmeasured" if effect is None
                               else "%+0.4f" % effect))],
        family_experiment_count=family_count,
        promising=promising)
    conclusion.validate()
    return conclusion


def _conflicting_conclusions(conn: sqlite3.Connection,
                             hypothesis: ResearchHypothesis,
                             proposed: ConclusionType) -> List[str]:
    """
    Earlier conclusions on the same claim that point the other way (§44).

    Only a genuine disagreement counts: SUPPORTED against REJECTED. An
    earlier INCONCLUSIVE does not conflict with anything — it is the
    absence of a finding, and treating it as one would manufacture
    conflict out of silence.
    """
    initialize_autoresearch_schema(conn)
    opposites = {
        ConclusionType.SUPPORTED: (ConclusionType.REJECTED.value,),
        ConclusionType.REJECTED: (ConclusionType.SUPPORTED.value,),
    }
    wanted = opposites.get(proposed)
    if not wanted:
        return []
    rows = conn.execute("""
        SELECT c.conclusion_id, c.conclusion FROM autoresearch_conclusions c
        JOIN autoresearch_hypotheses h ON h.hypothesis_id = c.hypothesis_id
        WHERE h.claim_fingerprint = ? AND c.conclusion IN (%s)
    """ % ",".join("?" * len(wanted)),
        (hypothesis.claim_fingerprint,) + tuple(wanted)).fetchall()
    return ["%s concluded %s" % (row[0], row[1]) for row in rows]


def save_conclusion(conn: sqlite3.Connection, conclusion: ResearchConclusion,
                    *, cycle_id: Optional[str] = None) -> None:
    """
    Store a conclusion. Negative ones exactly like positive ones (§42).

    There is no delete path in this module, and no code anywhere in the
    package removes a conclusion row.
    """
    initialize_autoresearch_schema(conn)
    conn.execute("""
        INSERT OR REPLACE INTO autoresearch_conclusions (
            conclusion_id, method_version, hypothesis_id, question_id,
            experiment_id, cycle_id, conclusion, confidence, effect,
            effect_in_sample, effect_low, effect_high, sample_size,
            reasons_json, limitations_json, warnings_json, evidence_json,
            family_experiment_count, promising, concluded_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (conclusion.conclusion_id, conclusion.method_version,
          conclusion.hypothesis_id, conclusion.question_id,
          conclusion.experiment_id, cycle_id, conclusion.conclusion.value,
          conclusion.confidence.value, conclusion.effect,
          conclusion.effect_in_sample, conclusion.effect_low,
          conclusion.effect_high, conclusion.sample_size,
          json.dumps(conclusion.reasons), json.dumps(conclusion.limitations),
          json.dumps(conclusion.warnings),
          json.dumps([e.as_dict() for e in conclusion.evidence]),
          conclusion.family_experiment_count,
          1 if conclusion.promising else 0, conclusion.concluded_at))
    conn.commit()


def memory_feedback(conn: sqlite3.Connection, pattern_id: str
                    ) -> List[Dict[str, Any]]:
    """
    Everything research has concluded about one memory pattern (§39).

    This is the memory update, read from the side that owns it. Phase
    21's tables are never written by Phase 23; instead a pattern's page
    can ask what happened when its regularity was actually tested, and
    the answer includes the failures.
    """
    initialize_autoresearch_schema(conn)
    keys = ("conclusion_id", "hypothesis_id", "experiment_id", "conclusion",
            "confidence", "effect", "sample_size", "concluded_at")
    return [dict(zip(keys, row)) for row in conn.execute("""
        SELECT c.conclusion_id, c.hypothesis_id, c.experiment_id,
               c.conclusion, c.confidence, c.effect, c.sample_size,
               c.concluded_at
        FROM autoresearch_conclusions c
        JOIN autoresearch_hypotheses h ON h.hypothesis_id = c.hypothesis_id
        WHERE h.source_reference = ?
        ORDER BY c.concluded_at DESC
    """, (pattern_id,))]


# ======================================================================
# The cycle
# ======================================================================

def run_cycle(conn: sqlite3.Connection, *,
              budget: Optional[ResearchBudget] = None,
              apply: bool = False,
              trigger: str = "manual",
              actor: Actor = Actor.SYSTEM,
              max_seconds: Optional[float] = None) -> Dict[str, Any]:
    """
    One bounded pass of the loop (§3, §52).

    `apply=False` runs everything except the writes and the
    experiments, so a caller can see what a cycle WOULD do. That is the
    same discipline as every other script in this project.

    The cycle always terminates for a stated reason: the budget, the
    timeout, an empty queue, or a refusal. It never loops on itself.
    """
    budget = budget or ResearchBudget()
    started = time.time()
    deadline = max_seconds if max_seconds is not None else budget.max_runtime_seconds
    cycle_id = _cycle_id()
    initialize_autoresearch_schema(conn)

    report: Dict[str, Any] = {
        "cycle_id": cycle_id, "apply": apply, "trigger": trigger,
        "budget": budget.as_dict(), "observations": 0, "blind_spots": [],
        "questions": {}, "hypotheses": 0, "duplicates_skipped": 0,
        "queued": 0, "selected": [], "skipped": [], "conclusions": [],
        "candidates": [], "unrunnable": [], "reclaimed": [],
        "not_claimed": [], "termination_reason": "",
    }

    # --- 1. observe ------------------------------------------------
    found, blind = observation_layer.observe_all(conn)
    report["observations"] = len(found)
    report["blind_spots"] = blind
    if apply:
        observation_layer.save(conn, found)
    by_id = {o.observation_id: o for o in found}

    # --- 2. question and triage -----------------------------------
    triaged = question_layer.raise_questions(conn, found)
    counts: Dict[str, int] = {}
    for question, _s, _c in triaged:
        counts[question.triage.value] = counts.get(question.triage.value, 0) + 1
    report["questions"] = counts
    if apply:
        question_layer.save(conn, triaged)

    # --- 3. hypothesise -------------------------------------------
    formed: List[Tuple[ResearchHypothesis, float]] = []
    seen_claims: set = set()
    for question, score, _cost in triaged:
        if not question.is_testable:
            continue
        observation = by_id.get(question.observation_id)
        if observation is None:
            continue
        try:
            hypothesis = hypothesis_layer.from_question(question, observation)
        except (hypothesis_layer.HypothesisRefused,
                governance.LeakageRefused, ValueError):
            continue

        if hypothesis.claim_fingerprint in seen_claims:
            report["duplicates_skipped"] += 1
            continue
        seen_claims.add(hypothesis.claim_fingerprint)

        context = hypothesis_layer.research_context(conn, hypothesis)
        if context["duplicate"]:
            report["duplicates_skipped"] += 1
            if apply:
                audit_record(
                    conn, actor=actor, action="skip_duplicate",
                    hypothesis_id=hypothesis.hypothesis_id,
                    cycle_id=cycle_id, decision="skipped",
                    reason="this claim was already tested: %s"
                           % json.dumps(context["duplicate"], default=str)[:300])
            continue

        formed.append((hypothesis, score.total))

    report["hypotheses"] = len(formed)
    if apply and formed:
        hypothesis_layer.save(conn, [h for h, _p in formed])
        for hypothesis, priority in formed:
            queue_layer.enqueue(conn, hypothesis, priority=priority,
                                cycle_id=cycle_id,
                                reason="raised by cycle %s" % cycle_id)
        report["queued"] = len(formed)

    # --- 4. schedule ----------------------------------------------
    if not apply:
        report["termination_reason"] = (
            "dry run: nothing was written and no experiment was run. "
            "Pass --apply to act on this plan.")
        report["selected"] = [
            {"hypothesis_id": h.hypothesis_id, "statement": h.statement,
             "priority": p} for h, p in formed[:budget.max_experiments_per_cycle]]
        report["runtime_seconds"] = round(time.time() - started, 2)
        return report

    # Recover anything a previous worker abandoned before scheduling
    # anything new, so a crashed run rejoins the queue instead of
    # vanishing from the programme (§59).
    reclaimed = queue_layer.reclaim_stale(conn)
    if reclaimed:
        report["reclaimed"] = [item["queue_id"] for item in reclaimed]
        for item in reclaimed:
            audit_record(conn, actor=actor, action="reclaim_stale_item",
                         hypothesis_id=item["hypothesis_id"],
                         cycle_id=cycle_id, decision="requeued",
                         reason="left running since %s"
                                % (item["started_at"] or "an unknown time"))

    try:
        queue_layer.assert_daily_budget(conn, budget=budget, day=utcnow())
    except BudgetExceeded as exc:
        report["termination_reason"] = str(exc)
        _save_cycle(conn, cycle_id, report, budget, started, trigger, actor)
        return report

    selected, skipped = queue_layer.next_batch(conn, budget=budget)
    report["selected"] = selected
    report["skipped"] = skipped

    # --- 5. run, conclude, remember -------------------------------
    from src.experiments import api as experiment_api, engine as experiment_engine

    for item in selected:
        if time.time() - started > deadline:
            report["termination_reason"] = (
                "the %.0fs cycle timeout was reached; remaining items stay "
                "queued for the next cycle" % deadline)
            break

        record = hypothesis_layer.load(conn, item["hypothesis_id"])
        if record is None:
            continue
        hypothesis = _rebuild(record)

        # Claim atomically. `next_batch` only proposes; without this a
        # second worker that had already selected the same item would
        # run it too, double-spending the budget and inflating the
        # data-snooping ledger with a window use that is not a
        # separate test.
        if not queue_layer.claim(conn, item["queue_id"], cycle_id=cycle_id,
                                 reason="selected by cycle %s" % cycle_id):
            report.setdefault("not_claimed", []).append(item["queue_id"])
            continue

        try:
            experiment = build_experiment(conn, hypothesis)
            experiment_engine.save_experiment(conn, experiment)
            outcome = experiment_api.start(conn, experiment.experiment_id)
        except Exception as exc:  # a failed run is a research event
            queue_layer.set_state(conn, item["queue_id"], QueueState.REJECTED,
                                  reason="the experiment could not run: %s" % exc)
            audit_record(conn, actor=actor, action="experiment_failed",
                         hypothesis_id=hypothesis.hypothesis_id,
                         cycle_id=cycle_id, decision="rejected",
                         reason=str(exc)[:400])
            continue

        hypothesis_layer.attach_experiment(
            conn, hypothesis.hypothesis_id, experiment.experiment_id)
        window_start, window_end = evaluation_window(conn, experiment)
        governance.record_window_use(
            conn, starts_at=window_start, ends_at=window_end,
            hypothesis_id=hypothesis.hypothesis_id,
            experiment_id=experiment.experiment_id,
            family_id=hypothesis.family_id, cycle_id=cycle_id)

        result = outcome.get("result")
        if not result:
            # Phase 22 refused or failed the run, so there is no
            # measurement. Interpreting an absent result would report
            # INSUFFICIENT_DATA -- "we tested and the sample was too
            # small" -- for something that was never tested. No
            # conclusion row is written; the queue records the refusal.
            queue_layer.set_state(
                conn, item["queue_id"], QueueState.REJECTED,
                reason=("the experiment did not produce a result (run "
                        "status %s); no conclusion is drawn, because a run "
                        "that did not happen is not a finding"
                        % outcome.get("status", "unknown")),
                experiment_id=experiment.experiment_id)
            audit_record(conn, actor=actor, action="experiment_no_result",
                         hypothesis_id=hypothesis.hypothesis_id,
                         experiment_id=experiment.experiment_id,
                         cycle_id=cycle_id, decision="rejected",
                         reason="run produced no result; nothing concluded")
            report.setdefault("unrunnable", []).append({
                "hypothesis_id": hypothesis.hypothesis_id,
                "experiment_id": experiment.experiment_id,
                "status": outcome.get("status", "unknown")})
            continue

        result = dict(result)
        result["window_start"] = window_start
        result["window_end"] = window_end
        conclusion = interpret(conn, hypothesis, experiment.experiment_id,
                               result, cycle_id=cycle_id)
        save_conclusion(conn, conclusion, cycle_id=cycle_id)
        report["conclusions"].append({
            "conclusion_id": conclusion.conclusion_id,
            "hypothesis_id": hypothesis.hypothesis_id,
            "experiment_id": experiment.experiment_id,
            "conclusion": conclusion.conclusion.value,
            "confidence": conclusion.confidence.value,
            "effect": conclusion.effect,
            "promising": conclusion.promising,
        })

        queue_layer.set_state(conn, item["queue_id"], QueueState.COMPLETED,
                              reason="concluded %s" % conclusion.conclusion.value,
                              experiment_id=experiment.experiment_id)

        audit_record(conn, actor=actor, action="conclude",
                     hypothesis_id=hypothesis.hypothesis_id,
                     experiment_id=experiment.experiment_id,
                     cycle_id=cycle_id,
                     decision=conclusion.conclusion.value,
                     reason=conclusion.reasons[0] if conclusion.reasons else "",
                     evidence=[e.as_dict() for e in conclusion.evidence])

        if conclusion.promising:
            candidate = candidate_registry.propose(
                conn, hypothesis=hypothesis, conclusion=conclusion,
                experiment_id=experiment.experiment_id)
            report["candidates"].append(candidate.candidate_id)

        if hypothesis.family_id:
            stats = prioritization.family_statistics(conn, hypothesis.family_id)
            status, why = prioritization.assess_family(stats)
            prioritization.save_family_state(
                conn, hypothesis.family_id, stats, status, why)

    if not report["termination_reason"]:
        if not selected:
            report["termination_reason"] = (
                "nothing was eligible to run: %d item(s) were skipped, each "
                "with a recorded reason" % len(skipped))
        else:
            report["termination_reason"] = (
                "every selected item completed; the cycle budget of %d was "
                "not exceeded" % budget.max_experiments_per_cycle)

    _save_cycle(conn, cycle_id, report, budget, started, trigger, actor)
    return report


def _rebuild(record: Dict[str, Any]) -> ResearchHypothesis:
    """Reconstruct a stored hypothesis into its object form."""
    from src.domain.autoresearch_models import (
        FalsifiabilityCriteria, QuestionSource,
    )
    falsifiability_data = record.get("falsifiability") or {}
    falsifiability = FalsifiabilityCriteria(**{
        key: value for key, value in falsifiability_data.items()
        if key in FalsifiabilityCriteria.__dataclass_fields__})
    return ResearchHypothesis(
        hypothesis_id=record["hypothesis_id"],
        question_id=record.get("question_id") or "",
        statement=record["statement"], mechanism=record["mechanism"],
        population=record.get("population") or "",
        condition=record.get("condition") or {},
        expected_direction=record.get("expected_direction") or "increase",
        falsifiability=falsifiability,
        family_id=record.get("family_id") or "",
        family_name=record.get("family_name") or "",
        source=QuestionSource(record.get("source") or "memory_pattern"),
        source_reference=record.get("source_reference") or "",
        evidence=[Evidence(**e) for e in (record.get("evidence") or [])],
        sample_size=record.get("sample_size") or 0,
        evaluator=record.get("evaluator") or "",
        parameters=record.get("parameters") or {},
        baseline=record.get("baseline") or "all_signals",
        author=Actor(record.get("author") or "system"))


def _save_cycle(conn: sqlite3.Connection, cycle_id: str,
                report: Dict[str, Any], budget: ResearchBudget,
                started: float, trigger: str, actor: Actor) -> None:
    runtime = round(time.time() - started, 2)
    report["runtime_seconds"] = runtime
    conn.execute("""
        INSERT OR REPLACE INTO autoresearch_cycles (
            cycle_id, method_version, trigger, actor, budget_json,
            observations_made, questions_raised, hypotheses_formed,
            experiments_run, conclusions_drawn, candidates_proposed,
            duplicates_skipped, termination_reason, runtime_seconds,
            rows_scanned, started_at, finished_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (cycle_id, RESEARCH_METHOD_VERSION, trigger, actor.value,
          json.dumps(budget.as_dict()), report["observations"],
          sum(report["questions"].values()), report["hypotheses"],
          len(report["conclusions"]), len(report["conclusions"]),
          len(report["candidates"]), report["duplicates_skipped"],
          report["termination_reason"], runtime, 0,
          utcnow(), utcnow()))
    conn.commit()


def cycles(conn: sqlite3.Connection, *, limit: int = 25
           ) -> List[Dict[str, Any]]:
    initialize_autoresearch_schema(conn)
    keys = ("cycle_id", "trigger", "actor", "observations_made",
            "questions_raised", "hypotheses_formed", "conclusions_drawn",
            "candidates_proposed", "duplicates_skipped",
            "termination_reason", "runtime_seconds", "started_at")
    return [dict(zip(keys, row)) for row in conn.execute("""
        SELECT cycle_id, trigger, actor, observations_made, questions_raised,
               hypotheses_formed, conclusions_drawn, candidates_proposed,
               duplicates_skipped, termination_reason, runtime_seconds,
               started_at
        FROM autoresearch_cycles ORDER BY started_at DESC LIMIT ?
    """, (limit,))]
