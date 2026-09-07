"""
src/autoresearch/candidates.py
----------------------------------------
Phase 23 §34, §35, §55, §56 — the candidate registry, and the line it
does not cross.

A CANDIDATE IS A RECORD, NOT A DEPLOYMENT
---------------------------------------------
§34 states the boundary: a result may become a CANDIDATE and never
automatically an ACTIVE anything. This module is the whole
implementation of that sentence, and it is deliberately small — the
boundary is enforced by what is ABSENT, not by what is checked.

There is no `promote()` here. There is no function that writes
`trained_models`, `signal_strategies`, `risk_limits`, or any capital
figure. `CandidateStatus.PROMOTED` exists in the vocabulary so Phase
24 does not have to invent it, and no code path in this package can
produce it: `ResearchCandidate.validate()` rejects it outright, and
`tests/autoresearch/test_boundary_and_safety.py` parses this package's
source to prove no production table is written from anywhere in it.

WHY EVERY CANDIDATE REQUIRES REVIEW (§35)
---------------------------------------------
`requires_review` defaults to 1 and this module never sets it to 0.
The review reason is generated from what makes the candidate
consequential — the effect size, the family history, the number of
attempts behind it — so a reviewer sees why it reached them rather
than only that it did.

The most important review reasons are the uncomfortable ones: a
candidate from a family that has failed five times, or one whose
evidence rests on a window tested repeatedly, gets a reason saying so.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional, Sequence

from src.data_access.autoresearch_schema import initialize_autoresearch_schema
from src.domain.autoresearch_models import (
    RESEARCH_METHOD_VERSION, CandidateStatus, CandidateType,
    ResearchCandidate, ResearchConclusion, ResearchHypothesis, _digest,
    utcnow,
)


class PromotionRefused(Exception):
    """
    Phase 23 cannot promote anything, and says so rather than failing
    silently or quietly doing nothing.
    """


def _candidate_id(hypothesis_id: str, conclusion_id: str) -> str:
    return "cand-" + _digest({"h": hypothesis_id, "c": conclusion_id})[:18]


def _code_version() -> str:
    import subprocess
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _review_reason(conclusion: ResearchConclusion) -> str:
    """
    Why a human needs to look at this (§35).

    Written to be useful to a sceptic. The clauses that make a
    candidate look worse are included first, because a review note that
    only lists a candidate's merits is a recommendation wearing a
    review's clothes.
    """
    parts: List[str] = []
    if conclusion.family_experiment_count > 1:
        parts.append(
            "%d hypotheses have been tested in this family, so this result "
            "is one of several attempts rather than a single pre-registered "
            "test" % conclusion.family_experiment_count)
    if conclusion.warnings:
        parts.append("overfitting shapes present: "
                     + ", ".join(conclusion.warnings))
    if conclusion.confidence.value in ("low", "insufficient"):
        parts.append("research confidence is %s" % conclusion.confidence.value)
    if conclusion.sample_size:
        parts.append("measured on %d out-of-sample observations"
                     % conclusion.sample_size)
    parts.append(
        "PROMISING means the criteria fixed before the test were met. It is "
        "not an approval to deploy, and no part of production has changed.")
    return " · ".join(parts)


def propose(conn: sqlite3.Connection, *, hypothesis: ResearchHypothesis,
            conclusion: ResearchConclusion, experiment_id: str = "",
            candidate_type: CandidateType = CandidateType.SIGNAL
            ) -> ResearchCandidate:
    """
    Record a candidate arising from a promising conclusion.

    Refuses anything that is not promising. §54 makes PROMISING the
    only route to a candidate, and allowing a second route would make
    the gate decorative.
    """
    if not conclusion.promising:
        raise PromotionRefused(
            "conclusion %s is not promising (%s), so it cannot become a "
            "candidate. The quality gate is the only route into the "
            "registry (§54)."
            % (conclusion.conclusion_id, conclusion.conclusion.value))

    initialize_autoresearch_schema(conn)
    candidate = ResearchCandidate(
        candidate_id=_candidate_id(hypothesis.hypothesis_id,
                                   conclusion.conclusion_id),
        candidate_type=candidate_type,
        name=hypothesis.statement[:140],
        hypothesis_id=hypothesis.hypothesis_id,
        conclusion_id=conclusion.conclusion_id,
        experiment_id=experiment_id or conclusion.experiment_id,
        status=CandidateStatus.READY_FOR_REVIEW,
        # §56: what this changes FROM. The baseline arm is the honest
        # answer -- a candidate with no base cannot be reproduced.
        base_version="baseline:%s" % hypothesis.baseline,
        changes={"evaluator": hypothesis.evaluator,
                 "parameters": hypothesis.parameters,
                 "condition": hypothesis.condition},
        code_version=_code_version(),
        requires_review=True,
        review_reason=_review_reason(conclusion),
        effect=conclusion.effect)
    candidate.validate()

    conn.execute("""
        INSERT OR REPLACE INTO autoresearch_candidates (
            candidate_id, method_version, candidate_type, name,
            hypothesis_id, conclusion_id, experiment_id, status,
            base_version, changes_json, dataset_snapshot_id, code_version,
            requires_review, review_reason, effect, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (candidate.candidate_id, candidate.method_version,
          candidate.candidate_type.value, candidate.name,
          candidate.hypothesis_id, candidate.conclusion_id,
          candidate.experiment_id, candidate.status.value,
          candidate.base_version,
          json.dumps(candidate.changes, sort_keys=True, default=str),
          candidate.dataset_snapshot_id, candidate.code_version,
          1 if candidate.requires_review else 0, candidate.review_reason,
          candidate.effect, candidate.created_at))
    conn.commit()
    return candidate


