"""
src/data_access/trading_loop_schema.py
-------------------------------------------------
Phase 25 storage — the paper-trading operating loop.

WHAT IS DELIBERATELY NOT HERE (§2, §44)
-------------------------------------------
No order table — Phase 14 owns `execution_orders` and `execution_fills`.
No broker, account or reconciliation table — Phase 14/15 own those.
No risk decision or intent table — Phase 11 owns `risk_decisions` and
`order_intents`.
No outcome, attribution or memory table — Phases 19, 20 and 21 own them.
No challenger table — Phase 24 owns those.

This schema adds only what genuinely had no home: the durable trading
MODE, the CYCLE and its stages, the eligibility verdict for every
signal the loop saw, the target-versus-actual position pair, the
account state as the broker reported it, the lineage chain, and the
paper validation record.

WHY MODE IS TWO TABLES
--------------------------
`trading_mode` holds exactly one row — the current state — so a reader
cannot accidentally act on a superseded one. `trading_mode_history` is
append-only, so how the system got here survives. Spec §26 wants the
kill switch durable AND auditable, and one table cannot be both without
a reader having to know which row is live.

WHY TARGETS AND ACTUALS ARE SEPARATE TABLES
-----------------------------------------------
Spec §16. One table with a `kind` column would let a query forget the
filter and report intentions as holdings, and that specific mistake is
the one §16 exists to prevent. Two tables make the wrong query fail
rather than mislead.

WHY EVERY SIGNAL GETS A ROW
-------------------------------
Spec §14: *do not silently discard signals*. `signal_eligibility` gets
one row per signal per cycle, including the eligible ones — which is
what makes the signal-to-trade conversion in §21 computable at all.

SAFE TO RUN REPEATEDLY: CREATE TABLE / INDEX IF NOT EXISTS plus an
additive column migration.
"""

from __future__ import annotations

import sqlite3


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """
    Additive migration for tables an earlier build already created.

    Same reasoning as Phases 22-24: CREATE TABLE IF NOT EXISTS is a
    no-op on an existing table, so a new column has to be added
    explicitly or every INSERT naming it fails. Nullable with defaults,
    so old rows stay valid and simply carry nothing.
    """
    wanted = {
        "paper_validations": [
            ("challenger_version", "INTEGER"),
        ],
        "trading_cycles": [
            ("worker", "TEXT NOT NULL DEFAULT ''"),
        ],
    }
    for table, columns in wanted.items():
        have = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
        if not have:
            continue
        for name, declaration in columns:
            if name not in have:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s"
                             % (table, name, declaration))


