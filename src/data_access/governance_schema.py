"""
src/data_access/governance_schema.py
-----------------------------------------
Phase 16 persistence (spec §37, §50, §52, §53, §54, §67, §73).

ADDITIVE, LIKE EVERY PHASE BEFORE IT
----------------------------------------
`CREATE TABLE IF NOT EXISTS`, safe against a populated database,
nothing dropped or renamed. That is this project's migration
mechanism and Phase 16 does not invent a second one.

WHAT IS PERSISTED AND WHY EACH IS SEPARATE
----------------------------------------------
Promotion approvals, sessions, session events, day state, trade
outcomes, missed trades, journal entries, alerts and health readings.

They are separate tables rather than one event log because they are
QUERIED differently: an operator asks "what happened in this session",
a future learning system asks "show me every trade by this model in
this regime", and an auditor asks "who approved live trading and
when". One table shaped for any of those is wrong for the other two.

THE OUTCOME TABLE IS THE ONE THAT MATTERS LONGEST
-----------------------------------------------------
Everything else is operational and ages out. `trade_outcomes` carries
the full lineage — model, prediction, signal, decision, intent, order,
IBKR order, fills, P&L, execution quality — and is what a future
learning phase will read. Its columns are wide on purpose: a lineage
reconstructed by joining six tables is a lineage that breaks when one
of them is pruned.

APPEND-ONLY WHERE IT MATTERS
--------------------------------
Session events, journal entries, approvals and alerts are inserted and
never updated. There is deliberately no method that rewrites one.
"""

from __future__ import annotations

import sqlite3


