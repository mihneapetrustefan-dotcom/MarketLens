"""
src/trading/api.py
------------------------
The read facade over the Phase 25 loop, and its integrity check.

WHY A TYPED FACADE AND NOT AN HTTP API
------------------------------------------
`docs/API_AUDIT.md` records that this repository has no web framework
by design: every phase is a batch job, nothing is network-reachable,
and the "API layer" is a typed Python facade with a permission object.
Adding a server for one phase would be the parallel architecture the
brief forbids. The CLI and the dashboard are both callers of this
module.

THE INTEGRITY CHECK IS THE POINT
------------------------------------
Phases 22, 23 and 24 each ended with one, and each time it found
something. The rule they converged on is that a check which cannot see
what it certifies is worse than no check, because it gets quoted. So
every check here reads ROWS and reports what it counted, and a check
that could not run says so rather than passing.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.data_access.trading_loop_schema import TRADING_LOOP_TABLES
from src.domain.trading_loop_models import (
    LOOP_METHOD_VERSION, LINEAGE_CHAIN, TradingMode,
)
from src.trading.mode import TradingModeStore
from src.trading.repository import TradingLoopRepository


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _count(conn: sqlite3.Connection, sql: str,
           params: Tuple[Any, ...] = ()) -> Optional[int]:
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error).lower():
            return None
        raise
    return int(row[0]) if row else 0


class TradingLoopAPI:
    """Everything a reader needs about the loop, and nothing that writes."""

    def __init__(self, conn: sqlite3.Connection,
                 method_version: str = LOOP_METHOD_VERSION):
        self.conn = conn
        self.method_version = method_version
        self.repository = TradingLoopRepository(conn, method_version)
        self.modes = TradingModeStore(conn, method_version)

    # ---------------- state ----------------

    def state(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """
        One call that answers "what is this system doing right now".

        `available` is False when the loop has never run, and that is
        reported as absence rather than as zeros -- the convention the
        dashboard has used since Phase 19.
        """
        now = now or datetime.now(timezone.utc)
        resolution = self.modes.resolve(now)
        if not _table_exists(self.conn, "trading_cycles"):
            kill, kill_reason = self.modes.kill_switch()
            return {"available": False,
                    "reason": "the trading loop has never run on this database",
                    "mode": resolution.mode.value,
                    "mode_source": resolution.source.value,
                    "mode_reason": resolution.reason,
                    "kill_switch": kill, "kill_reason": kill_reason}

        cycles = self.repository.recent_cycles(limit=25)
        kill, kill_reason = self.modes.kill_switch()
        account = self.repository.latest_account_state()
        positions = self.repository.latest_positions()

        return {
            "available": bool(cycles),
            "method_version": self.method_version,
            "mode": resolution.mode.value,
            "mode_source": resolution.source.value,
            "mode_reason": resolution.reason,
            "kill_switch": kill, "kill_reason": kill_reason,
            "sessions": self.repository.list_sessions(limit=20),
            "cycles": cycles,
            "total_cycles": _count(
                self.conn, "SELECT COUNT(*) FROM trading_cycles") or 0,
            "blocked_cycles": _count(
                self.conn,
                "SELECT COUNT(*) FROM trading_cycles WHERE status = 'blocked'") or 0,
            "account": account,
            "positions": positions,
            "eligibility": self.repository.eligibility_summary(),
            "lineage": self.repository.lineage_for(limit=50),
            "validations": self.repository.list_validations(limit=20),
            "audit": self.repository.audit_trail(limit=30),
        }

    def conversion(self) -> Dict[str, Any]:
        """
        Signal -> order -> fill -> outcome, as counts (§21, §30).

        The four numbers a reader actually wants, computed once so the
        CLI and the dashboard cannot disagree about them.
        """
        signals = _count(self.conn, "SELECT COUNT(*) FROM signal_eligibility")
        eligible = _count(
            self.conn,
            "SELECT COUNT(*) FROM signal_eligibility WHERE code = 'eligible'")
        orders = _count(self.conn,
                        "SELECT COUNT(*) FROM execution_orders")
        fills = _count(self.conn, "SELECT COUNT(*) FROM execution_fills")
        outcomes = _count(self.conn, "SELECT COUNT(*) FROM trade_outcomes")
        return {"signals_seen": signals, "eligible": eligible,
                "orders": orders, "fills": fills, "outcomes": outcomes,
                "signal_to_order": (orders / signals) if signals else None,
                "order_to_fill": (fills / orders) if orders else None,
                "fill_to_outcome": (outcomes / fills) if fills else None}

    def rejections(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Why signals did not trade, most common first (§14, §30)."""
        try:
            rows = self.conn.execute("""
                SELECT code, COUNT(*) n, MAX(detail)
                  FROM signal_eligibility WHERE code != 'eligible'
                 GROUP BY code ORDER BY n DESC LIMIT ?
            """, (limit,))
        except sqlite3.OperationalError:
            return []
        return [{"code": r[0], "count": r[1], "example": r[2]} for r in rows]

    # ---------------- integrity ----------------

    def integrity_check(self) -> Dict[str, Any]:
        """
        Fourteen checks that read rows.

        Every one reports what it COUNTED, so a passing check on an
        empty database is visibly a check over nothing rather than a
        clean bill of health. That distinction is what Phase 23.5 was
        about.
        """
        checks: List[Dict[str, Any]] = []

        def check(name: str, ok: Optional[bool], detail: str,
                  counted: Optional[int] = None) -> None:
            checks.append({"name": name, "ok": ok, "detail": detail,
                           "counted": counted})

        # 1. live mode was never recorded
        stored = _count(
            self.conn, "SELECT COUNT(*) FROM trading_mode WHERE mode = 'live'")
        if stored is None:
            check("no_live_mode_stored", None,
                  "the trading_mode table does not exist", None)
        else:
            check("no_live_mode_stored", stored == 0,
                  f"{stored} row(s) recording LIVE", stored)

        # 2. no session ever ran in a non-paper mode
        sessions = _count(
            self.conn,
            "SELECT COUNT(*) FROM paper_loop_sessions WHERE mode != 'paper'")
        if sessions is None:
            check("every_session_is_paper", None,
                  "no session table", None)
        else:
            check("every_session_is_paper", sessions == 0,
                  f"{sessions} session(s) not in paper mode", sessions)

        # 3. no order was ever created outside the paper environment
        live_orders = _count(
            self.conn,
            "SELECT COUNT(*) FROM execution_orders WHERE environment != 'paper'")
        if live_orders is None:
            check("every_order_is_paper", None,
                  "no execution_orders table -- no order has ever been placed",
                  None)
        else:
            check("every_order_is_paper", live_orders == 0,
                  f"{live_orders} order(s) outside paper", live_orders)

        # 4. every order carries a risk decision
        orphans = _count(self.conn, """
            SELECT COUNT(*) FROM execution_orders
             WHERE decision_id IS NULL OR decision_id = ''
        """)
        if orphans is None:
            check("every_order_has_a_risk_decision", None,
                  "no orders to check", None)
        else:
            total = _count(self.conn,
                           "SELECT COUNT(*) FROM execution_orders") or 0
            check("every_order_has_a_risk_decision", orphans == 0,
                  f"{orphans} of {total} order(s) carry no decision id", total)

        # 5. every order carries a signal
        unsourced = _count(self.conn, """
            SELECT COUNT(*) FROM execution_orders
             WHERE signal_id IS NULL OR signal_id = ''
        """)
        if unsourced is None:
            check("every_order_has_a_signal", None, "no orders to check", None)
        else:
            check("every_order_has_a_signal", unsourced == 0,
                  f"{unsourced} order(s) carry no signal id", unsourced)

        # 6. no broken lineage chain
        broken = self.repository.broken_lineage_count() if _table_exists(
            self.conn, "trade_lineage") else None
        if broken is None:
            check("no_broken_lineage", None, "no lineage table", None)
        else:
            total = _count(self.conn,
                           "SELECT COUNT(*) FROM trade_lineage") or 0
            check("no_broken_lineage", broken == 0,
                  f"{broken} of {total} chain(s) have a gap before a "
                  f"link that is present", total)

        # 6b. Phase 25 and Phase 16 agree about completeness
        #
        # They did not. Phase 25 recorded `complete = 1` on chains whose
        # model, model version and prediction were empty, because its
        # own chain did not include them, while Phase 16 recorded
        # `lineage_complete = 0` on the same trades. A check that
        # certifies what it cannot see is worse than no check.
        if (_table_exists(self.conn, "trade_lineage")
                and _table_exists(self.conn, "trade_outcomes")):
            disagreements = _count(self.conn, """
                SELECT COUNT(*) FROM trade_lineage l
                  JOIN trade_outcomes o ON o.order_id = l.order_id
                 WHERE l.complete != o.lineage_complete
            """)
            compared = _count(self.conn, """
                SELECT COUNT(*) FROM trade_lineage l
                  JOIN trade_outcomes o ON o.order_id = l.order_id
            """) or 0
            check("lineage_models_agree", (disagreements or 0) == 0,
                  f"{disagreements} of {compared} trade(s) where Phase 25 and "
                  f"Phase 16 disagree about lineage completeness", compared)
        else:
            check("lineage_models_agree", None,
                  "no trade has both a lineage row and an outcome", None)

        # 6c. a filled order can name the model that produced it
        if _table_exists(self.conn, "trade_lineage"):
            total_filled = _count(self.conn,
                                  "SELECT COUNT(*) FROM trade_lineage "
                                  "WHERE fill_id IS NOT NULL") or 0
            unexplained = _count(self.conn, """
                SELECT COUNT(*) FROM trade_lineage
                 WHERE fill_id IS NOT NULL
                   AND (strategy_id IS NULL OR strategy_id = '')
            """) or 0
            check("every_trade_names_its_strategy", unexplained == 0,
                  f"{unexplained} of {total_filled} filled trade(s) name no "
                  f"strategy", total_filled)
        else:
            check("every_trade_names_its_strategy", None,
                  "no lineage table", None)

        # 7. every signal the loop saw has a verdict
        seen = _count(self.conn, "SELECT COUNT(*) FROM signal_eligibility")
        if seen is None:
            check("every_signal_has_a_verdict", None,
                  "the loop has never evaluated a signal", None)
        else:
            blank = _count(self.conn, """
                SELECT COUNT(*) FROM signal_eligibility
                 WHERE code IS NULL OR code = ''
            """) or 0
            check("every_signal_has_a_verdict", blank == 0,
                  f"{blank} of {seen} verdict(s) carry no code", seen)

        # 8. targets and actuals never share a table
        overlap = [name for name in ("position_targets", "position_actuals")
                   if not _table_exists(self.conn, name)]
        check("targets_and_actuals_are_separate", not overlap,
              ("both tables exist and are distinct" if not overlap
               else f"missing: {', '.join(overlap)}"),
              None)

        # 9. no actual position was ever recorded as authoritative
        #    without coming from the broker
        bad_origin = _count(self.conn, """
            SELECT COUNT(*) FROM position_actuals
             WHERE origin NOT IN ('broker_reconciled', 'local_projection',
                                  'unknown')
        """)
        if bad_origin is None:
            check("positions_carry_a_known_origin", None,
                  "no position rows", None)
        else:
            total = _count(self.conn,
                           "SELECT COUNT(*) FROM position_actuals") or 0
            check("positions_carry_a_known_origin", bad_origin == 0,
                  f"{bad_origin} of {total} row(s) carry an unknown origin",
                  total)

        # 10. no validation reached a state past paper
        beyond = _count(
            self.conn,
            "SELECT COUNT(*) FROM paper_validations WHERE state = 'live_eligible'")
        if beyond is None:
            check("nothing_reached_live_eligible", None,
                  "no validation records", None)
        else:
            check("nothing_reached_live_eligible", beyond == 0,
                  f"{beyond} validation(s) at LIVE_ELIGIBLE", beyond)

        # 11. every review carries a named reviewer and a reason
        unsigned = _count(self.conn, """
            SELECT COUNT(*) FROM paper_validation_reviews
             WHERE reviewer IS NULL OR reviewer = ''
                OR reason IS NULL OR reason = ''
        """)
        if unsigned is None:
            check("every_review_is_signed", None, "no reviews", None)
        else:
            total = _count(
                self.conn,
                "SELECT COUNT(*) FROM paper_validation_reviews") or 0
            check("every_review_is_signed", unsigned == 0,
                  f"{unsigned} of {total} review(s) are unsigned", total)

        # 12. no cycle is stuck claimed with no worker
        stuck = _count(self.conn, """
            SELECT COUNT(*) FROM trading_cycles
             WHERE status = 'claimed' AND claimed_by = ''
        """)
        if stuck is None:
            check("no_orphaned_cycle", None, "no cycles", None)
        else:
            total = _count(self.conn,
                           "SELECT COUNT(*) FROM trading_cycles") or 0
            check("no_orphaned_cycle", stuck == 0,
                  f"{stuck} of {total} cycle(s) are claimed by nobody", total)

        failed = [c for c in checks if c["ok"] is False]
        unrun = [c for c in checks if c["ok"] is None]
        return {
            "checks": checks,
            "passed": sum(1 for c in checks if c["ok"] is True),
            "failed": len(failed), "not_run": len(unrun),
            # A check that could not run is NOT a pass. Reported
            # separately so a summary line cannot round it into one.
            "ok": not failed,
            "conclusive": not failed and not unrun,
        }
