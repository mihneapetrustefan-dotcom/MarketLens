"""
src/data_access/adaptive_allocation_schema.py
--------------------------------------------------------
Durable record of shadow allocation plans (Phase 25.9I-D).

TWO TABLES, NOT FOUR
------------------------
The phase brief offered `adaptive_allocation_plans`,
`adaptive_allocation_lines`, `adaptive_allocation_constraints` and
`adaptive_risk_budget_snapshots`, and said to create only what is
justified. Constraints and risk-budget state are properties OF a plan
and OF a line: splitting them into their own tables would add two joins
and two chances for a partial write, to store data that is never queried
independently. They are stored as JSON on the rows that own them, which
is the same choice `adaptive_admission_decisions` made in 25.9I-B for
score components.

WHAT IS NEVER WRITTEN
-------------------------
Orders, intents, fills, positions, targets, or any table the canonical
Portfolio, Risk or Execution layers own. A shadow plan is a proposal
about money, not a movement of it. `src/adaptive/` imports none of those
packages, so there is no path from a plan to a venue.

APPEND-ONLY AND IDEMPOTENT
------------------------------
Plan ids derive from (as_of, config version, revision, candidate set), so
re-running the same allocation writes the same row and `INSERT OR IGNORE`
makes the repeat a no-op. A revision is a new plan naming its
predecessor; nothing updates a plan that has been written, so an earlier
proposal stays readable after a later one replaces it.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = "25.9I-D.1"


def initialize_adaptive_allocation_schema(conn: sqlite3.Connection) -> None:
    """Create the shadow-allocation tables. Safe to call repeatedly."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS adaptive_allocation_plans (
            plan_id              TEXT PRIMARY KEY,
            as_of                TEXT NOT NULL,
            status               TEXT NOT NULL,
            revision             INTEGER NOT NULL,
            previous_plan_id     TEXT,
            account_equity       REAL NOT NULL,
            cash                 REAL NOT NULL,
            base_currency        TEXT,
            deployable_capital   REAL NOT NULL,
            deployed             REAL NOT NULL,
            reserved             REAL NOT NULL,
            unallocated          REAL NOT NULL,
            capital_above_ceiling REAL NOT NULL,
            total_capital_at_risk REAL,
            risk_budget_total    REAL,
            risk_budget_allocated REAL,
            config_version       TEXT,
            session_snapshot_id  TEXT,
            lineage_id           TEXT,
            cash_reserves        TEXT,
            constraints          TEXT,
            gaps                 TEXT,
            notes                TEXT,
            created_at           TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS adaptive_allocation_lines (
            plan_id              TEXT NOT NULL,
            instrument_id        TEXT NOT NULL,
            status               TEXT NOT NULL,
            sector_id            TEXT,
            requested_notional   REAL NOT NULL,
            allocated_notional   REAL NOT NULL,
            capital_at_risk      REAL,
            portfolio_weight     REAL,
            risk_status          TEXT NOT NULL,
            risk_per_dollar      REAL,
            desirability         REAL NOT NULL,
            desirability_completeness REAL,
            desirability_positives TEXT,
            desirability_penalties TEXT,
            desirability_unavailable TEXT,
            constraints_applied  TEXT,
            rejection_reasons    TEXT,
            reasons              TEXT,
            session_snapshot_id  TEXT,
            admission_decision_id TEXT,
            PRIMARY KEY (plan_id, instrument_id)
        );

        CREATE INDEX IF NOT EXISTS idx_adaptive_alloc_lines_instrument
            ON adaptive_allocation_lines (instrument_id);
        CREATE INDEX IF NOT EXISTS idx_adaptive_alloc_plans_session
            ON adaptive_allocation_plans (session_snapshot_id);
    """)
    conn.commit()


#: Tables this phase owns, for the safety tests.
ADAPTIVE_ALLOCATION_TABLES = (
    "adaptive_allocation_plans",
    "adaptive_allocation_lines",
)
