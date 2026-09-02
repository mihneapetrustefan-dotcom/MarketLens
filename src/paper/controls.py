"""
src/paper/controls.py
--------------------------
Safety controls: emergency stops, rate limits, circuit breakers
(Phase 13, spec §38, §39, §40, §72, §74).

WHY THESE EXIST IN A PHASE THAT CANNOT LOSE MONEY
-----------------------------------------------------
Nothing here touches a broker, so none of these controls can prevent a
real loss today. They are built now for two reasons.

First, they catch pathological behaviour that would otherwise be
invisible: a strategy emitting ten thousand orders a tick is broken
whether or not the orders are real, and without a rate limit the
symptom is a slow database rather than an alert.

Second, and more importantly — spec §92 requires that the live path,
when it arrives, cannot reach a broker except through this pipeline.
Controls added AFTER a venue is connected are controls someone can be
tempted to skip during an incident. Built now, they are simply part of
how an order gets created.

EVERY REFUSAL NAMES ITSELF
------------------------------
Spec §40 forbids hiding the rejection reason. A blocked order carries
the specific control that blocked it and the numbers behind the
decision, so "why did nothing trade this afternoon" is answerable from
the record rather than by re-running anything.

CONFIGURATION CHANGES ARE RECORDED, NOT JUST APPLIED
--------------------------------------------------------
Spec §72 requires previous value, new value, timestamp, actor and
reason for any change to a critical parameter. `ControlLedger` records
all five, and the audit trail it produces is append-only.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from src.domain.paper_models import (
    ControlAction, PaperAccountStatus, PaperRejectReason, PaperSessionStatus,
    finite_or_none, safe_ratio,
)
from src.paper.clock import require_utc


@dataclass
class ControlDecision:
    """
    Whether an action is permitted, and if not, exactly why.

    Carries a machine-readable reject reason AND a human sentence,
    because the first makes rejections countable and the second makes
    them explicable.
    """
    allowed: bool
    reason: Optional[PaperRejectReason] = None
    detail: str = ""
    control: str = ""

    @classmethod
    def allow(cls) -> "ControlDecision":
        return cls(allowed=True)

    @classmethod
    def block(cls, reason: PaperRejectReason, detail: str,
              control: str) -> "ControlDecision":
        return cls(allowed=False, reason=reason, detail=detail, control=control)


@dataclass
class RateLimits:
    """
    Caps on order creation (spec §40).

    Per-tick and per-day, because they catch different faults: a
    per-tick explosion is usually a strategy bug, while a per-day
    creep is usually a configuration that trades far more than
    intended.
    """
    version: str = "limits-v1"
    max_orders_per_tick: int = 25
    max_orders_per_day: int = 200
    max_cancels_per_day: int = 200

    def check_tick(self, created_this_tick: int) -> ControlDecision:
        if created_this_tick >= self.max_orders_per_tick:
            return ControlDecision.block(
                PaperRejectReason.RATE_LIMITED,
                f"{created_this_tick} orders already created this tick, "
                f"limit {self.max_orders_per_tick}",
                "max_orders_per_tick")
        return ControlDecision.allow()

    def check_day(self, created_today: int) -> ControlDecision:
        if created_today >= self.max_orders_per_day:
            return ControlDecision.block(
                PaperRejectReason.RATE_LIMITED,
                f"{created_today} orders already created today, "
                f"limit {self.max_orders_per_day}",
                "max_orders_per_day")
        return ControlDecision.allow()


@dataclass
class CircuitBreakers:
    """
    Loss and exposure limits that halt trading (spec §39).

    These are SIMULATION safeguards, versioned so a session records
    which thresholds were in force. They stop new exposure; they never
    liquidate. Forced liquidation is a decision with its own risks and
    is not something a paper safeguard should take unilaterally.
    """
    version: str = "breaker-v1"
    daily_loss_limit_pct: Optional[float] = 0.05
    max_drawdown_pct: Optional[float] = 0.20
    max_gross_exposure_pct: Optional[float] = 1.5

    def check(self, equity: Optional[float], day_start_equity: Optional[float],
              peak_equity: Optional[float],
              gross_exposure: Optional[float]) -> ControlDecision:
        if equity is None or equity <= 0:
            return ControlDecision.allow()

        if self.daily_loss_limit_pct is not None and day_start_equity:
            change = safe_ratio(equity - day_start_equity, day_start_equity)
            if change is not None and change <= -abs(self.daily_loss_limit_pct):
                return ControlDecision.block(
                    PaperRejectReason.CIRCUIT_BREAKER,
                    f"down {abs(change):.2%} today, daily limit "
                    f"{abs(self.daily_loss_limit_pct):.2%}",
                    "daily_loss_limit")

        if self.max_drawdown_pct is not None and peak_equity and peak_equity > 0:
            drawdown = safe_ratio(equity - peak_equity, peak_equity)
            if drawdown is not None and drawdown <= -abs(self.max_drawdown_pct):
                return ControlDecision.block(
                    PaperRejectReason.CIRCUIT_BREAKER,
                    f"drawdown {abs(drawdown):.2%} from peak, limit "
                    f"{abs(self.max_drawdown_pct):.2%}",
                    "max_drawdown")

        if self.max_gross_exposure_pct is not None and gross_exposure is not None:
            leverage = safe_ratio(gross_exposure, equity)
            if leverage is not None and leverage > self.max_gross_exposure_pct:
                return ControlDecision.block(
                    PaperRejectReason.CIRCUIT_BREAKER,
                    f"gross exposure {leverage:.2f}x, limit "
                    f"{self.max_gross_exposure_pct:.2f}x",
                    "max_gross_exposure")

        return ControlDecision.allow()


class ControlLedger:
    """
    The gate every new order passes, and the audit trail of every
    intervention.

    Deliberately the single place these checks live. Scattering "is
    trading paused?" through the session would guarantee that one path
    eventually forgets to ask.
    """

    def __init__(self, rate_limits: Optional[RateLimits] = None,
                 breakers: Optional[CircuitBreakers] = None):
        self.rate_limits = rate_limits or RateLimits()
        self.breakers = breakers or CircuitBreakers()
        self.actions: List[ControlAction] = []
        self._orders_this_tick = 0
        self._orders_by_day: Dict[str, int] = {}
        self._cancels_by_day: Dict[str, int] = {}
        #: Instrument- and strategy-level pauses (spec §38).
        self.paused_instruments: set = set()
        self.paused_strategies: set = set()

    # ---------------- counters ----------------

    def begin_tick(self) -> None:
        self._orders_this_tick = 0

    def record_order(self, at: datetime) -> None:
        self._orders_this_tick += 1
        key = require_utc(at, "at").date().isoformat()
        self._orders_by_day[key] = self._orders_by_day.get(key, 0) + 1

    def record_cancel(self, at: datetime) -> None:
        key = require_utc(at, "at").date().isoformat()
        self._cancels_by_day[key] = self._cancels_by_day.get(key, 0) + 1

    def orders_today(self, at: datetime) -> int:
        return self._orders_by_day.get(require_utc(at, "at").date().isoformat(), 0)

    def orders_this_tick(self) -> int:
        return self._orders_this_tick

    # ---------------- the gate ----------------

    def may_create_order(self, *, at: datetime,
                         account_status: PaperAccountStatus,
                         session_status: PaperSessionStatus,
                         is_increase: bool,
                         instrument_id: Optional[str] = None,
                         strategy_id: Optional[str] = None,
                         health_allows: bool = True,
                         health_detail: str = "",
                         equity: Optional[float] = None,
                         day_start_equity: Optional[float] = None,
                         peak_equity: Optional[float] = None,
                         gross_exposure: Optional[float] = None
                         ) -> ControlDecision:
        """
        Every gate an order must clear, in order of severity.

        Reductions are allowed through several gates that block
        increases — reduce-only mode, and the circuit breakers. A
        breaker that also blocked the exit would trap a position it was
        trying to protect, which is the opposite of a safeguard.
        """
        require_utc(at, "at")

        # --- session and account state ---
        if session_status not in (PaperSessionStatus.CREATED,
                                  PaperSessionStatus.RUNNING):
            return ControlDecision.block(
                PaperRejectReason.ACCOUNT_NOT_ACTIVE,
                f"session is {session_status.value}", "session_status")

        if account_status == PaperAccountStatus.EMERGENCY_STOP:
            return ControlDecision.block(
                PaperRejectReason.ACCOUNT_NOT_ACTIVE,
                "account is under emergency stop", "emergency_stop")
        if account_status in (PaperAccountStatus.PAUSED, PaperAccountStatus.CLOSED):
            return ControlDecision.block(
                PaperRejectReason.ACCOUNT_NOT_ACTIVE,
                f"account is {account_status.value}", "account_status")
        if account_status == PaperAccountStatus.REDUCE_ONLY and is_increase:
            return ControlDecision.block(
                PaperRejectReason.ACCOUNT_NOT_ACTIVE,
                "account is reduce-only; reductions remain allowed",
                "reduce_only")

        # --- targeted pauses ---
        if instrument_id and instrument_id in self.paused_instruments and is_increase:
            return ControlDecision.block(
                PaperRejectReason.ACCOUNT_NOT_ACTIVE,
                f"{instrument_id} is paused", "instrument_paused")
        if strategy_id and strategy_id in self.paused_strategies and is_increase:
            return ControlDecision.block(
                PaperRejectReason.ACCOUNT_NOT_ACTIVE,
                f"strategy {strategy_id} is paused", "strategy_paused")

        # --- health (spec §61) ---
        if not health_allows:
            return ControlDecision.block(
                PaperRejectReason.SAFE_MODE,
                health_detail or "pipeline health does not permit new orders",
                "health")

        # --- rate limits ---
        tick_check = self.rate_limits.check_tick(self._orders_this_tick)
        if not tick_check.allowed:
            return tick_check
        day_check = self.rate_limits.check_day(self.orders_today(at))
        if not day_check.allowed:
            return day_check

        # --- circuit breakers, increases only ---
        if is_increase:
            breaker = self.breakers.check(
                equity, day_start_equity, peak_equity, gross_exposure)
            if not breaker.allowed:
                return breaker

        return ControlDecision.allow()

    # ---------------- interventions ----------------

    def _record(self, session_id: str, action: str, at: datetime, actor: str,
                reason: str, previous: Optional[str] = None,
                new: Optional[str] = None) -> ControlAction:
        raw = f"{session_id}|{action}|{at.isoformat()}|{actor}"
        entry = ControlAction(
            action_id=f"ca-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}",
            session_id=session_id, action=action, at=require_utc(at, "at"),
            actor=actor, reason=reason,
            previous_value=previous, new_value=new)
        self.actions.append(entry)
        return entry

    def pause_instrument(self, session_id: str, instrument_id: str, at: datetime,
                         actor: str = "operator", reason: str = "") -> ControlAction:
        self.paused_instruments.add(instrument_id)
        return self._record(session_id, "pause_instrument", at, actor, reason,
                            previous="active", new=f"paused:{instrument_id}")

    def resume_instrument(self, session_id: str, instrument_id: str, at: datetime,
                          actor: str = "operator", reason: str = "") -> ControlAction:
        self.paused_instruments.discard(instrument_id)
        return self._record(session_id, "resume_instrument", at, actor, reason,
                            previous=f"paused:{instrument_id}", new="active")

    def pause_strategy(self, session_id: str, strategy_id: str, at: datetime,
                       actor: str = "operator", reason: str = "") -> ControlAction:
        self.paused_strategies.add(strategy_id)
        return self._record(session_id, "pause_strategy", at, actor, reason,
                            previous="active", new=f"paused:{strategy_id}")

    def resume_strategy(self, session_id: str, strategy_id: str, at: datetime,
                        actor: str = "operator", reason: str = "") -> ControlAction:
        self.paused_strategies.discard(strategy_id)
        return self._record(session_id, "resume_strategy", at, actor, reason,
                            previous=f"paused:{strategy_id}", new="active")

    def record_configuration_change(self, session_id: str, parameter: str,
                                    previous: str, new: str, at: datetime,
                                    actor: str, reason: str) -> ControlAction:
        """Spec §72 — a critical parameter change records all five facts."""
        return self._record(session_id, f"configure:{parameter}", at, actor,
                            reason, previous=previous, new=new)

    def audit_trail(self) -> List[ControlAction]:
        """Append-only. Returned as a copy so a caller cannot rewrite history."""
        return list(self.actions)
