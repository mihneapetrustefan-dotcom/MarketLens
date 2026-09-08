"""
src/trading/mode.py
-------------------------
The durable trading mode and the durable kill switch (§9, §26).

WHY THIS EXISTS AT ALL
--------------------------
Phase 14 built `ExecutionSafety`, which enforces the kill switch
correctly and refuses real money unconditionally. But its switch lives
in `SafetySwitches.emergency_stop`, which is an in-memory field on an
object constructed fresh by every script. `execution_controls` was
created to hold it and **nothing ever wrote to it** — a grep for
`save_control` finds the definition and no caller.

So an operator who stopped trading stopped it until the next process
started. Spec §26: *the state must be durable and auditable*. This
module is that state.

ONE STORE, NOT TWO
----------------------
Phase 14 still ENFORCES. This module only PERSISTS, and
`apply_to_safety()` loads the stored value into the Phase 14 object at
startup. There is deliberately no second enforcement path: a caller
that skipped `apply_to_safety` would find `ExecutionSafety` permitting
orders, which is the failure mode of two sources of truth, so
`TradingModeStore.resolve()` returns a resolution that already carries
the kill-switch state and the loop blocks on it before any order is
built.

FAIL CLOSED, AND SPECIFICALLY (§25)
---------------------------------------
Every path that cannot establish PAPER returns OFF with a reason
naming which check failed. A missing table, an empty table, an
unreadable row, a value of "live", a misspelling, a NULL: six
different reasons, one answer.

`live` IS REFUSED BY NAME
-----------------------------
A stored value of "live" does not fall through the generic unknown
branch. It resolves to OFF with the reason "live trading is blocked in
this phase", because an operator who set it needs to be told that the
boundary is structural rather than that they made a typo.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from src.data_access.trading_loop_schema import initialize_trading_loop_schema
from src.domain.trading_loop_models import (
    LOOP_METHOD_VERSION, ModeResolution, ModeSource, TradingMode, require_utc,
)

#: Read only as a LAST resort, after the stored value. An environment
#: variable cannot enable trading that the database has switched off —
#: `resolve()` consults the store first and returns on any stored row.
ENV_TRADING_MODE = "MARKETLENS_TRADING_MODE"


def _iso(moment: Optional[datetime]) -> Optional[str]:
    return moment.isoformat() if moment else None


def _absent_table(error: sqlite3.OperationalError) -> bool:
    """
    Narrow, on purpose.

    A blanket `except OperationalError` makes a missing COLUMN look
    identical to a missing table, and Phase 22 shipped that bug: a
    schema drift reported as "feature not installed" for a week.
    """
    return "no such table" in str(error).lower()


class TradingModeStore:
    """
    Reads and writes the single row of `trading_mode`.

    Every write also appends to `trading_mode_history`, in the same
    transaction. A mode change with no history entry is not possible,
    which is what makes §32's audit requirement structural.
    """

    def __init__(self, conn: sqlite3.Connection,
                 method_version: str = LOOP_METHOD_VERSION):
        self.conn = conn
        self.method_version = method_version

    # ---------------- reading ----------------

    def _row(self) -> Optional[sqlite3.Row]:
        try:
            cursor = self.conn.execute(
                "SELECT mode, reason, actor, kill_switch, kill_reason, "
                "updated_at FROM trading_mode WHERE singleton = 1")
        except sqlite3.OperationalError as error:
            if _absent_table(error):
                return None
            raise
        return cursor.fetchone()

    def resolve(self, now: Optional[datetime] = None) -> ModeResolution:
        """
        What the system is permitted to do, right now.

        Order: stored row, then environment, then the default of OFF.
        The stored row wins because it is the one an operator changed
        deliberately and durably; an environment variable that could
        override it would make the durable state advisory.
        """
        require_utc(now, "now")
        row = self._row()

        if row is None:
            raw_env = (os.environ.get(ENV_TRADING_MODE) or "").strip()
            if raw_env:
                mode, reason = TradingMode.resolve(raw_env)
                return ModeResolution(
                    mode=mode, source=ModeSource.ENVIRONMENT,
                    reason=reason or "", resolved_at=now, stored_raw=raw_env)
            return ModeResolution(
                mode=TradingMode.OFF, source=ModeSource.DEFAULT,
                reason="no trading mode has been recorded; the default is OFF",
                resolved_at=now)

        stored_raw = row[0]
        mode, reason = TradingMode.resolve(stored_raw)

        # The kill switch overrides a permitted mode. It does not
        # change the mode itself -- an operator who releases the switch
        # should get back the mode they configured, not have to set it
        # again.
        if mode.is_permitted and bool(row[3]):
            return ModeResolution(
                mode=TradingMode.OFF, source=ModeSource.STORED,
                reason=("the kill switch is active"
                        + (f": {row[4]}" if row[4] else "")),
                resolved_at=now, stored_raw=str(stored_raw))

        return ModeResolution(mode=mode, source=ModeSource.STORED,
                              reason=reason, resolved_at=now,
                              stored_raw=str(stored_raw))

    def kill_switch(self) -> Tuple[bool, str]:
        """
        `(active, reason)`. Absent state is reported as INACTIVE.

        That is the one place in this module where absence does not
        mean "refuse", and it is deliberate: a missing row means the
        switch was never pulled, and reporting it as active would make
        a fresh database look like an emergency. The FAIL-CLOSED
        guarantee is carried by the MODE, which defaults to OFF -- so a
        fresh database still cannot trade.
        """
        row = self._row()
        if row is None:
            return False, ""
        return bool(row[3]), str(row[4] or "")

    def state(self) -> Dict[str, Any]:
        row = self._row()
        if row is None:
            return {"configured": False, "mode": TradingMode.OFF.value,
                    "kill_switch": False, "kill_reason": "",
                    "reason": "no trading mode has been recorded",
                    "actor": "", "updated_at": None}
        return {"configured": True, "mode": str(row[0]), "reason": str(row[1]),
                "actor": str(row[2]), "kill_switch": bool(row[3]),
                "kill_reason": str(row[4]), "updated_at": row[5]}

    # ---------------- writing ----------------

    def _ensure(self) -> None:
        initialize_trading_loop_schema(self.conn)

    def _history(self, mode: str, previous: str, kill: bool, actor: str,
                 reason: str, at: datetime) -> None:
        self.conn.execute("""
            INSERT INTO trading_mode_history
            (entry_id, mode, previous_mode, kill_switch, actor, reason,
             method_version, occurred_at)
            VALUES (?,?,?,?,?,?,?,?)
        """, ("tmh-" + uuid.uuid4().hex[:16], mode, previous, int(kill),
              actor, reason, self.method_version, at.isoformat()))

    def set_mode(self, mode: TradingMode, *, actor: str, reason: str,
                 at: datetime) -> ModeResolution:
        """
        Record a mode. LIVE is refused here as well as at resolution.

        Refusing at the WRITE is what stops a live value from ever
        existing in the database. Refusing at the READ is what stops
        one that arrived another way -- a restored backup, a manual
        UPDATE -- from being acted on. Both, because either alone
        leaves a gap.
        """
        require_utc(at, "at")
        if not actor:
            raise ValueError("changing the trading mode requires an actor")
        if not reason:
            raise ValueError("changing the trading mode requires a reason")
        if mode is TradingMode.LIVE:
            from src.domain.trading_loop_models import TradingModeRefused
            raise TradingModeRefused(
                "LIVE cannot be recorded. Phase 25 has no live execution "
                "path, and storing the value would only create a row that "
                "every reader has to refuse.")

        self._ensure()
        current = self.state()
        kill, kill_reason = self.kill_switch()
        self.conn.execute("""
            INSERT INTO trading_mode
            (singleton, mode, reason, actor, kill_switch, kill_reason,
             method_version, updated_at)
            VALUES (1,?,?,?,?,?,?,?)
            ON CONFLICT(singleton) DO UPDATE SET
                mode = excluded.mode, reason = excluded.reason,
                actor = excluded.actor, method_version = excluded.method_version,
                updated_at = excluded.updated_at
        """, (mode.value, reason, actor, int(kill), kill_reason,
              self.method_version, at.isoformat()))
        self._history(mode.value, str(current.get("mode") or ""), kill,
                      actor, reason, at)
        self.conn.commit()
        return self.resolve(at)

    def activate_kill_switch(self, *, actor: str, reason: str,
                             at: datetime) -> ModeResolution:
        """
        Stop new trading, durably.

        Working orders are not cancelled and positions are not closed —
        the same rule Phase 14 states. The switch stops the system from
        ADDING exposure; what to do about exposure already at a venue
        is a decision that needs a person and the history this keeps.
        """
        return self._set_kill(True, actor=actor, reason=reason, at=at)

    def release_kill_switch(self, *, actor: str, reason: str,
                            at: datetime) -> ModeResolution:
        return self._set_kill(False, actor=actor, reason=reason, at=at)

    def _set_kill(self, active: bool, *, actor: str, reason: str,
                  at: datetime) -> ModeResolution:
        require_utc(at, "at")
        if not actor:
            raise ValueError("operating the kill switch requires an actor")
        if not reason:
            raise ValueError("operating the kill switch requires a reason")
        self._ensure()
        current = self.state()
        mode = str(current.get("mode") or TradingMode.OFF.value)
        self.conn.execute("""
            INSERT INTO trading_mode
            (singleton, mode, reason, actor, kill_switch, kill_reason,
             method_version, updated_at)
            VALUES (1,?,?,?,?,?,?,?)
            ON CONFLICT(singleton) DO UPDATE SET
                kill_switch = excluded.kill_switch,
                kill_reason = excluded.kill_reason,
                actor = excluded.actor,
                method_version = excluded.method_version,
                updated_at = excluded.updated_at
        """, (mode, str(current.get("reason") or ""), actor, int(active),
              reason if active else "", self.method_version, at.isoformat()))
        self._history(mode, mode, active, actor,
                      ("kill switch activated: " if active
                       else "kill switch released: ") + reason, at)
        self.conn.commit()
        return self.resolve(at)

    # ---------------- the wire into Phase 14 ----------------

    def apply_to_safety(self, safety: Any, at: datetime) -> bool:
        """
        Load the durable kill switch into the Phase 14 enforcer.

        Returns whether the switch is active. Called once at the start
        of every cycle, so a switch pulled between two runs takes
        effect on the next one without anybody restarting anything.

        Uses the public `activate_kill_switch`/`release_kill_switch`
        rather than assigning `switches.emergency_stop`, so the Phase
        14 audit trail records the load too.
        """
        active, reason = self.kill_switch()
        if active and not safety.kill_switch_active:
            safety.activate_kill_switch(
                reason=reason or "durable kill switch is set", at=at,
                actor="trading-loop")
        elif not active and safety.kill_switch_active:
            safety.release_kill_switch(
                reason="durable kill switch is clear", at=at,
                actor="trading-loop")
        return active
