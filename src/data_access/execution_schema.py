"""
src/data_access/execution_schema.py
----------------------------------------
Phase 14 persistence (spec §53).

ADDITIVE, LIKE EVERY OTHER PHASE
------------------------------------
This project has no ORM and no migration framework. Schema changes are
`CREATE TABLE IF NOT EXISTS` functions called at startup, and every
earlier phase follows that pattern. Introducing Alembic for one phase
would be the parallel architecture §0 forbids — and would leave twelve
phases on one mechanism and one on another.

So this is the migration: additive, idempotent, and safe to run against
a populated database. Nothing here drops, renames or rewrites a column,
and no existing table is touched.

WHAT IS PERSISTED AND WHAT DELIBERATELY IS NOT
--------------------------------------------------
Persisted: brokers, accounts, instrument mappings, orders, the full
state history, fills, events, reconciliation records, errors, health
and the audit trail.

NOT persisted, anywhere: credentials, endpoints, tokens, account
numbers. There is no column in this file that could hold one. A future
adapter reads its secrets from the environment at connect time, so
there is nothing for a database dump, a backup or a dashboard export to
leak.

WHY ORDER STATE HISTORY IS ITS OWN TABLE
--------------------------------------------
Current state cannot answer the questions the history exists for: how
long an order worked before filling, whether it passed through UNKNOWN,
which event caused each move. Storing only the latest state would make
every one of those unanswerable the moment it mattered.

THE TWO UNIQUE INDEXES ARE THE IDEMPOTENCY GUARANTEE
--------------------------------------------------------
`idx_execution_orders_idem` and `idx_execution_fills_idem` are what
make duplicate protection survive a process restart. The in-memory
checks are the fast path; these are the ones that hold when the
process that held the memory is gone.
"""

from __future__ import annotations

import sqlite3


