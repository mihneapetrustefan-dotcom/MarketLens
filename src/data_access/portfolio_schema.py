"""
src/data_access/portfolio_schema.py
----------------------------------------
SQL persistence for Phase 11 (Portfolio Intelligence & Risk Engine).

WHY `portfolio_state_snapshots` AND NOT `portfolio_snapshots`
-----------------------------------------------------------------
`portfolio_snapshots` already exists and is written by the legacy
PortfolioHistory module on every daily run (89 rows today). It records
something genuinely different: the aggregate outcome of a hypothetical
"$1000 in every checked recommendation" simulation, with no positions
behind it.

Reusing that name would either break the daily pipeline or force two
unrelated meanings through one table. The Phase 11 snapshot is a
priced, position-level state of a real declared portfolio, so it gets
its own table and the legacy one keeps working untouched.

DECISIONS ARE APPEND-ONLY, LIKE SIGNALS
-------------------------------------------
A risk decision is never rewritten. It records what was decided, from
which snapshot, under which constraint version — so "why was this
allowed in March?" stays answerable after the thresholds change in
April. The same audit argument Phase 10 made for signals applies with
more force here, because these decisions are what eventually gate
money.

VIOLATIONS ARE A TABLE, NOT A JSON BLOB
-------------------------------------------
"How often does the sector cap bind?" and "which constraint rejects the
most proposals?" are the questions this layer exists to answer. Buried
in a serialized column they become log-grep exercises, so violations
get their own queryable rows — the same reasoning signal_suppressions
followed in Phase 10.

STILL NO EXECUTION ANYWHERE
-------------------------------
`order_intents` has no account, no venue, no broker reference, no
order id, no fill and no status beyond validity. Nothing in this phase
writes an execution record, because nothing in this phase can execute.

SAFE TO RUN REPEATEDLY: every statement is CREATE TABLE / CREATE INDEX
IF NOT EXISTS.
"""

import sqlite3


