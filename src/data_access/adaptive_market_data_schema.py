"""
src/data_access/adaptive_market_data_schema.py
----------------------------------------------------------
The local record of what market data was asked for, and what it cost
(Phase 25.9I-C).

WHY A LOCAL LEDGER EXISTS AT ALL
------------------------------------
IBKR's own snapshot counter is "updated once per business day at end of
day, not in real time during the trading session" (ticket #561227). So
during the session -- exactly when a runaway would happen -- the
provider's meter reads zero.

A full uncontrolled session under the conservative counting basis is
about 12,000 requests. If the cap did not apply, that is roughly USD
120 spent before the portal showed a single digit of it. The ledger is
the only intraday meter that exists, and it is why a bounded validation
run can be bounded at all.

FOUR TABLES, EACH ANSWERING A DIFFERENT QUESTION
----------------------------------------------------
    adaptive_market_data_requests           what was asked for, one row
                                            per billable request
    adaptive_market_data_exchange_usage     the per-venue, per-month
                                            rollup, and the place where
                                            IBKR's next-day counter is
                                            later reconciled against it
    adaptive_market_data_state_transitions  every COLD/WARM/HOT move
                                            with its reason
    adaptive_market_data_entitlements       what the provider said to
                                            expect, beside what was
                                            actually observed

The usage rollup is NOT redundant with the request rows. The rows are
the evidence; the rollup is what a cap decision reads on the hot path,
and what a next-day reconciliation compares against a portal figure.

WHAT IS NEVER WRITTEN
-------------------------
No order, intent, fill, position or execution row. No credential, token,
session cookie or account identifier -- the ledger records an exchange
and an instrument, never who asked for it at the broker. `src/adaptive/`
imports no execution or trading module and the import boundary is
asserted by test.

APPEND-ONLY AND IDEMPOTENT
------------------------------
Request ids derive from content (provider, exchange, instrument,
timestamp, type, purpose), so replaying a validation run writes the same
rows and `INSERT OR IGNORE` makes the repeat a no-op. Usage rollups are
the one exception and are recomputed from the rows rather than
incremented blindly, so a partial write cannot leave a counter that
drifts away from its evidence.

MONTHS ARE NEVER DELETED
----------------------------
`billing_period` is part of every key. October's counters start at zero
because they are different ROWS, not because September's were cleared
(spec section 10).
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = "25.9I-C.1"


def initialize_adaptive_market_data_schema(conn: sqlite3.Connection) -> None:
    """Create the market-data governance tables. Safe to call repeatedly."""
    conn.executescript("""
        -- One row per billable request. The intraday meter.
        CREATE TABLE IF NOT EXISTS adaptive_market_data_requests (
            request_id            TEXT PRIMARY KEY,
            provider              TEXT NOT NULL,
            exchange              TEXT NOT NULL,
            instrument_id         TEXT NOT NULL DEFAULT '',
            requested_at          TEXT NOT NULL,
            billing_period        TEXT NOT NULL,          -- YYYY-MM
            request_type          TEXT NOT NULL,          -- snapshot | stream | probe
            counting_basis        TEXT NOT NULL DEFAULT 'unknown',
            request_units         INTEGER NOT NULL DEFAULT 1,
            estimated_cost_before_cap_usd REAL,
            estimated_billed_cost_usd     REAL,
            cap_state             TEXT NOT NULL DEFAULT 'unknown',
            activation_reason     TEXT NOT NULL DEFAULT '',
            requester             TEXT NOT NULL DEFAULT '',
            purpose               TEXT NOT NULL DEFAULT '',
            priority              TEXT NOT NULL DEFAULT '',
            -- lineage, carried never derived
            lineage_id            TEXT,
            session_snapshot_id   TEXT,
            admission_decision_id TEXT,
            allocation_plan_id    TEXT,
            thesis_id             TEXT,
            validation_run_id     TEXT,
            policy_version        TEXT NOT NULL DEFAULT '',
            note                  TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_md_requests_period
            ON adaptive_market_data_requests (billing_period, exchange);
        CREATE INDEX IF NOT EXISTS idx_md_requests_at
            ON adaptive_market_data_requests (requested_at);
        CREATE INDEX IF NOT EXISTS idx_md_requests_run
            ON adaptive_market_data_requests (validation_run_id);

        -- Per venue, per calendar month. Recomputed from the rows above.
        CREATE TABLE IF NOT EXISTS adaptive_market_data_exchange_usage (
            provider              TEXT NOT NULL,
            billing_period        TEXT NOT NULL,
            exchange              TEXT NOT NULL,
            request_count         INTEGER NOT NULL DEFAULT 0,
            estimated_cost_usd    REAL,
            uncapped_cost_usd     REAL,
            cap_state             TEXT NOT NULL DEFAULT 'unknown',
            requests_until_cap    INTEGER,
            first_request_at      TEXT,
            last_request_at       TEXT,
            -- Reconciliation against the provider's own next-day counter.
            -- NULL until a human or a read-only integration supplies it;
            -- NULL means "not yet compared", never "agreed".
            portal_reported_count INTEGER,
            portal_reported_cost_usd REAL,
            reconciled_at         TEXT,
            reconciliation_note   TEXT NOT NULL DEFAULT '',
            updated_at            TEXT NOT NULL,
            PRIMARY KEY (provider, billing_period, exchange)
        );

        -- Every COLD/WARM/HOT move, with the reason it happened.
        CREATE TABLE IF NOT EXISTS adaptive_market_data_state_transitions (
            transition_id         TEXT PRIMARY KEY,
            instrument_id         TEXT NOT NULL,
            exchange              TEXT NOT NULL DEFAULT '',
            previous_state        TEXT NOT NULL,
            new_state             TEXT NOT NULL,
            at                    TEXT NOT NULL,
            activation_reason     TEXT NOT NULL DEFAULT '',
            deactivation_reason   TEXT NOT NULL DEFAULT '',
            requesters            TEXT NOT NULL DEFAULT '',
            reasons               TEXT NOT NULL DEFAULT '',
            priority              TEXT NOT NULL DEFAULT '',
            estimated_cost_usd    REAL,
            budget_policy         TEXT NOT NULL DEFAULT '',
            expires_at            TEXT,
            lineage_id            TEXT,
            session_snapshot_id   TEXT,
            note                  TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_md_transitions_instrument
            ON adaptive_market_data_state_transitions (instrument_id, at);

        -- Provider expectation beside account observation. Never merged.
        CREATE TABLE IF NOT EXISTS adaptive_market_data_entitlements (
            provider              TEXT NOT NULL,
            exchange              TEXT NOT NULL,
            billing_period        TEXT NOT NULL,
            -- what the provider said would happen after the cap
            expected_after_cap    TEXT NOT NULL DEFAULT '',
            policy_confidence     TEXT NOT NULL DEFAULT 'unknown',
            cap_state             TEXT NOT NULL DEFAULT 'unknown',
            -- what this account actually did
            observed              TEXT NOT NULL DEFAULT 'unknown',
            observed_marker       TEXT NOT NULL DEFAULT '',
            observed_at           TEXT,
            observation_count     INTEGER NOT NULL DEFAULT 0,
            realtime_verified     INTEGER NOT NULL DEFAULT 0,
            expectation_unmet     INTEGER NOT NULL DEFAULT 0,
            updated_at            TEXT NOT NULL,
            note                  TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (provider, exchange, billing_period)
        );
    """)
    conn.commit()