def initialize_trading_loop_schema(conn: sqlite3.Connection) -> None:
    """Create the Phase 25 tables. Idempotent and additive."""

    # ---------------- mode and kill switch (§9, §26) ----------------

    # Exactly one row, enforced by the CHECK on a constant primary key.
    # A second row cannot be inserted, so "the current mode" is never a
    # question about ordering or timestamps.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trading_mode (
            singleton       INTEGER PRIMARY KEY CHECK (singleton = 1),
            mode            TEXT NOT NULL,
            reason          TEXT NOT NULL DEFAULT '',
            actor           TEXT NOT NULL DEFAULT '',
            kill_switch     INTEGER NOT NULL DEFAULT 0,
            kill_reason     TEXT NOT NULL DEFAULT '',
            method_version  TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS trading_mode_history (
            entry_id        TEXT PRIMARY KEY,
            mode            TEXT NOT NULL,
            previous_mode   TEXT NOT NULL DEFAULT '',
            kill_switch     INTEGER NOT NULL DEFAULT 0,
            actor           TEXT NOT NULL,
            reason          TEXT NOT NULL,
            method_version  TEXT NOT NULL,
            occurred_at     TEXT NOT NULL
        )
    """)

    # ---------------- sessions (§28, §29) ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_loop_sessions (
            session_id                  TEXT PRIMARY KEY,
            name                        TEXT NOT NULL,
            mode                        TEXT NOT NULL,
            method_version              TEXT NOT NULL,
            broker_id                   TEXT NOT NULL DEFAULT '',
            account_id                  TEXT NOT NULL DEFAULT '',
            strategy_id                 TEXT,
            strategy_version            TEXT,
            challenger_id               TEXT,
            trained_model_id            TEXT,
            model_status                TEXT,
            experimental                INTEGER NOT NULL DEFAULT 1,
            constraint_version          TEXT NOT NULL DEFAULT '',
            feature_set_version         TEXT NOT NULL DEFAULT '',
            dataset_version             TEXT NOT NULL DEFAULT '',
            configuration_fingerprint   TEXT NOT NULL DEFAULT '',
            configuration_json          TEXT NOT NULL DEFAULT '',
            cycle_seconds               INTEGER NOT NULL DEFAULT 900,
            status                      TEXT NOT NULL DEFAULT 'open',
            started_at                  TEXT NOT NULL,
            ended_at                    TEXT
        )
    """)

    # ---------------- cycles (§13) ----------------

    # `status` plus `claimed_by`/`claimed_at` implement the atomic
    # claim: UPDATE ... WHERE status='claimed' AND claimed_by='' is
    # either seen by one worker or by none. The Phase 23.5 pattern.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trading_cycles (
            cycle_id                TEXT PRIMARY KEY,
            session_id              TEXT NOT NULL,
            method_version          TEXT NOT NULL,
            anchor                  TEXT NOT NULL,
            status                  TEXT NOT NULL,
            mode                    TEXT NOT NULL DEFAULT 'off',
            health                  TEXT NOT NULL DEFAULT 'blocked',
            claimed_by              TEXT NOT NULL DEFAULT '',
            claimed_at              TEXT,
            finished_at             TEXT,
            signals_seen            INTEGER NOT NULL DEFAULT 0,
            signals_eligible        INTEGER NOT NULL DEFAULT 0,
            targets_set             INTEGER NOT NULL DEFAULT 0,
            intents_created         INTEGER NOT NULL DEFAULT 0,
            intents_rejected        INTEGER NOT NULL DEFAULT 0,
            orders_submitted        INTEGER NOT NULL DEFAULT 0,
            orders_rejected         INTEGER NOT NULL DEFAULT 0,
            fills_recorded          INTEGER NOT NULL DEFAULT 0,
            positions_reconciled    INTEGER NOT NULL DEFAULT 0,
            discrepancies           INTEGER NOT NULL DEFAULT 0,
            outcomes_recorded       INTEGER NOT NULL DEFAULT 0,
            blocks_json             TEXT NOT NULL DEFAULT '[]',
            timestamps_json         TEXT NOT NULL DEFAULT '{}',
            detail                  TEXT NOT NULL DEFAULT '',
            worker                  TEXT NOT NULL DEFAULT ''
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS trading_cycle_stages (
            cycle_id        TEXT NOT NULL,
            stage           TEXT NOT NULL,
            outcome         TEXT NOT NULL,
            detail          TEXT NOT NULL DEFAULT '',
            count           INTEGER NOT NULL DEFAULT 0,
            duration_ms     REAL,
            block_reason    TEXT NOT NULL DEFAULT '',
            started_at      TEXT,
            finished_at     TEXT,
            PRIMARY KEY (cycle_id, stage)
        )
    """)

    # ---------------- eligibility (§14) ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_eligibility (
            cycle_id            TEXT NOT NULL,
            signal_id           TEXT NOT NULL,
            method_version      TEXT NOT NULL,
            instrument_id       TEXT NOT NULL DEFAULT '',
            code                TEXT NOT NULL,
            detail              TEXT NOT NULL DEFAULT '',
            checks_performed    INTEGER NOT NULL DEFAULT 0,
            trained_model_id    TEXT,
            model_status        TEXT,
            strategy_id         TEXT,
            experimental        INTEGER NOT NULL DEFAULT 1,
            evaluated_at        TEXT NOT NULL,
            PRIMARY KEY (cycle_id, signal_id, method_version)
        )
    """)

    # ---------------- targets vs actuals (§5, §7, §16) ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS position_targets (
            cycle_id            TEXT NOT NULL,
            instrument_id       TEXT NOT NULL,
            method_version      TEXT NOT NULL,
            target_quantity     REAL,
            target_weight       REAL,
            reference_price     REAL,
            signal_id           TEXT,
            decision_id         TEXT,
            portfolio_id        TEXT,
            reason              TEXT NOT NULL DEFAULT '',
            decided_at          TEXT NOT NULL,
            PRIMARY KEY (cycle_id, instrument_id, method_version)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS position_actuals (
            cycle_id            TEXT NOT NULL,
            instrument_id       TEXT NOT NULL,
            method_version      TEXT NOT NULL,
            quantity            REAL NOT NULL DEFAULT 0,
            average_price       REAL,
            market_price        REAL,
            unrealized_pnl      REAL,
            realized_pnl        REAL,
            origin              TEXT NOT NULL,
            broker_id           TEXT NOT NULL DEFAULT '',
            account_id          TEXT NOT NULL DEFAULT '',
            observed_at         TEXT NOT NULL,
            PRIMARY KEY (cycle_id, instrument_id, method_version)
        )
    """)

    # The gap between the two, stored rather than recomputed by every
    # reader — so that "what is still outstanding" has one answer.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS position_deltas (
            cycle_id            TEXT NOT NULL,
            instrument_id       TEXT NOT NULL,
            method_version      TEXT NOT NULL,
            target_quantity     REAL,
            actual_quantity     REAL NOT NULL DEFAULT 0,
            pending_quantity    REAL NOT NULL DEFAULT 0,
            outstanding         REAL,
            action              TEXT NOT NULL DEFAULT '',
            side                TEXT,
            computed_at         TEXT NOT NULL,
            PRIMARY KEY (cycle_id, instrument_id, method_version)
        )
    """)

    # ---------------- account state (§4, §17) ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS loop_account_states (
            cycle_id            TEXT NOT NULL,
            broker_id           TEXT NOT NULL,
            account_id          TEXT NOT NULL,
            method_version      TEXT NOT NULL,
            source              TEXT NOT NULL,
            base_currency       TEXT NOT NULL DEFAULT 'USD',
            cash                REAL,
            equity              REAL,
            buying_power        REAL,
            available_funds     REAL,
            margin_used         REAL,
            margin_available    REAL,
            realized_pnl        REAL,
            unrealized_pnl      REAL,
            open_positions      INTEGER NOT NULL DEFAULT 0,
            open_orders         INTEGER NOT NULL DEFAULT 0,
            pending_orders      INTEGER NOT NULL DEFAULT 0,
            connection_state    TEXT NOT NULL DEFAULT 'unknown',
            detail              TEXT NOT NULL DEFAULT '',
            observed_at         TEXT NOT NULL,
            synchronized_at     TEXT,
            PRIMARY KEY (cycle_id, broker_id, account_id, method_version)
        )
    """)

    # ---------------- lineage (§10, §18) ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_lineage (
            lineage_id              TEXT PRIMARY KEY,
            cycle_id                TEXT NOT NULL,
            method_version          TEXT NOT NULL,
            instrument_id           TEXT NOT NULL DEFAULT '',
            signal_id               TEXT,
            decision_id             TEXT,
            intent_id               TEXT,
            order_id                TEXT,
            fill_id                 TEXT,
            position_instrument_id  TEXT,
            outcome_id              TEXT,
            trained_model_id        TEXT,
            model_version           TEXT,
            strategy_id             TEXT,
            strategy_version        TEXT,
            challenger_id           TEXT,
            complete                INTEGER NOT NULL DEFAULT 0,
            broken                  INTEGER NOT NULL DEFAULT 0,
            recorded_at             TEXT NOT NULL
        )
    """)

    # ---------------- paper validation (§21, §22, §38) ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_validations (
            validation_id               TEXT PRIMARY KEY,
            strategy_id                 TEXT NOT NULL,
            strategy_version            TEXT NOT NULL,
            session_id                  TEXT NOT NULL,
            method_version              TEXT NOT NULL,
            baseline_id                 TEXT,
            baseline_version            TEXT,
            challenger_id               TEXT,
            challenger_version          INTEGER,
            state                       TEXT NOT NULL,
            decisions                   INTEGER NOT NULL DEFAULT 0,
            orders                      INTEGER NOT NULL DEFAULT 0,
            fills                       INTEGER NOT NULL DEFAULT 0,
            rejected_orders             INTEGER NOT NULL DEFAULT 0,
            completed_trades            INTEGER NOT NULL DEFAULT 0,
            risk_violations             INTEGER NOT NULL DEFAULT 0,
            operational_failures        INTEGER NOT NULL DEFAULT 0,
            signals_seen                INTEGER NOT NULL DEFAULT 0,
            realized_pnl                REAL,
            unrealized_pnl              REAL,
            max_drawdown                REAL,
            turnover                    REAL,
            gross_exposure              REAL,
            dimensions_json             TEXT NOT NULL DEFAULT '[]',
            unmeasured_json             TEXT NOT NULL DEFAULT '[]',
            conclusive                  INTEGER NOT NULL DEFAULT 0,
            configuration_fingerprint   TEXT NOT NULL DEFAULT '',
            notes                       TEXT NOT NULL DEFAULT '',
            started_at                  TEXT,
            ended_at                    TEXT
        )
    """)

    # Append-only, exactly like `challenger_reviews`. A state change a
    # person made keeps its actor, its reason and its moment; changing
    # one's mind writes a second row.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_validation_reviews (
            review_id       TEXT PRIMARY KEY,
            validation_id   TEXT NOT NULL,
            from_state      TEXT NOT NULL,
            to_state        TEXT NOT NULL,
            reviewer        TEXT NOT NULL,
            reason          TEXT NOT NULL,
            method_version  TEXT NOT NULL,
            reviewed_at     TEXT NOT NULL
        )
    """)

    # ---------------- audit (§32) ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS trading_loop_audit (
            audit_id        TEXT PRIMARY KEY,
            method_version  TEXT NOT NULL,
            actor           TEXT NOT NULL,
            action          TEXT NOT NULL,
            session_id      TEXT NOT NULL DEFAULT '',
            cycle_id        TEXT NOT NULL DEFAULT '',
            subject_id      TEXT NOT NULL DEFAULT '',
            detail          TEXT NOT NULL DEFAULT '',
            occurred_at     TEXT NOT NULL
        )
    """)

    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_tl_mode_history "
        "ON trading_mode_history (occurred_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_tl_cycle_session "
        "ON trading_cycles (session_id, anchor DESC)",
        "CREATE INDEX IF NOT EXISTS idx_tl_cycle_status "
        "ON trading_cycles (status, anchor DESC)",
        "CREATE INDEX IF NOT EXISTS idx_tl_elig_code "
        "ON signal_eligibility (code, evaluated_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_tl_elig_signal "
        "ON signal_eligibility (signal_id)",
        "CREATE INDEX IF NOT EXISTS idx_tl_target_instr "
        "ON position_targets (instrument_id)",
        "CREATE INDEX IF NOT EXISTS idx_tl_actual_instr "
        "ON position_actuals (instrument_id)",
        "CREATE INDEX IF NOT EXISTS idx_tl_lineage_cycle "
        "ON trade_lineage (cycle_id)",
        "CREATE INDEX IF NOT EXISTS idx_tl_lineage_signal "
        "ON trade_lineage (signal_id)",
        "CREATE INDEX IF NOT EXISTS idx_tl_lineage_order "
        "ON trade_lineage (order_id)",
        "CREATE INDEX IF NOT EXISTS idx_tl_validation_strategy "
        "ON paper_validations (strategy_id, strategy_version)",
        "CREATE INDEX IF NOT EXISTS idx_tl_validation_challenger "
        "ON paper_validations (challenger_id)",
        "CREATE INDEX IF NOT EXISTS idx_tl_review_validation "
        "ON paper_validation_reviews (validation_id, reviewed_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_tl_audit_time "
        "ON trading_loop_audit (occurred_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_tl_account_cycle "
        "ON loop_account_states (cycle_id)",
    ):
        conn.execute(statement)

    _add_missing_columns(conn)
    conn.commit()


#: Every table this schema owns. Used by the boundary test, which
#: counts rows in every table before and after a cycle and asserts that
#: only these moved — the Phase 23.5 method, which is the only kind of
#: boundary claim that can actually fail.
TRADING_LOOP_TABLES = (
    "trading_mode",
    "trading_mode_history",
    "paper_loop_sessions",
    "trading_cycles",
    "trading_cycle_stages",
    "signal_eligibility",
    "position_targets",
    "position_actuals",
    "position_deltas",
    "loop_account_states",
    "trade_lineage",
    "paper_validations",
    "paper_validation_reviews",
    "trading_loop_audit",
)
