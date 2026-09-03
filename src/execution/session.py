"""
src/execution/session.py
-----------------------------
Trading sessions and daily controls (Phase 16, spec §42, §43, §44,
§45, §54, §55, §56, §86, §87).

WHAT A SESSION IS
---------------------
One bounded period of execution under one fixed configuration, with a
named operator, an environment, a capital limit and a start and end.
Everything executed belongs to exactly one, which is what makes "what
was the system doing when this happened" answerable.

CONFIGURATION IS FROZEN AT START
------------------------------------
`SessionConfiguration` is fingerprinted when the session opens, and
changing it while the session runs is refused (spec §56). A trade must
be traceable to the exact model, strategy, risk and execution settings
that produced it (§87), and a configuration that could drift mid-session
would make that traceability a guess.

Amending settings therefore means stopping the session and opening a
new one — which is an audit record rather than a silent change.

THE LIFECYCLE IS ENFORCED, NOT SUGGESTED
--------------------------------------------
START, PAUSE, RESUME, STOP, EMERGENCY_STOP. Illegal moves raise rather
than being ignored, because a caller that thinks it resumed a stopped
session has a bug and swallowing it leaves the bug in place with
execution possibly enabled.

`EMERGENCY_STOP` is terminal and cannot be resumed. That asymmetry is
deliberate: a routine pause is reversible by whoever paused it, and an
emergency stop should require a human to open a new session and say
why — because the reason for the stop has not been examined merely
because the market calmed down.

NOTHING IS EVER DELETED
---------------------------
Spec §43. A stopped session keeps its orders, its P&L, its risk events
and its reconciliation record. The end of a session is a boundary, not
an erasure.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.domain.broker_models import ExecutionEnvironment, finite_or_none
from src.execution.governance import ExecutionLevel
from src.execution.limits import DayState, LimitBreach


def _require_utc(value: Optional[datetime], name: str) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be in UTC")
    return value


class SessionState(str, Enum):
    CREATED = "created"
    VALIDATING = "validating"
    ACTIVE = "active"
    PAUSED = "paused"
    STOPPED = "stopped"
    EMERGENCY_STOPPED = "emergency_stopped"
    FAILED = "failed"

    @property
    def accepts_orders(self) -> bool:
        return self is SessionState.ACTIVE

    @property
    def is_terminal(self) -> bool:
        return self in (SessionState.STOPPED, SessionState.EMERGENCY_STOPPED,
                        SessionState.FAILED)

    @property
    def is_resumable(self) -> bool:
        """
        Only a routine pause resumes.

        An emergency stop is terminal on purpose: the reason for it has
        not been examined merely because the market calmed down, so
        continuing requires a human to open a new session and say why.
        """
        return self is SessionState.PAUSED


class SessionAction(str, Enum):
    START = "start"
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    EMERGENCY_STOP = "emergency_stop"


#: The lifecycle. Anything absent is refused.
_TRANSITIONS: Dict[SessionState, Dict[SessionAction, SessionState]] = {
    SessionState.CREATED: {
        SessionAction.START: SessionState.ACTIVE,
        SessionAction.STOP: SessionState.STOPPED,
        SessionAction.EMERGENCY_STOP: SessionState.EMERGENCY_STOPPED,
    },
    SessionState.VALIDATING: {
        SessionAction.START: SessionState.ACTIVE,
        SessionAction.STOP: SessionState.STOPPED,
        SessionAction.EMERGENCY_STOP: SessionState.EMERGENCY_STOPPED,
    },
    SessionState.ACTIVE: {
        SessionAction.PAUSE: SessionState.PAUSED,
        SessionAction.STOP: SessionState.STOPPED,
        SessionAction.EMERGENCY_STOP: SessionState.EMERGENCY_STOPPED,
    },
    SessionState.PAUSED: {
        SessionAction.RESUME: SessionState.ACTIVE,
        SessionAction.STOP: SessionState.STOPPED,
        SessionAction.EMERGENCY_STOP: SessionState.EMERGENCY_STOPPED,
    },
}


class SessionTransitionError(Exception):
    """Raised on an illegal session move."""


@dataclass
class SessionConfiguration:
    """
    Everything that shapes what a session may do (spec §56, §87).

    Versions are recorded rather than referenced, so a trade from six
    months ago still says which model produced it even if that model
    has since been retrained.
    """
    broker_id: str = "ibkr"
    account_id: str = ""
    environment: ExecutionEnvironment = ExecutionEnvironment.PAPER
    level: ExecutionLevel = ExecutionLevel.PAPER

    strategies: Tuple[str, ...] = ()
    capital_limit: Optional[float] = None
    max_order_notional: Optional[float] = None
    daily_loss_limit: Optional[float] = None
    max_open_positions: Optional[int] = None

    # version identity (spec §54, §55)
    model_version: Optional[str] = None
    strategy_version: Optional[str] = None
    feature_version: Optional[str] = None
    signal_version: Optional[str] = None
    risk_config_version: str = "v1"
    execution_config_version: str = "exec-v1"
    code_version: str = "phase16-v1"

    execution_policy: str = "market"
    notes: str = ""

    def __post_init__(self):
        if self.environment.is_real_money:
            raise ValueError(
                "a session cannot be configured for a real-money environment; "
                "no adapter in this repository accepts one")

    def fingerprint(self) -> str:
        """A stable hash of every field that changes behaviour."""
        parts = [
            self.broker_id, self.account_id, self.environment.value,
            str(int(self.level)), ",".join(sorted(self.strategies)),
            str(self.capital_limit), str(self.max_order_notional),
            str(self.daily_loss_limit), str(self.max_open_positions),
            str(self.model_version), str(self.strategy_version),
            str(self.feature_version), str(self.signal_version),
            self.risk_config_version, self.execution_config_version,
            self.code_version, self.execution_policy,
        ]
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:20]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "broker_id": self.broker_id, "account_id": self.account_id,
            "environment": self.environment.value, "level": int(self.level),
            "level_label": self.level.label,
            "strategies": list(self.strategies),
            "capital_limit": self.capital_limit,
            "max_order_notional": self.max_order_notional,
            "daily_loss_limit": self.daily_loss_limit,
            "max_open_positions": self.max_open_positions,
            "model_version": self.model_version,
            "strategy_version": self.strategy_version,
            "feature_version": self.feature_version,
            "signal_version": self.signal_version,
            "risk_config_version": self.risk_config_version,
            "execution_config_version": self.execution_config_version,
            "code_version": self.code_version,
            "execution_policy": self.execution_policy,
            "fingerprint": self.fingerprint(),
        }


@dataclass
class SessionEvent:
    """One recorded action on a session. Append-only."""
    session_id: str
    sequence: int
    at: Optional[datetime]
    action: str
    actor: str
    from_state: Optional[str] = None
    to_state: Optional[str] = None
    reason: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")
        if not self.actor:
            raise ValueError("a session event must name an actor")


@dataclass
class PreflightCheck:
    """One start-of-session validation (spec §44)."""
    name: str
    passed: bool
    detail: str = ""
    #: False when the check could not be performed at all.
    measured: bool = True

    @property
    def blocks(self) -> bool:
        return not self.passed or not self.measured


@dataclass
class SessionSummary:
    """What a session did, produced at its end (spec §45, §86)."""
    session_id: str
    at: Optional[datetime] = None
    orders_submitted: int = 0
    orders_filled: int = 0
    orders_rejected: int = 0
    orders_cancelled: int = 0
    orders_unknown: int = 0
    fills: int = 0
    risk_blocks: int = 0
    limit_blocks: int = 0
    errors: int = 0
    alerts: int = 0
    realized_pnl: float = 0.0
    unrealized_pnl: Optional[float] = None
    fees: float = 0.0
    gross_exposure: Optional[float] = None
    open_positions: int = 0
    open_orders: int = 0
    reconciliation_clean: Optional[bool] = None
    reconciliation_mismatches: int = 0
    configuration_fingerprint: str = ""

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")

    @property
    def total_pnl(self) -> Optional[float]:
        if self.unrealized_pnl is None:
            return None
        return self.realized_pnl + self.unrealized_pnl

    @property
    def is_clean_close(self) -> bool:
        """
        Whether the session ended without anything unexplained.

        Open orders and unknown states both count against it: a session
        that ended with an order still working at the venue has not
        finished, it has stopped.
        """
        return (self.reconciliation_clean is True
                and self.orders_unknown == 0
                and self.open_orders == 0)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "at": self.at.isoformat() if self.at else None,
            "orders_submitted": self.orders_submitted,
            "orders_filled": self.orders_filled,
            "orders_rejected": self.orders_rejected,
            "orders_cancelled": self.orders_cancelled,
            "orders_unknown": self.orders_unknown,
            "fills": self.fills, "risk_blocks": self.risk_blocks,
            "limit_blocks": self.limit_blocks, "errors": self.errors,
            "alerts": self.alerts,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "total_pnl": self.total_pnl, "fees": self.fees,
            "gross_exposure": self.gross_exposure,
            "open_positions": self.open_positions,
            "open_orders": self.open_orders,
            "reconciliation_clean": self.reconciliation_clean,
            "reconciliation_mismatches": self.reconciliation_mismatches,
            "configuration_fingerprint": self.configuration_fingerprint,
            "clean_close": self.is_clean_close,
        }


@dataclass
class TradingSession:
    """
    One bounded period of execution under one frozen configuration.

    The configuration is fingerprinted at construction. `amend` refuses
    while the session is active, because a trade must be traceable to
    the exact settings that produced it.
    """
    session_id: str
    config: SessionConfiguration
    operator: str
    created_at: Optional[datetime] = None
    state: SessionState = SessionState.CREATED
    started_at: Optional[datetime] = None
    paused_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    termination_reason: str = ""
    approval_id: Optional[str] = None
    day: DayState = field(default_factory=DayState)
    events: List[SessionEvent] = field(default_factory=list)
    preflight: List[PreflightCheck] = field(default_factory=list)
    summary: Optional[SessionSummary] = None
    _fingerprint: str = ""

    def __post_init__(self):
        for name in ("created_at", "started_at", "paused_at", "ended_at"):
            setattr(self, name, _require_utc(getattr(self, name), name))
        if not self.operator:
            raise ValueError("a session must name its operator")
        self._fingerprint = self.config.fingerprint()

    # ---------------- identity ----------------

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    @property
    def configuration_drifted(self) -> bool:
        """
        True if the configuration object was mutated after start.

        Checked rather than trusted: the fingerprint is recomputed and
        compared, so a mutation reaches a report even if it bypassed
        `amend`.
        """
        return self.config.fingerprint() != self._fingerprint

    def amend(self, actor: str, now: datetime, reason: str,
              **changes: Any) -> None:
        """
        Change configuration. Refused while the session is active.

        Amending means stopping and opening a new session, which is an
        audit record rather than a silent change (spec §56).
        """
        if self.state.accepts_orders:
            raise SessionTransitionError(
                "configuration cannot change while a session is active; "
                "stop the session and open a new one")
        if not actor or not reason:
            raise ValueError("an amendment requires an actor and a reason")
        for key, value in changes.items():
            if not hasattr(self.config, key):
                raise ValueError(f"unknown configuration field {key!r}")
            setattr(self.config, key, value)
        self._fingerprint = self.config.fingerprint()
        self._record("configuration_amended", now, actor, reason=reason,
                     payload={"changes": {k: str(v) for k, v in changes.items()}})

    # ---------------- lifecycle ----------------

    def _record(self, action: str, at: Optional[datetime], actor: str,
                from_state: Optional[SessionState] = None,
                to_state: Optional[SessionState] = None,
                reason: str = "", payload: Optional[Dict[str, Any]] = None
                ) -> SessionEvent:
        event = SessionEvent(
            session_id=self.session_id, sequence=len(self.events) + 1, at=at,
            action=action, actor=actor,
            from_state=from_state.value if from_state else None,
            to_state=to_state.value if to_state else None,
            reason=reason, payload=payload or {})
        self.events.append(event)
        return event

    def apply(self, action: SessionAction, actor: str, now: datetime,
              reason: str = "") -> SessionEvent:
        """
        Move the session, or raise explaining why not.

        Raises rather than returning False: a caller that believes it
        resumed a stopped session has a bug, and swallowing it leaves
        the bug in place with execution possibly enabled.
        """
        if not actor:
            raise ValueError("a session action requires an actor")
        allowed = _TRANSITIONS.get(self.state, {})
        if action not in allowed:
            raise SessionTransitionError(
                f"cannot {action.value} a session that is {self.state.value}"
                + ("; an emergency stop is terminal and requires a new session"
                   if self.state is SessionState.EMERGENCY_STOPPED else ""))

        if action in (SessionAction.STOP, SessionAction.EMERGENCY_STOP) and not reason:
            raise ValueError(f"{action.value} requires a reason")

        previous = self.state
        self.state = allowed[action]

        if action is SessionAction.START:
            self.started_at = _require_utc(now, "now")
        elif action is SessionAction.PAUSE:
            self.paused_at = _require_utc(now, "now")
        elif action is SessionAction.RESUME:
            self.paused_at = None
        else:
            self.ended_at = _require_utc(now, "now")
            self.termination_reason = reason

        return self._record(action.value, now, actor, previous, self.state,
                            reason)

    # ---------------- preflight ----------------

    def run_preflight(self, checks: Sequence[PreflightCheck],
                      actor: str, now: datetime) -> bool:
        """
        Record start-of-session validation (spec §44).

        Every check must pass AND have been measured. An unmeasured
        check blocks: not knowing whether the account reconciles is not
        the same as knowing that it does.
        """
        self.preflight = list(checks)
        blocking = [c for c in self.preflight if c.blocks]
        self._record("preflight", now, actor,
                     reason=("passed" if not blocking else
                             "blocked on " + ", ".join(c.name for c in blocking)),
                     payload={"checks": [
                         {"name": c.name, "passed": c.passed,
                          "measured": c.measured, "detail": c.detail}
                         for c in self.preflight]})
        return not blocking

    @property
    def preflight_passed(self) -> bool:
        return bool(self.preflight) and not any(c.blocks for c in self.preflight)

    # ---------------- gate ----------------

    def may_submit(self, now: datetime) -> Tuple[bool, str]:
        """
        Whether this session may place an order right now.

        Five conditions, all required. Written as one method so no
        caller can check three of five and believe it has checked.
        """
        if not self.state.accepts_orders:
            return False, f"the session is {self.state.value}"
        if not self.preflight_passed:
            return False, "start-of-session validation has not passed"
        if self.configuration_drifted:
            return False, ("the session configuration changed after it started; "
                           "trades could not be attributed to it")
        if self.config.environment.is_real_money:
            return False, "real-money environments are refused"
        if self.config.level.requires_approval and not self.approval_id:
            return False, (f"level {int(self.config.level)} requires an "
                           f"approval and none is attached")
        return True, "the session permits execution"

    # ---------------- close ----------------

    def close(self, summary: SessionSummary) -> SessionSummary:
        summary.configuration_fingerprint = self.fingerprint
        self.summary = summary
        return summary

    def as_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "state": self.state.value,
            "operator": self.operator,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "termination_reason": self.termination_reason,
            "approval_id": self.approval_id,
            "configuration": self.config.as_dict(),
            "configuration_drifted": self.configuration_drifted,
            "preflight_passed": self.preflight_passed,
            "preflight": [{"name": c.name, "passed": c.passed,
                           "measured": c.measured, "detail": c.detail}
                          for c in self.preflight],
            "events": len(self.events),
            "summary": self.summary.as_dict() if self.summary else None,
        }


def new_session(config: SessionConfiguration, operator: str, now: datetime,
                approval_id: Optional[str] = None) -> TradingSession:
    return TradingSession(
        session_id=f"sess-{uuid.uuid4().hex[:16]}", config=config,
        operator=operator, created_at=now, approval_id=approval_id)


def standard_preflight(*, broker_connected: Optional[bool] = None,
                       account_available: Optional[bool] = None,
                       market_data_live: Optional[bool] = None,
                       reconciliation_clean: Optional[bool] = None,
                       risk_available: Optional[bool] = None,
                       capital_configured: Optional[bool] = None,
                       no_unknown_orders: Optional[bool] = None,
                       kill_switch_off: Optional[bool] = None
                       ) -> List[PreflightCheck]:
    """
    The start-of-session checks spec §44 asks for.

    Anything passed as None is recorded as UNMEASURED, which blocks —
    so a caller that forgets to supply a check cannot accidentally
    start a session that skipped it.
    """
    def check(name: str, value: Optional[bool], detail: str) -> PreflightCheck:
        if value is None:
            return PreflightCheck(name, passed=False, measured=False,
                                  detail="not measured")
        return PreflightCheck(name, passed=value,
                              detail="" if value else detail)

    return [
        check("broker_connected", broker_connected,
              "the broker session is not usable"),
        check("account_available", account_available,
              "the account could not be read"),
        check("market_data_live", market_data_live,
              "market data is not live enough to trade on"),
        check("reconciliation_clean", reconciliation_clean,
              "an unresolved reconciliation mismatch is outstanding"),
        check("risk_available", risk_available,
              "the risk engine could not be consulted"),
        check("capital_configured", capital_configured,
              "capital limits are not configured"),
        check("no_unknown_orders", no_unknown_orders,
              "orders in an unknown state are outstanding"),
        check("kill_switch_off", kill_switch_off,
              "the emergency stop is active"),
    ]