def reject(conn: sqlite3.Connection, candidate_id: str, reason: str) -> None:
    """
    Mark a candidate rejected. Nothing is deleted (§42).

    A rejected candidate is evidence about what this system tried and
    declined, which is exactly the kind of record that stops the same
    idea being proposed again in six months.
    """
    initialize_autoresearch_schema(conn)
    conn.execute("""
        UPDATE autoresearch_candidates
        SET status = ?, review_reason = review_reason || ' | rejected: ' || ?
        WHERE candidate_id = ?
    """, (CandidateStatus.REJECTED.value, reason, candidate_id))
    conn.commit()


def promote(conn: sqlite3.Connection, candidate_id: str) -> None:
    """
    Always refuses. This function exists to be findable.

    Somebody looking for how a candidate reaches production will search
    for exactly this name. Finding a refusal with the reason is far
    better than finding nothing and concluding the mechanism is
    somewhere they have not looked yet.
    """
    raise PromotionRefused(
        "Phase 23 cannot promote candidate %s, or any candidate. Promotion "
        "is a human decision made through the Phase 18 gate "
        "(`scripts/promote_model.py`, which requires an approver and a "
        "reason). A research phase that could promote its own findings "
        "would be marking its own homework, which is the failure mode the "
        "research/production boundary exists to prevent (§34)."
        % candidate_id)


def listing(conn: sqlite3.Connection, *, status: Optional[str] = None,
            limit: int = 100) -> List[Dict[str, Any]]:
    initialize_autoresearch_schema(conn)
    sql = """
        SELECT candidate_id, candidate_type, name, hypothesis_id,
               conclusion_id, experiment_id, status, base_version,
               changes_json, code_version, requires_review, review_reason,
               effect, created_at
        FROM autoresearch_candidates
    """
    params: tuple = ()
    if status:
        sql += " WHERE status = ?"
        params = (status,)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params = params + (limit,)
    keys = ("candidate_id", "candidate_type", "name", "hypothesis_id",
            "conclusion_id", "experiment_id", "status", "base_version",
            "changes_json", "code_version", "requires_review",
            "review_reason", "effect", "created_at")
    records = []
    for row in conn.execute(sql, params):
        record = dict(zip(keys, row))
        try:
            record["changes"] = json.loads(record.pop("changes_json") or "{}")
        except (TypeError, ValueError):
            record["changes"] = {}
        records.append(record)
    return records
