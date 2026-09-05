"""
src/modeling/promotion.py
-------------------------------
Making a model ACTIVE, and refusing to.

THE ONE RULE
----------------
A model becomes production-facing only when a **named person** says so,
with a **stated reason**, about a model that **passes the gate**.

All three are required and none has a default. There is no
`--force`, no `auto=True`, no environment variable, and no code path
anywhere in the repository that calls `promote()` on a schedule. Phase
18's instruction is that promotion remains controlled, and the way to
guarantee that is for the promoting function to be unable to run
without a human in its arguments.

WHY THE GATE IS NOT RE-IMPLEMENTED HERE
-------------------------------------------
`promote()` asks `selection.eligibility()`, which asks
`ModelEvaluation.is_deployable`. Three layers, one threshold, defined
once in Phase 9. A second copy of the rule would eventually disagree
with the first, and the disagreement would be discovered by a model
being active that the evaluator says should not be.

WHAT PROMOTION DOES TO THE PREVIOUS CHAMPION
------------------------------------------------
Retires it, in the same transaction, recording the reason. Two ACTIVE
models for one label would make "which model is production" ambiguous
at exactly the moment it matters most, and inference's tie-break
(newest first) would resolve it silently.

RETIRED IS NOT DELETED
--------------------------
A retired model keeps its row, its parameters and its predictions
forever. `ModelStatus` says so in its own docstring: *a prediction
whose model has vanished cannot be audited.*
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from src.data_access.model_promotion_schema import (
    initialize_model_promotion_schema,
)
from src.domain.model_models import ModelStatus
from src.modeling.selection import ModelEligibility, eligibility


class PromotionRefused(Exception):
    """
    Raised when a model may not be promoted.

    Carries the eligibility verdict so the refusal states which
    criterion failed rather than merely that one did.
    """

    def __init__(self, message: str, verdict: Optional[ModelEligibility] = None):
        super().__init__(message)
        self.verdict = verdict


@dataclass
class PromotionRecord:
    """One promotion or demotion, as written."""
    promotion_id: str
    trained_model_id: str
    action: str
    approved_by: str
    reason: str
    from_status: str
    to_status: str
    promoted_at: datetime


def code_version() -> str:
    """
    The commit the promotion was made against.

    Best effort: a promotion from a tarball with no git metadata still
    records everything else rather than failing. An empty string is
    honest; a fabricated hash would not be.
    """
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _write(conn: sqlite3.Connection, verdict: ModelEligibility, *,
           action: str, approved_by: str, reason: str,
           from_status: str, to_status: str,
           now: datetime) -> PromotionRecord:
    promotion_id = f"mp-{uuid.uuid4().hex[:20]}"
    conn.execute("""
        INSERT INTO model_promotions (
            promotion_id, trained_model_id, model_qualified_id, label_name,
            action, from_status, to_status, approved_by, reason,
            evaluation_id, metrics_json, beats_all_baselines,
            effective_sample, deployable, dataset_version,
            feature_set_version, label_version, code_version, promoted_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (promotion_id, verdict.trained_model_id, verdict.model_qualified_id,
          verdict.label_name, action, from_status, to_status,
          approved_by, reason, verdict.evaluation_id,
          json.dumps(verdict.metrics, sort_keys=True),
          None if verdict.beats_all_baselines is None else int(verdict.beats_all_baselines),
          verdict.effective_sample_size,
          None if verdict.deployable is None else int(verdict.deployable),
          verdict.dataset_version, verdict.feature_set_version,
          verdict.label_version, code_version(), now.isoformat()))
    conn.execute("UPDATE trained_models SET status = ? WHERE trained_model_id = ?",
                 (to_status, verdict.trained_model_id))
    return PromotionRecord(
        promotion_id=promotion_id, trained_model_id=verdict.trained_model_id,
        action=action, approved_by=approved_by, reason=reason,
        from_status=from_status, to_status=to_status, promoted_at=now)