def initialize_governance_schema(conn: sqlite3.Connection) -> None:
    """Create the Phase 16 tables. Safe to call repeatedly."""

    # ---------------- promotion and approval (spec §41, §83) ----------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS promotion_requests (
            request_id      TEXT PRIMARY KEY,
            level           INTEGER NOT NULL,
            level_label     TEXT NOT NULL DEFAULT '',
            state           TEXT NOT NULL DEFAULT 'requested',
            requested_by    TEXT NOT NULL,
            requested_at    TEXT,
            reason          TEXT NOT NULL DEFAULT '',
            -- The approver is stored separately from the requester and
            -- the two are compared: nobody approves their own request.
            approved_by     TEXT,
            approved_at     TEXT,
            expires_at      TEXT,
            decision_note   TEXT NOT NULL DEFAULT '',
            -- What the decision was actually based on, frozen at the
            -- moment it was taken.
            gate_snapshot_json      TEXT NOT NULL DEFAULT '{}',
            readiness_snapshot_json TEXT NOT NULL DEFAULT '{}'
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS readiness_assessments (
            assessment_id TEXT PRIMARY KEY,
            at            TEXT NOT NULL,
            is_ready      INTEGER NOT NULL DEFAULT 0,
            verdicts_json TEXT NOT NULL DEFAULT '{}',
            notes_json    TEXT NOT NULL DEFAULT '{}',
            actor         TEXT NOT NULL DEFAULT 'system'
        )
    """)

    # ---------------- sessions (spec §42, §43, §86) -------------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS trading_sessions (
            session_id          TEXT PRIMARY KEY,
            state               TEXT NOT NULL DEFAULT 'created',
            operator            TEXT NOT NULL,
            broker_id           TEXT NOT NULL DEFAULT 'ibkr',
            account_id          TEXT NOT NULL DEFAULT '',
            environment         TEXT NOT NULL DEFAULT 'paper',
            level               INTEGER NOT NULL DEFAULT 2,
            approval_id         TEXT,

            -- Frozen at start. A trade must be traceable to the exact
            -- settings that produced it (spec §87).
            config_json         TEXT NOT NULL DEFAULT '{}',
            config_fingerprint  TEXT NOT NULL DEFAULT '',
            model_version       TEXT,
            strategy_version    TEXT,
            feature_version     TEXT,
            signal_version      TEXT,
            risk_config_version TEXT,
            execution_config_version TEXT,
            code_version        TEXT,

            capital_limit       REAL,
            daily_loss_limit    REAL,

            created_at          TEXT,
            started_at          TEXT,
            ended_at            TEXT,
            termination_reason  TEXT NOT NULL DEFAULT '',
            preflight_json      TEXT NOT NULL DEFAULT '[]',
            summary_json        TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sessions_state
        ON trading_sessions (state, started_at)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_events (
            session_id  TEXT NOT NULL,
            sequence    INTEGER NOT NULL,
            at          TEXT,
            action      TEXT NOT NULL,
            actor       TEXT NOT NULL,
            from_state  TEXT,
            to_state    TEXT,
            reason      TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (session_id, sequence)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_day_state (
            session_id       TEXT NOT NULL,
            day              TEXT NOT NULL,
            realized_pnl     REAL NOT NULL DEFAULT 0,
            unrealized_pnl   REAL,
            starting_equity  REAL,
            current_equity   REAL,
            peak_equity      REAL,
            orders_submitted INTEGER NOT NULL DEFAULT 0,
            orders_rejected  INTEGER NOT NULL DEFAULT 0,
            updated_at       TEXT,
            PRIMARY KEY (session_id, day)
        )
    """)

    # ---------------- trade outcomes (spec §20, §67) ------------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_outcomes (
            outcome_id      TEXT PRIMARY KEY,
            session_id      TEXT,
            instrument_id   TEXT NOT NULL,
            side            TEXT NOT NULL,
            quantity        REAL NOT NULL,
            environment     TEXT NOT NULL DEFAULT 'paper',
            is_open         INTEGER NOT NULL DEFAULT 0,

            entry_at        TEXT,
            exit_at         TEXT,
            entry_price     REAL,
            exit_price      REAL,
            gross_pnl       REAL,
            fees            REAL NOT NULL DEFAULT 0,
            net_pnl         REAL,
            return_pct      REAL,
            holding_days    REAL,
            exit_reason     TEXT NOT NULL DEFAULT 'unknown',
            market_regime   TEXT,
            event_context_json TEXT NOT NULL DEFAULT '[]',

            -- The full lineage, flat rather than joined. A chain that
            -- needs six tables is a chain that breaks when one is
            -- pruned (spec §66, §67).
            correlation_id  TEXT NOT NULL DEFAULT '',
            model_id        TEXT,
            model_version   TEXT,
            prediction_id   TEXT,
            feature_version TEXT,
            signal_id       TEXT,
            signal_version  TEXT,
            strategy_id     TEXT,
            strategy_version TEXT,
            portfolio_id    TEXT,
            decision_id     TEXT,
            risk_config_version TEXT,
            intent_id       TEXT,
            order_id        TEXT,
            client_order_id TEXT,
            broker_order_id TEXT,
            execution_ids_json TEXT NOT NULL DEFAULT '[]',
            fill_ids_json   TEXT NOT NULL DEFAULT '[]',
            execution_config_version TEXT,
            code_version    TEXT,
            lineage_complete INTEGER NOT NULL DEFAULT 0,

            -- Execution quality (spec §19, §66)
            decision_price  REAL,
            reference_price REAL,
            bid             REAL,
            ask             REAL,
            submitted_price REAL,
            fill_price      REAL,
            slippage        REAL,
            slippage_bps    REAL,
            commission      REAL NOT NULL DEFAULT 0,
            submit_latency_ms REAL,
            ack_latency_ms  REAL,
            fill_latency_ms REAL,

            -- Post-mortem slots. NULL means no judgement has been
            -- made, which must stay distinguishable from a judgement
            -- of False (spec §21, §68).
            post_mortem_json TEXT NOT NULL DEFAULT '{}',
            reviewed_by     TEXT,
            reviewed_at     TEXT
        )
    """)
    for column in ("strategy_id", "model_version", "signal_id",
                   "instrument_id", "market_regime", "session_id"):
        conn.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_outcomes_{column}
            ON trade_outcomes ({column})
        """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS missed_trades (
            missed_id       TEXT PRIMARY KEY,
            at              TEXT,
            session_id      TEXT,
            instrument_id   TEXT NOT NULL,
            reason          TEXT NOT NULL,
            detail          TEXT NOT NULL DEFAULT '',
            side            TEXT,
            intended_quantity REAL,
            reference_price REAL,
            reject_code     TEXT,
            -- Whether the SYSTEM stopped it, as opposed to the market.
            -- The distinction a later analysis most needs.
            prevented_by_system INTEGER NOT NULL DEFAULT 0,
            forward_return  REAL,
            correlation_id  TEXT NOT NULL DEFAULT '',
            signal_id       TEXT,
            strategy_id     TEXT,
            lineage_json    TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_missed_reason
        ON missed_trades (reason, strategy_id)
    """)

    # ---------------- journal and alerts (spec §46, §52) --------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS execution_journal (
            entry_id       TEXT PRIMARY KEY,
            at             TEXT,
            session_id     TEXT,
            kind           TEXT NOT NULL,
            summary        TEXT NOT NULL DEFAULT '',
            correlation_id TEXT NOT NULL DEFAULT '',
            detail_json    TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_journal_session
        ON execution_journal (session_id, kind)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS execution_alerts (
            alert_id        TEXT PRIMARY KEY,
            at              TEXT,
            session_id      TEXT,
            code            TEXT NOT NULL,
            severity        TEXT NOT NULL,
            message         TEXT NOT NULL DEFAULT '',
            detail          TEXT NOT NULL DEFAULT '',
            order_id        TEXT,
            acknowledged    INTEGER NOT NULL DEFAULT 0,
            acknowledged_by TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_alerts_open
        ON execution_alerts (acknowledged, severity)
    """)

    # ---------------- health and metrics (spec §18, §75) --------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS system_health_readings (
            session_id  TEXT NOT NULL,
            at          TEXT NOT NULL,
            capability  TEXT NOT NULL,
            state       TEXT NOT NULL,
            detail      TEXT NOT NULL DEFAULT '',
            latency_ms  REAL,
            age_seconds REAL,
            PRIMARY KEY (session_id, at, capability)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS execution_metrics (
            session_id   TEXT NOT NULL,
            at           TEXT NOT NULL,
            metrics_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (session_id, at)
        )
    """)

    # ---------------- limits (spec §12, §25) --------------------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS limit_breaches (
            breach_id    TEXT PRIMARY KEY,
            at           TEXT,
            session_id   TEXT,
            limit_name   TEXT NOT NULL,
            detail       TEXT NOT NULL DEFAULT '',
            order_id     TEXT,
            -- Latching breaches do not clear themselves; a human must
            -- reactivate, and that reactivation is recorded here.
            latched      INTEGER NOT NULL DEFAULT 0,
            cleared_at   TEXT,
            cleared_by   TEXT,
            clear_reason TEXT NOT NULL DEFAULT ''
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS environment_comparisons (
            comparison_id TEXT PRIMARY KEY,
            at            TEXT NOT NULL,
            live_trades   INTEGER NOT NULL DEFAULT 0,
            live_days     INTEGER NOT NULL DEFAULT 0,
            conclusive    INTEGER NOT NULL DEFAULT 0,
            rows_json     TEXT NOT NULL DEFAULT '[]',
            notes_json    TEXT NOT NULL DEFAULT '[]'
        )
    """)

    conn.commit()
