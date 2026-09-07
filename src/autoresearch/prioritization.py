"""
src/autoresearch/prioritization.py
--------------------------------------------
Phase 23 §11, §12, §20, §62-§66 — what to research next, and what to stop
researching.

WHY PRIORITY IS A VECTOR AND NOT A NUMBER
---------------------------------------------
`PriorityScore` stores six components and the total. Storing only the
total would make the ordering unarguable, and a research priority
nobody can argue with is one nobody will trust or correct.

There is deliberately NO predicted-profitability component. §11 says
priority must not rest on predicted profit alone, and the reliable way
to obey that is not to compute the quantity: a field that exists will
be weighted eventually, and it would dominate the moment somebody was
under pressure for a result.

WHY DEAD ENDS MATTER MORE THAN LEADS
----------------------------------------
§65 asks for a way to stop testing an idea that keeps failing. That is
the same mechanism as §21's multiple-testing control seen from the
other side: a family that has been tested twenty times and produced one
marginal success has produced no successes, and the twentieth test is
where a researcher without this bookkeeping starts believing the
noise.

`family_state` therefore reports the MEDIAN effect beside the best one.
§20 is explicit: do not select only the best experiment. A family whose
best result is +0.4% and whose median is −0.2% is a weak family, and
the pair of numbers says so where either alone would not.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.domain.autoresearch_models import (
    MIN_RESEARCH_SAMPLE, ConclusionType, FamilyStatus, PriorityScore,
    ResearchCost, ResearchObservation, utcnow,
)

#: A family with at least this many concluded experiments and no
#: SUPPORTED conclusion is depleted (§65). Set below the Phase 22
#: `max_repeats_per_family` budget so depletion is reached by evidence
#: rather than by running out of allowance.
DEPLETION_THRESHOLD = 6

#: Below this total, a question is filed LOW_PRIORITY rather than
#: queued. Not a quality judgement -- a queue that admits everything is
#: not a queue.
PRIORITY_FLOOR = 0.35


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


# ======================================================================
# Cost
# ======================================================================

def estimate_cost(observation: ResearchObservation, *,
                  variants: int = 1) -> ResearchCost:
    """
    What testing this observation is likely to cost (§12).

    Rough by design. The purpose is to prefer a cheap decisive test
    over an expensive one, not to predict a runtime to the second --
    and a precise-looking estimate would be trusted more than it
    deserves.
    """
    rows = max(observation.sample_size, MIN_RESEARCH_SAMPLE)
    # Measured on this project: a signal cohort experiment with three
    # robustness slices and a 2,000-iteration bootstrap runs in about
    # two seconds per variant over a few thousand rows.
    seconds = 2.0 * variants + rows / 5000.0
    complexity = 1 + (1 if observation.measures.get("conditions") else 0)
    return ResearchCost(rows_required=rows, variants=variants,
                        estimated_seconds=round(seconds, 2),
                        complexity=complexity)


# ======================================================================
# Priority
# ======================================================================

def score_observation(observation: ResearchObservation, *,
                      novelty: float,
                      cost: ResearchCost,
                      known_weaknesses: Sequence[str] = ()) -> PriorityScore:
    """
    Why this observation is worth a question before that one (§11).

    `weakness_relevance` is the component that keeps the researcher
    pointed at the system's actual problems rather than at whatever is
    easiest to measure. It is fed from the error types the record
    already attributes most often, so "relevant" means relevant to
    documented failures rather than to a hunch.
    """
    sample = observation.sample_size
    sample_adequacy = min(sample / (4.0 * MIN_RESEARCH_SAMPLE), 1.0)

    # Evidence strength: how much record stands behind the observation,
    # tempered by how many distinct instruments it spans. A pattern
    # drawn from one instrument is one instrument's history, however
    # many rows it contains (§74: one asset must not become a universal
    # rule).
    instruments = int(observation.measures.get("instrument_count") or 0)
    breadth = min(instruments / 10.0, 1.0) if instruments else 0.3
    evidence_strength = round(min(0.6 * sample_adequacy + 0.4 * breadth, 1.0), 4)

    quality = str(observation.measures.get("quality") or "").lower()
    stability = str(observation.measures.get("stability") or "").lower()
    confidence = 0.3
    if quality == "confirmed":
        confidence += 0.4
    if stability == "stable":
        confidence += 0.3
    confidence = round(min(confidence, 1.0), 4)

    subject = observation.subject.lower()
    relevance = 1.0 if any(w.lower() in subject or w.lower() in
                           observation.statement.lower()
                           for w in known_weaknesses) else 0.4

    # Research risk, not trading risk: how likely is this line of
    # enquiry to produce a result that misleads. A single-instrument
    # observation is the classic case.
    risk = 0.0
    if instruments == 1:
        risk += 0.5
    if sample < 2 * MIN_RESEARCH_SAMPLE:
        risk += 0.3

    return PriorityScore(
        evidence_strength=evidence_strength,
        sample_adequacy=round(sample_adequacy, 4),
        novelty=round(novelty, 4),
        weakness_relevance=relevance,
        confidence=confidence,
        cost_penalty=cost.score,
        risk_penalty=round(min(risk, 1.0), 4))


def known_weaknesses(conn: sqlite3.Connection, *, limit: int = 5
                     ) -> List[str]:
    """The failures the record attributes most often (§11)."""
    if not _table_exists(conn, "error_attributions"):
        return []
    return [row[0] for row in conn.execute("""
        SELECT error_type FROM error_attributions
        WHERE role='primary' AND observability='observed'
          AND error_type NOT IN ('no_error','unknown','expected_loss')
        GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT ?
    """, (limit,))]


# ======================================================================
# Family state — dead ends and reactivation
# ======================================================================

def family_statistics(conn: sqlite3.Connection, family_id: str
                      ) -> Dict[str, Any]:
    """
    How a hypothesis family has actually done (§14, §20).

    Reports best AND median. §20 forbids selecting only the best
    experiment, and the honest way to enforce that is to make the
    median impossible to avoid seeing.
    """
    if not _table_exists(conn, "autoresearch_conclusions"):
        return {"experiments": 0, "supported": 0, "rejected": 0,
                "inconclusive": 0, "best_effect": None, "median_effect": None}

    rows = conn.execute("""
        SELECT c.conclusion, c.effect
        FROM autoresearch_conclusions c
        JOIN autoresearch_hypotheses h
          ON h.hypothesis_id = c.hypothesis_id
        WHERE h.family_id = ?
    """, (family_id,)).fetchall()

    effects = [effect for _c, effect in rows if effect is not None]
    counts = {"supported": 0, "rejected": 0, "inconclusive": 0}
    for conclusion, _effect in rows:
        if conclusion == ConclusionType.SUPPORTED.value:
            counts["supported"] += 1
        elif conclusion == ConclusionType.REJECTED.value:
            counts["rejected"] += 1
        else:
            counts["inconclusive"] += 1

    return {
        "experiments": len(rows),
        "supported": counts["supported"],
        "rejected": counts["rejected"],
        "inconclusive": counts["inconclusive"],
        "best_effect": max(effects) if effects else None,
        "median_effect": statistics.median(effects) if effects else None,
    }


def assess_family(stats: Dict[str, Any]) -> Tuple[FamilyStatus, str]:
    """
    Whether a family is worth more research (§65).

    RESEARCH_DEPLETED is not a permanent verdict -- §66 allows
    reactivation when the context changes. It means "stop spending the
    budget here for now", which is a scheduling decision, not a claim
    that the idea is false.
    """
    experiments = stats.get("experiments", 0)
    supported = stats.get("supported", 0)
    if experiments == 0:
        return FamilyStatus.ACTIVE, "no experiment has been concluded yet"
    if supported:
        return FamilyStatus.ACTIVE, (
            "%d of %d experiments in this family concluded SUPPORTED"
            % (supported, experiments))
    if experiments >= DEPLETION_THRESHOLD:
        median = stats.get("median_effect")
        best = stats.get("best_effect")
        return FamilyStatus.RESEARCH_DEPLETED, (
            "%d experiments, none supported; best effect %s, median %s. "
            "Further tests of the same idea are more likely to find noise "
            "than an effect."
            % (experiments,
               "n/a" if best is None else "%+0.4f" % best,
               "n/a" if median is None else "%+0.4f" % median))
    if experiments >= DEPLETION_THRESHOLD // 2:
        return FamilyStatus.LOW_PRIORITY, (
            "%d experiments, none supported; not yet depleted but no longer "
            "the best use of the budget" % experiments)
    return FamilyStatus.ACTIVE, "%d experiments so far" % experiments


def reactivation_reason(conn: sqlite3.Connection, family_id: str,
                        *, since: Optional[str] = None) -> str:
    """
    Whether a depleted family deserves another look (§66).

    Returns a reason, or "" for none. The trigger is new EVIDENCE, not
    the passage of time: a family becomes interesting again when the
    record it failed against has changed, which is a fact about the
    database rather than about anyone's patience.
    """
    if not since or not _table_exists(conn, "trading_experiences"):
        return ""
    added = conn.execute("""
        SELECT COUNT(*) FROM trading_experiences WHERE available_at > ?
    """, (since,)).fetchone()[0]
    if added >= 10 * MIN_RESEARCH_SAMPLE:
        return ("%d experiences have become knowable since this family was "
                "last tested, which is enough new record to change what a "
                "chronological split would show." % added)
    return ""


def save_family_state(conn: sqlite3.Connection, family_id: str,
                      stats: Dict[str, Any], status: FamilyStatus,
                      reason: str, *, reactivation: str = "") -> None:
    from src.data_access.autoresearch_schema import initialize_autoresearch_schema
    from src.domain.autoresearch_models import RESEARCH_METHOD_VERSION
    initialize_autoresearch_schema(conn)
    conn.execute("""
        INSERT OR REPLACE INTO autoresearch_family_state (
            family_id, method_version, status, experiments, supported,
            rejected, inconclusive, best_effect, median_effect, reason,
            reactivation_reason, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (family_id, RESEARCH_METHOD_VERSION, status.value,
          stats.get("experiments", 0), stats.get("supported", 0),
          stats.get("rejected", 0), stats.get("inconclusive", 0),
          stats.get("best_effect"), stats.get("median_effect"), reason,
          reactivation, utcnow()))
    conn.commit()