def initialize_portfolio_schema(conn: sqlite3.Connection) -> None:
    """Create the Phase 11 tables and indexes. Idempotent."""

    # ---------------- portfolios and positions ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS portfolios (
            portfolio_id   TEXT PRIMARY KEY,
            name           TEXT NOT NULL,
            base_currency  TEXT NOT NULL DEFAULT 'USD',
            cash           REAL NOT NULL DEFAULT 0.0,
            -- 'declared' | 'paper' | 'simulated'. There is deliberately
            -- no 'live' kind: this phase cannot source a live book.
            kind           TEXT NOT NULL DEFAULT 'declared',
            created_at     TEXT,
            metadata_json  TEXT NOT NULL DEFAULT '{}'
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            position_id          TEXT PRIMARY KEY,
            portfolio_id         TEXT NOT NULL,
            instrument_id        TEXT NOT NULL,
            -- Signed: negative is a short. One field, so there is no
            -- second flag that can disagree with it.
            quantity             REAL NOT NULL,
            average_entry_price  REAL,
            currency             TEXT NOT NULL DEFAULT 'USD',
            status               TEXT NOT NULL DEFAULT 'open',
            source               TEXT NOT NULL DEFAULT 'declared',
            opened_at            TEXT,
            closed_at            TEXT,
            realized_pnl         REAL,
            metadata_json        TEXT NOT NULL DEFAULT '{}'
        )
    """)

    # ---------------- snapshots ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_state_snapshots (
            snapshot_id        TEXT PRIMARY KEY,
            portfolio_id       TEXT NOT NULL,
            as_of              TEXT NOT NULL,
            base_currency      TEXT NOT NULL DEFAULT 'USD',
            cash               REAL NOT NULL DEFAULT 0.0,
            equity             REAL,
            gross_exposure     REAL,
            net_exposure       REAL,
            long_exposure      REAL,
            short_exposure     REAL,
            leverage           REAL,
            unrealized_pnl     REAL,
            realized_pnl       REAL,
            position_count     INTEGER NOT NULL DEFAULT 0,
            -- 0 when any open position could not be priced at as_of.
            -- Every weight derives from equity, so an incomplete
            -- snapshot must never be silently trusted.
            is_complete        INTEGER NOT NULL DEFAULT 1,
            unvalued_count     INTEGER NOT NULL DEFAULT 0,
            is_multi_currency  INTEGER NOT NULL DEFAULT 0,
            computed_at        TEXT,
            metrics_json       TEXT NOT NULL DEFAULT '{}'
        )
    """)

    # ---------------- constraints ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS risk_constraint_sets (
            version        TEXT PRIMARY KEY,
            name           TEXT NOT NULL,
            trading_state  TEXT NOT NULL DEFAULT 'enabled',
            created_at     TEXT,
            description    TEXT NOT NULL DEFAULT ''
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS risk_constraints (
            constraint_set_version TEXT NOT NULL,
            constraint_id          TEXT NOT NULL,
            scope                  TEXT NOT NULL,
            severity               TEXT NOT NULL DEFAULT 'hard',
            max_value              REAL,
            min_value              REAL,
            applies_to             TEXT,
            description            TEXT NOT NULL DEFAULT '',
            enabled                INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (constraint_set_version, constraint_id)
        )
    """)

    # ---------------- proposals ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS allocation_proposals (
            proposal_id        TEXT PRIMARY KEY,
            portfolio_id       TEXT NOT NULL,
            as_of              TEXT NOT NULL,
            sizing_strategy_id TEXT NOT NULL DEFAULT '',
            sizing_version     TEXT NOT NULL DEFAULT 'v1',
            note               TEXT NOT NULL DEFAULT '',
            created_at         TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS allocation_changes (
            proposal_id      TEXT NOT NULL,
            instrument_id    TEXT NOT NULL,
            current_weight   REAL,
            target_weight    REAL,
            current_quantity REAL,
            target_quantity  REAL,
            signal_id        TEXT,
            reason           TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (proposal_id, instrument_id)
        )
    """)

    # ---------------- decisions ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS risk_decisions (
            decision_id             TEXT PRIMARY KEY,
            portfolio_id            TEXT NOT NULL,
            proposal_id             TEXT,
            state                   TEXT NOT NULL,
            as_of                   TEXT NOT NULL,
            summary                 TEXT NOT NULL DEFAULT '',
            reasons_json            TEXT NOT NULL DEFAULT '[]',
            evaluated_scopes_json   TEXT NOT NULL DEFAULT '[]',
            skipped_scopes_json     TEXT NOT NULL DEFAULT '{}',

            -- provenance: what must match for a replay to reproduce this
            risk_engine_version     TEXT NOT NULL DEFAULT 'v1',
            constraint_set_version  TEXT NOT NULL DEFAULT 'v1',
            sizing_version          TEXT,
            snapshot_as_of          TEXT,
            information_cutoff      TEXT,
            price_data_as_of        TEXT,
            provenance_inputs_json  TEXT NOT NULL DEFAULT '{}',

            metrics_json            TEXT NOT NULL DEFAULT '{}',
            created_at              TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS risk_violations (
            decision_id    TEXT NOT NULL,
            constraint_id  TEXT NOT NULL,
            scope          TEXT NOT NULL,
            severity       TEXT NOT NULL,
            message        TEXT NOT NULL DEFAULT '',
            observed_value REAL,
            current_value  REAL,
            limit_value    REAL,
            applies_to     TEXT,
            -- 1 when the engine resolved the breach itself (by trimming
            -- the change back inside the limit). Kept so "how often
            -- does this cap bind?" stays answerable, while separating
            -- a fixed breach from one that actually rejected.
            remediated     INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (decision_id, constraint_id, scope, applies_to)
        )
    """)

    # ---------------- the execution boundary (inert) ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS order_intents (
            intent_id        TEXT PRIMARY KEY,
            portfolio_id     TEXT NOT NULL,
            instrument_id    TEXT NOT NULL,
            side             TEXT NOT NULL,
            target_weight    REAL,
            target_quantity  REAL,
            source_signal_id TEXT,
            decision_id      TEXT,
            reason           TEXT NOT NULL DEFAULT '',
            created_at       TEXT,
            valid_until      TEXT
            -- No account, venue, broker, order id, fill or execution
            -- status. Adding any of those is a later phase's decision,
            -- made deliberately, not a column that quietly appeared.
        )
    """)

    # ---------------- indexes: one per named query pattern ----------------

    conn.execute("CREATE INDEX IF NOT EXISTS idx_positions_portfolio ON positions(portfolio_id, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_positions_instrument ON positions(instrument_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_portfolio ON portfolio_state_snapshots(portfolio_id, as_of)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_constraints_set ON risk_constraints(constraint_set_version)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_proposals_portfolio ON allocation_proposals(portfolio_id, as_of)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_portfolio ON risk_decisions(portfolio_id, as_of)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_state ON risk_decisions(state)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_proposal ON risk_decisions(proposal_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_violations_scope ON risk_violations(scope)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_violations_constraint ON risk_violations(constraint_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_intents_portfolio ON order_intents(portfolio_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_intents_decision ON order_intents(decision_id)")

    conn.commit()