def promote(conn: sqlite3.Connection, trained_model_id: str, *,
            approved_by: str, reason: str,
            now: Optional[datetime] = None) -> List[PromotionRecord]:
    """
    Make a model ACTIVE. Refuses unless it passes the gate.

    `approved_by` and `reason` are keyword-only and have no defaults:
    a caller cannot promote by accident, and cannot promote anonymously.

    Returns every record written — the promotion, plus a retirement
    record for each model it displaced.
    """
    now = now or datetime.now(timezone.utc)
    initialize_model_promotion_schema(conn)

    if not (approved_by or "").strip():
        raise PromotionRefused(
            "A promotion needs a named approver. An unattributed "
            "promotion is an automatic one wearing a person's clothes.")
    if not (reason or "").strip():
        raise PromotionRefused(
            "A promotion needs a stated reason. 'It was the newest' is "
            "the failure this gate exists to prevent, and writing it "
            "down is what makes it visible.")

    verdict = eligibility(conn, trained_model_id)
    if not verdict.model_qualified_id and verdict.reasons:
        raise PromotionRefused(
            f"No trained model {trained_model_id!r} exists.", verdict)

    if verdict.status == ModelStatus.ACTIVE:
        raise PromotionRefused(
            f"{trained_model_id} is already ACTIVE. Promoting it again "
            f"would add a record of a decision nobody made.", verdict)

    if not verdict.may_be_promoted:
        detail = "; ".join(verdict.reasons) or "no evaluation evidence"
        raise PromotionRefused(
            f"{trained_model_id} does not pass the quality gate: {detail}.\n"
            f"  verdict: {verdict.verdict}\n"
            f"  This is not a permission problem. The evaluator is "
            f"reporting that the model has not shown an edge, and "
            f"promoting it anyway would make the gate decorative.",
            verdict)

    records: List[PromotionRecord] = []

    # Retire the incumbent for this label FIRST, so there is never a
    # moment — even inside the transaction — when two models are active.
    incumbents = conn.execute("""
        SELECT trained_model_id FROM trained_models
        WHERE status = ? AND label_name = ? AND trained_model_id != ?
    """, (ModelStatus.ACTIVE.value, verdict.label_name,
          verdict.trained_model_id)).fetchall()
    for (incumbent_id,) in incumbents:
        incumbent = eligibility(conn, incumbent_id)
        records.append(_write(
            conn, incumbent, action="demote", approved_by=approved_by,
            reason=f"superseded by {verdict.trained_model_id}: {reason}",
            from_status=ModelStatus.ACTIVE.value,
            to_status=ModelStatus.RETIRED.value, now=now))

    records.append(_write(
        conn, verdict, action="promote", approved_by=approved_by,
        reason=reason, from_status=verdict.status.value,
        to_status=ModelStatus.ACTIVE.value, now=now))
    conn.commit()
    return records


def demote(conn: sqlite3.Connection, trained_model_id: str, *,
           approved_by: str, reason: str,
           to_status: ModelStatus = ModelStatus.DEGRADED,
           now: Optional[datetime] = None) -> PromotionRecord:
    """
    Withdraw a model from production.

    Deliberately NOT gated: taking something out of production must
    never be harder than putting it in. DEGRADED by default rather than
    RETIRED — "the evidence turned against it" and "something better
    replaced it" are different events and should not share a word.
    """
    now = now or datetime.now(timezone.utc)
    initialize_model_promotion_schema(conn)

    if not (approved_by or "").strip():
        raise PromotionRefused("A demotion needs a named approver.")
    if not (reason or "").strip():
        raise PromotionRefused("A demotion needs a stated reason.")

    verdict = eligibility(conn, trained_model_id)
    if not verdict.model_qualified_id and verdict.reasons:
        raise PromotionRefused(
            f"No trained model {trained_model_id!r} exists.", verdict)

    record = _write(conn, verdict, action="demote", approved_by=approved_by,
                    reason=reason, from_status=verdict.status.value,
                    to_status=to_status.value, now=now)
    conn.commit()
    return record


def history(conn: sqlite3.Connection,
            trained_model_id: Optional[str] = None) -> List[dict]:
    """Every promotion and demotion, newest first."""
    initialize_model_promotion_schema(conn)
    sql = """
        SELECT promotion_id, trained_model_id, model_qualified_id, action,
               from_status, to_status, approved_by, reason, evaluation_id,
               deployable, code_version, promoted_at
        FROM model_promotions
    """
    params: tuple = ()
    if trained_model_id:
        sql += " WHERE trained_model_id = ?"
        params = (trained_model_id,)
    sql += " ORDER BY promoted_at DESC"
    keys = ("promotion_id", "trained_model_id", "model_qualified_id", "action",
            "from_status", "to_status", "approved_by", "reason",
            "evaluation_id", "deployable", "code_version", "promoted_at")
    return [dict(zip(keys, row)) for row in conn.execute(sql, params)]