def initialize_execution_schema(conn: sqlite3.Connection) -> None:
    """Create the Phase 14 tables. Safe to call repeatedly."""

    # ---------------- brokers, accounts, connections ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS brokers (
            broker_id     TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            environment   TEXT NOT NULL DEFAULT 'paper',
            adapter       TEXT NOT NULL DEFAULT '',
            enabled       INTEGER NOT NULL DEFAULT 1,
            -- False for a venue that is named but has no adapter, so
            -- the UI can say "planned" instead of showing an absence.
            implemented   INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
            -- Deliberately no endpoint, credential or token column.
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS broker_accounts (
            account_id          TEXT PRIMARY KEY,
            broker_id           TEXT NOT NULL,
            name                TEXT NOT NULL,
            environment         TEXT NOT NULL DEFAULT 'paper',
            base_currency       TEXT NOT NULL DEFAULT 'USD',
            enabled             INTEGER NOT NULL DEFAULT 1,
            position_accounting TEXT NOT NULL DEFAULT 'netting',
            permissions_json    TEXT NOT NULL DEFAULT '[]',
            linked_reference    TEXT,
            created_at          TEXT,
            metadata_json       TEXT NOT NULL DEFAULT '{}'
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS broker_capability (
            broker_id            TEXT PRIMARY KEY,
            capability_json      TEXT NOT NULL DEFAULT '{}',
            position_accounting  TEXT NOT NULL DEFAULT 'netting',
            rate_limit_per_minute INTEGER,
            recorded_at          TEXT,
            notes                TEXT NOT NULL DEFAULT ''
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS broker_connection (
            broker_id         TEXT NOT NULL,
            at                TEXT NOT NULL,
            state             TEXT NOT NULL,
            attempts          INTEGER NOT NULL DEFAULT 0,
            last_heartbeat_at TEXT,
            detail            TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (broker_id, at)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS broker_health (
            broker_id            TEXT NOT NULL,
            at                   TEXT NOT NULL,
            state                TEXT NOT NULL,
            latency_ms           REAL,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            detail               TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (broker_id, at)
        )
    """)

    # ---------------- instruments ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS broker_instrument_mapping (
            canonical_instrument_id TEXT NOT NULL,
            broker_id               TEXT NOT NULL,
            broker_symbol           TEXT NOT NULL,
            venue                   TEXT NOT NULL DEFAULT '',
            asset_class             TEXT NOT NULL DEFAULT 'stock',
            currency                TEXT NOT NULL DEFAULT 'USD',
            tick_size               REAL,
            lot_size                REAL,
            minimum_quantity        REAL,
            quantity_increment      REAL,
            price_precision         INTEGER,
            contract_multiplier     REAL NOT NULL DEFAULT 1.0,
            timezone_name           TEXT NOT NULL DEFAULT 'UTC',
            trading_hours           TEXT NOT NULL DEFAULT '',
            tradable                INTEGER NOT NULL DEFAULT 1,
            broker_payload_json     TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (broker_id, canonical_instrument_id)
        )
    """)
    # The reverse lookup, for attributing an inbound broker event to an
    # instrument. Not unique: two canonical instruments could in
    # principle share a symbol at different venues.
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_mapping_symbol
        ON broker_instrument_mapping (broker_id, broker_symbol)
    """)

    # ---------------- orders ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS execution_orders (
            order_id            TEXT PRIMARY KEY,
            intent_id           TEXT NOT NULL,
            broker_id           TEXT NOT NULL,
            account_id          TEXT NOT NULL,
            instrument_id       TEXT NOT NULL,
            side                TEXT NOT NULL,
            quantity            REAL NOT NULL,
            order_type          TEXT NOT NULL DEFAULT 'market',
            time_in_force       TEXT NOT NULL DEFAULT 'day',
            limit_price         REAL,
            stop_price          REAL,

            state               TEXT NOT NULL,
            filled_quantity     REAL NOT NULL DEFAULT 0,
            average_fill_price  REAL,
            reject_code         TEXT,
            reject_detail       TEXT NOT NULL DEFAULT '',

            -- the six identifiers, kept apart on purpose
            idempotency_key     TEXT NOT NULL DEFAULT '',
            client_order_id     TEXT NOT NULL DEFAULT '',
            broker_order_id     TEXT,
            broker_symbol       TEXT NOT NULL DEFAULT '',

            -- provenance: the chain that answers "why did this happen"
            correlation_id      TEXT NOT NULL DEFAULT '',
            signal_id           TEXT,
            prediction_id       TEXT,
            model_version       TEXT,
            strategy_id         TEXT,
            portfolio_id        TEXT,
            decision_id         TEXT,
            execution_policy    TEXT NOT NULL DEFAULT 'market',
            environment         TEXT NOT NULL DEFAULT 'paper',

            intent_at           TEXT,
            validated_at        TEXT,
            submitted_at        TEXT,
            acknowledged_at     TEXT,
            terminal_at         TEXT,
            expires_at          TEXT,

            -- execution quality, for the analysis a later phase will do
            decision_price      REAL,
            reference_price     REAL,
            bid                 REAL,
            ask                 REAL,
            submitted_price     REAL,
            commission          REAL NOT NULL DEFAULT 0,
            fees                REAL NOT NULL DEFAULT 0,
            note                TEXT NOT NULL DEFAULT ''
        )
    """)
    # The restart-proof half of idempotency. In-memory checks are the
    # fast path; this is the one that survives the process.
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_orders_idem
        ON execution_orders (idempotency_key)
        WHERE idempotency_key != ''
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_execution_orders_state
        ON execution_orders (broker_id, account_id, state)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_execution_orders_broker_ref
        ON execution_orders (broker_id, broker_order_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_execution_orders_correlation
        ON execution_orders (correlation_id)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS order_state_history (
            order_id       TEXT NOT NULL,
            sequence       INTEGER NOT NULL,
            from_state     TEXT,
            to_state       TEXT NOT NULL,
            at             TEXT,
            reason         TEXT NOT NULL DEFAULT '',
            event_id       TEXT,
            correlation_id TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (order_id, sequence)
        )
    """)

    # ---------------- fills ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS execution_fills (
            fill_id          TEXT PRIMARY KEY,
            order_id         TEXT NOT NULL,
            broker_id        TEXT NOT NULL,
            account_id       TEXT NOT NULL,
            instrument_id    TEXT NOT NULL,
            side             TEXT NOT NULL,
            quantity         REAL NOT NULL,
            price            REAL NOT NULL,
            filled_at        TEXT,
            execution_id     TEXT,
            broker_order_id  TEXT,
            commission       REAL NOT NULL DEFAULT 0,
            fees             REAL NOT NULL DEFAULT 0,
            exchange_fees    REAL NOT NULL DEFAULT 0,
            financing        REAL NOT NULL DEFAULT 0,
            taxes            REAL NOT NULL DEFAULT 0,
            currency         TEXT NOT NULL DEFAULT 'USD',
            reference_price  REAL,
            liquidity        TEXT NOT NULL DEFAULT '',
            idempotency_key  TEXT NOT NULL DEFAULT '',
            correlation_id   TEXT NOT NULL DEFAULT '',
            -- Normalization is lossy; the lost part is often what
            -- explains a discrepancy, so the venue's own words are kept.
            raw_payload_json TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_fills_idem
        ON execution_fills (idempotency_key)
        WHERE idempotency_key != ''
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_execution_fills_order
        ON execution_fills (order_id)
    """)

    # ---------------- events (append-only) ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS execution_events (
            event_id        TEXT PRIMARY KEY,
            event_type      TEXT NOT NULL,
            at              TEXT,
            received_at     TEXT,
            source          TEXT NOT NULL DEFAULT 'system',
            broker_id       TEXT,
            account_id      TEXT,
            order_id        TEXT,
            broker_order_id TEXT,
            fill_id         TEXT,
            instrument_id   TEXT,
            correlation_id  TEXT NOT NULL DEFAULT '',
            idempotency_key TEXT NOT NULL DEFAULT '',
            sequence        INTEGER,
            payload_json    TEXT NOT NULL DEFAULT '{}'
        )
    """)
    # Survives a restart so a redelivery after reconnect — which is
    # exactly when redeliveries happen — is still recognised.
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_events_idem
        ON execution_events (idempotency_key)
        WHERE idempotency_key != ''
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_execution_events_order
        ON execution_events (order_id)
    """)

    # ---------------- reconciliation (append-only) ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS reconciliation_records (
            reconciliation_id  TEXT PRIMARY KEY,
            broker_id          TEXT NOT NULL,
            account_id         TEXT NOT NULL,
            at                 TEXT NOT NULL,
            scope              TEXT NOT NULL DEFAULT 'all',
            orders_compared    INTEGER NOT NULL DEFAULT 0,
            fills_compared     INTEGER NOT NULL DEFAULT 0,
            positions_compared INTEGER NOT NULL DEFAULT 0,
            checks_performed   INTEGER NOT NULL DEFAULT 0,
            is_clean           INTEGER NOT NULL DEFAULT 1,
            mismatches_json    TEXT NOT NULL DEFAULT '[]',
            correlation_id     TEXT NOT NULL DEFAULT '',
            detail             TEXT NOT NULL DEFAULT ''
        )
    """)

    # ---------------- errors and audit ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS execution_errors (
            error_id       TEXT PRIMARY KEY,
            at             TEXT,
            code           TEXT NOT NULL,
            message        TEXT NOT NULL DEFAULT '',
            broker_id      TEXT,
            account_id     TEXT,
            order_id       TEXT,
            correlation_id TEXT NOT NULL DEFAULT '',
            retryable      INTEGER NOT NULL DEFAULT 0,
            context_json   TEXT NOT NULL DEFAULT '{}'
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS execution_audit (
            audit_id       TEXT PRIMARY KEY,
            at             TEXT,
            action         TEXT NOT NULL,
            actor          TEXT NOT NULL,
            subject_type   TEXT NOT NULL DEFAULT '',
            subject_id     TEXT NOT NULL DEFAULT '',
            correlation_id TEXT NOT NULL DEFAULT '',
            detail         TEXT NOT NULL DEFAULT '',
            payload_json   TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_execution_audit_subject
        ON execution_audit (subject_type, subject_id)
    """)

    # ---------------- safety switches ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS execution_controls (
            control_key TEXT PRIMARY KEY,
            enabled     INTEGER NOT NULL DEFAULT 1,
            updated_at  TEXT,
            actor       TEXT NOT NULL DEFAULT 'system',
            reason      TEXT NOT NULL DEFAULT ''
            -- There is deliberately no 'live_execution_enabled' row.
            -- Real-money execution is not a flag in this phase, so
            -- there is nothing here to set.
        )
    """)

    conn.commit()
