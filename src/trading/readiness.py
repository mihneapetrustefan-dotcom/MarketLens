"""
src/trading/readiness.py
-------------------------------
The pre-order readiness result (Phase 25.9E, §76, §77, §82).

TWO QUESTIONS, NEVER ONE
----------------------------
    SYSTEM_READY       could this cycle's order be sent, mechanically?
    ORDER_AUTHORIZED   has a person allowed orders to be sent?

A cycle can be READY_TO_SUBMIT and not authorised; that is the expected
state of Phase 25.9E and of any day an operator has not opened the paper
gate. Collapsing the two would make "the machinery works" read as "the
system may trade".

THREE KINDS OF NOTHING
--------------------------
A cycle that places no order is one of:

    NORMAL_NO_TRADE   the system worked and there was nothing to do:
                      no deployable model, no signal, risk declined,
                      target already met, market closed
    GOVERNANCE_HOLD   a deliberate human control: trading mode off,
                      kill switch, paper gate closed
    TEMPORARY_BLOCK   something it needs is not usable right now: stale
                      price, broker down, reconciliation mismatch,
                      another runner owns the account
    SYSTEM_ERROR      a stage raised; the cycle does not know its state

A healthy no-model day is the first. Reporting it as the third or fourth
is how a correct system gets "fixed" into an unsafe one.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src.domain.trading_loop_models import (
    BlockReason, CycleResult, CycleStatus, StageOutcome,
)

READY_TO_SUBMIT = "READY_TO_SUBMIT"
NOT_READY = "NOT_READY"

NORMAL_NO_TRADE = "NORMAL_NO_TRADE"
GOVERNANCE_HOLD = "GOVERNANCE_HOLD"
TEMPORARY_BLOCK = "TEMPORARY_BLOCK"
SYSTEM_ERROR = "SYSTEM_ERROR"
READY = "READY"

GOVERNANCE_BLOCKS = {BlockReason.MODE_NOT_PERMITTED, BlockReason.MODE_UNKNOWN,
                     BlockReason.KILL_SWITCH, BlockReason.CONFIGURATION_CHANGED}
NORMAL_BLOCKS = {BlockReason.SESSION_NOT_OPEN}

DIMENSIONS = ("market", "data", "model", "signal", "portfolio", "risk",
              "account", "reconciliation", "execution", "broker_session",
              "governance")


@dataclass
class ExecutionReadiness:
    cycle_id: str
    verdict: str = NOT_READY
    classification: str = NORMAL_NO_TRADE
    system_ready: bool = False
    order_authorized: bool = False
    #: Requests validated end to end and held at the boundary.
    requests_ready: int = 0
    dimensions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    authorization_missing: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _stage(result: CycleResult, name: str):
    for stage in result.stages:
        if stage.stage.value == name:
            return stage
    return None


def assess(result: CycleResult, context: Dict[str, Any], *,
           allow_paper_orders: bool, dry_run: bool,
           pre_submission_only: bool, may_submit: bool) -> ExecutionReadiness:
    """One verdict per cycle, from what the cycle actually established."""
    readiness = ExecutionReadiness(cycle_id=result.cycle_id)
    dims = readiness.dimensions
    blocks = {b.reason for b in result.blocks}

    def put(name: str, ok: Optional[bool], detail: str) -> None:
        dims[name] = {"ok": ok, "detail": detail}

    market = _stage(result, "market_data")
    put("data", not ({BlockReason.STALE_MARKET_DATA, BlockReason.NO_MARKET_DATA}
                     & blocks) and market is not None,
        market.detail if market else "not reached")
    put("market", BlockReason.SESSION_NOT_OPEN not in blocks,
        "session open" if BlockReason.SESSION_NOT_OPEN not in blocks
        else "session not open")
    deployable = context.get("deployable_models") or {}
    put("model", any(deployable.values()) if deployable else False,
        f"{sum(1 for v in deployable.values() if v)} deployable of "
        f"{len(deployable)} evaluated")
    put("signal", result.signals_eligible > 0,
        f"{result.signals_eligible} eligible of {result.signals_seen}")
    portfolio = _stage(result, "portfolio")
    put("portfolio", None if portfolio is None else
        portfolio.outcome is not StageOutcome.FAILED,
        portfolio.detail if portfolio else "not reached")
    risk = _stage(result, "risk")
    put("risk", None if risk is None else risk.outcome is StageOutcome.RAN,
        risk.detail if risk else "not reached")
    put("account", BlockReason.ACCOUNT_STATE_UNKNOWN not in blocks,
        "broker account state read"
        if BlockReason.ACCOUNT_STATE_UNKNOWN not in blocks else "unknown")
    put("reconciliation", context.get("reconciliation_clean"),
        "clean" if context.get("reconciliation_clean")
        else "not clean or not run")
    put("broker_session",
        not ({BlockReason.BROKER_DISCONNECTED, BlockReason.BROKER_UNHEALTHY}
             & blocks) and bool(context.get("broker_usable", True)),
        "usable" if context.get("broker_usable", True) else "not usable")
    put("execution", readiness.requests_ready > 0 or None,
        "")
    put("governance", not (GOVERNANCE_BLOCKS & blocks),
        "mode permits paper" if not (GOVERNANCE_BLOCKS & blocks)
        else "; ".join(sorted(b.value for b in GOVERNANCE_BLOCKS & blocks)))

    readiness.requests_ready = int(context.get("requests_ready") or 0)
    dims["execution"] = {
        "ok": readiness.requests_ready > 0 if context.get("reached_boundary")
        else None,
        "detail": (f"{readiness.requests_ready} request(s) validated and held "
                   f"at the boundary" if context.get("reached_boundary")
                   else "boundary not reached")}

    failed = [s for s in result.stages if s.outcome is StageOutcome.FAILED]
    if result.status is CycleStatus.FAILED or failed:
        readiness.classification = SYSTEM_ERROR
        readiness.reasons = [f"{s.stage.value}: {s.detail}" for s in failed] or \
            [result.detail]
    elif blocks - NORMAL_BLOCKS - GOVERNANCE_BLOCKS:
        readiness.classification = TEMPORARY_BLOCK
        readiness.reasons = [f"{b.reason.value}: {b.detail}" for b in result.blocks
                             if b.reason not in NORMAL_BLOCKS | GOVERNANCE_BLOCKS]
    elif GOVERNANCE_BLOCKS & blocks:
        readiness.classification = GOVERNANCE_HOLD
        readiness.reasons = [f"{b.reason.value}: {b.detail}" for b in result.blocks]
    elif context.get("reached_boundary") and readiness.requests_ready > 0:
        readiness.classification = READY
        readiness.verdict = READY_TO_SUBMIT
        readiness.system_ready = True
    else:
        readiness.classification = NORMAL_NO_TRADE
        readiness.reasons = [context.get("no_trade_reason")
                             or _no_trade_reason(result, dims)]

    missing = []
    if not allow_paper_orders:
        missing.append("paper ordering gate not opened (--allow-paper-orders)")
    if dry_run:
        missing.append("dry run")
    if pre_submission_only:
        missing.append("Phase 25.9E pre-submission mode: submission is "
                       "structurally disabled")
    if not may_submit:
        missing.append("IBKR paper ordering not enabled or broker not connected")
    readiness.authorization_missing = missing
    readiness.order_authorized = readiness.system_ready and not missing
    return readiness


def _no_trade_reason(result: CycleResult, dims: Dict[str, Dict[str, Any]]) -> str:
    if result.signals_seen == 0:
        return "no live signal"
    if not dims.get("model", {}).get("ok"):
        return "no deployable model; signals are not eligible"
    if result.signals_eligible == 0:
        return "no signal is eligible"
    risk = dims.get("risk", {})
    if risk.get("ok") is False:
        return "risk declined: " + str(risk.get("detail"))
    return "no change to the book was required"
