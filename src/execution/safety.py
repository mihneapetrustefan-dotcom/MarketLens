"""
src/execution/safety.py
----------------------------
Execution safety controls and the kill switch (Phase 14, spec §28, §29,
§32, §46, §58, §64).

THE ONE RULE THIS MODULE EXISTS FOR
---------------------------------------
No code path in this phase may place a real-money order.

That is enforced three times over, at three different layers, because
a single check is a single edit away from being wrong:

  1. `ExecutionEnvironment.LIVE` cannot be attached to a `Broker` that
     claims to be implemented, or to a `BrokerAccount` at all — the
     domain types refuse construction.
  2. `ExecutionSafety.allow_real_orders` is a read-only property that
     returns False. There is no setter, no constructor argument and no
     environment variable that changes it.
  3. `ExecutionSafety.check` refuses `LIVE` before it looks at
     anything else, so even a broker that somehow claimed the
     environment would be stopped at the gate.

A configuration flag alone would not be enough. Flags get flipped by
people who are debugging something else at two in the morning.

WHY THE FLAGS ARE LAYERED
-----------------------------
`execution_enabled` is the system-wide switch. Then the environment,
then the broker, then the account, then the strategy, then the
portfolio. An operator disabling one strategy should not have to
disable the system, and an operator disabling the system should not
have to remember every strategy. The check runs outermost-first so the
reported reason is the broadest one that applies, which is the one
worth acting on.

THE KILL SWITCH FAILS SAFE
------------------------------
When it is active, no new order may be submitted — including one
already validated and waiting, and including a retry of a submission
that timed out. What it deliberately does NOT do is cancel working
orders or delete anything: history stays intact, and reconciliation
keeps running, because the moment you most need to know what you hold
is the moment you hit the switch.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.domain.broker_models import (
    AuditEvent, ExecutionEnvironment, ExecutionPermission,
    ExecutionRejectCode, ValidationResult, explain,
)


#: The environment variable an operator would reach for. It is read so
#: that setting it produces a LOUD refusal rather than silence — the
#: worst outcome would be someone setting it, seeing nothing happen,
#: and assuming it worked.
REAL_ORDERS_ENV = "MARKETLENS_ALLOW_REAL_ORDERS"


class RealMoneyExecutionDisabled(Exception):
    """
    Raised when anything attempts a real-money path.

    Never caught inside this package. It exists to terminate a code
    path that should not exist, loudly, wherever it is attempted.
    """


@dataclass
class SafetySwitches:
    """
    The operator-controlled flags (spec §28).

    Note what is absent: there is no `live_execution_enabled` field.
    Real-money execution is not a flag in this phase, so there is
    nothing here to set to True.
    """
    execution_enabled: bool = True
    paper_execution_enabled: bool = True
    demo_execution_enabled: bool = False
    emergency_stop: bool = False

    #: Per-entity switches. Absent means enabled; an explicit False
    #: disables. Stored as dicts so an operator can disable one
    #: strategy without enumerating all the others.
    brokers: Dict[str, bool] = field(default_factory=dict)
    accounts: Dict[str, bool] = field(default_factory=dict)
    strategies: Dict[str, bool] = field(default_factory=dict)
    portfolios: Dict[str, bool] = field(default_factory=dict)

    def enabled_for(self, bucket: Dict[str, bool], key: Optional[str]) -> bool:
        if not key:
            return True
        return bucket.get(key, True)


@dataclass
class SafetyVerdict:
    """Why execution was permitted or refused, with the code to record."""
    permitted: bool
    code: Optional[ExecutionRejectCode] = None
    detail: str = ""

    @property
    def explanation(self) -> str:
        if self.permitted:
            return "Execution controls permit this order."
        return explain(self.code, self.detail) if self.code else "Refused."


class ExecutionSafety:
    """
    The gate every order passes before anything else happens.

    Deliberately has no dependency on brokers, adapters or the
    database. It answers one question — may this proceed — and the
    answer must not be able to change because some other subsystem is
    in an unusual state.
    """

    def __init__(self, switches: Optional[SafetySwitches] = None,
                 actor: str = "system"):
        self.switches = switches or SafetySwitches()
        self.actor = actor
        self.audit: List[AuditEvent] = []
        self._kill_switch_reason: str = ""
        self._kill_switch_at: Optional[datetime] = None

    # ---------------- the invariant ----------------

    @property
    def allow_real_orders(self) -> bool:
        """
        Permanently False (spec §64).

        A property with no setter rather than a field, so that
        `safety.allow_real_orders = True` is a runtime error rather
        than a working line of code.
        """
        return False

    @staticmethod
    def real_orders_requested_by_environment() -> bool:
        """Whether someone set the environment variable, so we can say so."""
        raw = (os.environ.get(REAL_ORDERS_ENV) or "").strip().lower()
        return raw in ("1", "true", "yes", "on")

    def assert_not_real_money(self, environment: ExecutionEnvironment) -> None:
        """
        The hard stop. Raises rather than returning a verdict.

        Called at the top of every path that could conceivably reach a
        venue. A caller cannot accidentally ignore its result, because
        it does not return one.
        """
        if environment.is_real_money:
            raise RealMoneyExecutionDisabled(
                "Phase 14 has no real-money execution path. No broker "
                "adapter capable of placing a live order exists in this "
                "repository, and none can be enabled by configuration.")

    # ---------------- the kill switch ----------------

    @property
    def kill_switch_active(self) -> bool:
        return self.switches.emergency_stop

    def activate_kill_switch(self, reason: str, at: Optional[datetime] = None,
                             actor: Optional[str] = None) -> AuditEvent:
        """
        Stop all new orders (spec §29).

        Working orders are NOT cancelled and nothing is deleted. The
        switch stops the system from adding exposure; deciding what to
        do about exposure already at a venue is a human decision that
        needs the history this preserves.
        """
        if not reason:
            raise ValueError("activating the kill switch requires a reason")
        self.switches.emergency_stop = True
        self._kill_switch_reason = reason
        self._kill_switch_at = at
        return self._record("kill_switch_activated", at, actor, detail=reason)

    def release_kill_switch(self, reason: str, at: Optional[datetime] = None,
                            actor: Optional[str] = None) -> AuditEvent:
        if not reason:
            raise ValueError("releasing the kill switch requires a reason")
        self.switches.emergency_stop = False
        self._kill_switch_reason = ""
        self._kill_switch_at = None
        return self._record("kill_switch_released", at, actor, detail=reason)

    @property
    def kill_switch_reason(self) -> str:
        return self._kill_switch_reason

    # ---------------- the gate ----------------

    def check(self, environment: ExecutionEnvironment,
              broker_id: Optional[str] = None,
              account_id: Optional[str] = None,
              strategy_id: Optional[str] = None,
              portfolio_id: Optional[str] = None) -> SafetyVerdict:
        """
        Outermost switch first, so the reported reason is the broadest
        one that applies.
        """
        if environment.is_real_money:
            return SafetyVerdict(
                False, ExecutionRejectCode.REAL_MONEY_BLOCKED,
                "No live adapter exists and none can be configured.")

        if self.switches.emergency_stop:
            return SafetyVerdict(
                False, ExecutionRejectCode.EMERGENCY_STOP,
                self._kill_switch_reason)

        if not self.switches.execution_enabled:
            return SafetyVerdict(False, ExecutionRejectCode.EXECUTION_DISABLED)

        if environment is ExecutionEnvironment.PAPER and not self.switches.paper_execution_enabled:
            return SafetyVerdict(
                False, ExecutionRejectCode.ENVIRONMENT_DISABLED,
                "Paper execution is switched off.")

        if environment is ExecutionEnvironment.DEMO and not self.switches.demo_execution_enabled:
            return SafetyVerdict(
                False, ExecutionRejectCode.ENVIRONMENT_DISABLED,
                "Demo execution is switched off, and no demo adapter is "
                "implemented in this phase.")

        if not self.switches.enabled_for(self.switches.brokers, broker_id):
            return SafetyVerdict(False, ExecutionRejectCode.BROKER_DISABLED,
                                 f"Broker {broker_id}.")
        if not self.switches.enabled_for(self.switches.accounts, account_id):
            return SafetyVerdict(False, ExecutionRejectCode.ACCOUNT_DISABLED,
                                 f"Account {account_id}.")
        if not self.switches.enabled_for(self.switches.strategies, strategy_id):
            return SafetyVerdict(False, ExecutionRejectCode.STRATEGY_DISABLED,
                                 f"Strategy {strategy_id}.")
        if not self.switches.enabled_for(self.switches.portfolios, portfolio_id):
            return SafetyVerdict(False, ExecutionRejectCode.PORTFOLIO_DISABLED,
                                 f"Portfolio {portfolio_id}.")

        return SafetyVerdict(True)

    def apply_to(self, result: ValidationResult,
                 environment: ExecutionEnvironment,
                 at: Optional[datetime] = None,
                 correlation_id: str = "",
                 **routing) -> ValidationResult:
        """Fold a safety verdict into a validation result."""
        result.checks_performed += 1
        verdict = self.check(environment, **routing)
        if not verdict.permitted:
            result.fail(verdict.code, verdict.detail, at=at,
                        correlation_id=correlation_id,
                        environment=environment.value, **routing)
        return result

    # ---------------- permissions ----------------

    @staticmethod
    def permission_for(environment: ExecutionEnvironment) -> ExecutionPermission:
        return {
            ExecutionEnvironment.SIMULATION: ExecutionPermission.DRY_RUN_EXECUTION,
            ExecutionEnvironment.PAPER: ExecutionPermission.PAPER_EXECUTION,
            ExecutionEnvironment.DEMO: ExecutionPermission.DEMO_EXECUTION,
            ExecutionEnvironment.LIVE: ExecutionPermission.LIVE_EXECUTION_ADMIN,
        }[environment]

    def check_permission(self, held: Tuple[ExecutionPermission, ...],
                         required: ExecutionPermission) -> SafetyVerdict:
        """
        Whether a caller may do this (spec §47).

        `LIVE_EXECUTION_ADMIN` is refused even when it is held, because
        holding a permission for a capability that does not exist must
        not be mistaken for the capability existing.
        """
        if required is ExecutionPermission.LIVE_EXECUTION_ADMIN:
            return SafetyVerdict(
                False, ExecutionRejectCode.REAL_MONEY_BLOCKED,
                "Live execution is not implemented, so no permission grants it.")
        if required not in held:
            return SafetyVerdict(
                False, ExecutionRejectCode.NOT_PERMITTED,
                f"Requires {required.value}.")
        return SafetyVerdict(True)

    # ---------------- audit ----------------

    def _record(self, action: str, at: Optional[datetime],
                actor: Optional[str], detail: str = "") -> AuditEvent:
        event = AuditEvent(
            audit_id=f"aud-{action}-{int(at.timestamp()) if at else len(self.audit)}",
            at=at, action=action, actor=actor or self.actor,
            subject_type="execution_safety", subject_id="global",
            detail=detail)
        self.audit.append(event)
        return event

    def state(self) -> Dict[str, Any]:
        """A reportable summary, for the CLI and the dashboard."""
        return {
            "allow_real_orders": self.allow_real_orders,
            "real_orders_env_set": self.real_orders_requested_by_environment(),
            "execution_enabled": self.switches.execution_enabled,
            "paper_execution_enabled": self.switches.paper_execution_enabled,
            "demo_execution_enabled": self.switches.demo_execution_enabled,
            "emergency_stop": self.switches.emergency_stop,
            "emergency_stop_reason": self._kill_switch_reason,
            "disabled_brokers": [k for k, v in self.switches.brokers.items() if not v],
            "disabled_accounts": [k for k, v in self.switches.accounts.items() if not v],
            "disabled_strategies": [k for k, v in self.switches.strategies.items() if not v],
            "disabled_portfolios": [k for k, v in self.switches.portfolios.items() if not v],
        }