# ======================================================================
# Research portfolio — diversity and exploration
# ======================================================================

#: The areas a research programme should spread across (§62). Not a
#: quota: a report, so a programme that has tested nothing but signal
#: thresholds can see that about itself.
RESEARCH_AREAS = ("signal", "model", "feature", "regime", "event",
                  "execution", "risk", "portfolio")


def research_diversity(conn: sqlite3.Connection) -> Dict[str, Any]:
    """
    What the research budget has actually been spent on (§62).

    Returns counts per area plus a concentration figure: the share
    taken by the single largest area. A programme at 1.0 has tested
    exactly one kind of idea, however many experiments it ran.
    """
    if not _table_exists(conn, "autoresearch_hypotheses"):
        return {"areas": {}, "total": 0, "concentration": None,
                "untouched": list(RESEARCH_AREAS)}

    counts = {area: 0 for area in RESEARCH_AREAS}
    rows = conn.execute("""
        SELECT evaluator, family_name, statement FROM autoresearch_hypotheses
    """).fetchall()
    for evaluator, family_name, statement in rows:
        text = " ".join(str(x or "") for x in (evaluator, family_name, statement)).lower()
        for area in RESEARCH_AREAS:
            if area in text:
                counts[area] += 1
                break
        else:
            counts["signal"] += 1  # the evaluators that exist are signal-level

    total = sum(counts.values())
    largest = max(counts.values()) if total else 0
    return {
        "areas": counts,
        "total": total,
        "concentration": round(largest / total, 4) if total else None,
        "untouched": [area for area, n in counts.items() if n == 0],
    }


def exploration_balance(conn: sqlite3.Connection) -> Dict[str, Any]:
    """
    How much of the budget went to new ground versus known ground (§63).

    This is research portfolio bookkeeping, NOT reinforcement learning:
    nothing here adjusts a policy, sets a reward, or changes what gets
    proposed next. It reports a ratio and stops, and a human decides
    whether the balance is wrong.
    """
    if not _table_exists(conn, "autoresearch_hypotheses"):
        return {"exploration": 0, "exploitation": 0, "ratio": None}

    rows = conn.execute("""
        SELECT family_id, COUNT(*) FROM autoresearch_hypotheses
        WHERE family_id != '' GROUP BY 1
    """).fetchall()
    exploration = sum(1 for _f, n in rows if n == 1)
    exploitation = sum(n for _f, n in rows if n > 1)
    total = exploration + exploitation
    return {
        "exploration": exploration,
        "exploitation": exploitation,
        "ratio": round(exploration / total, 4) if total else None,
        "note": ("exploration counts families tested once; exploitation "
                 "counts repeat tests of a family already tried"),
    }
