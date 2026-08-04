"""
portfolio_history.py
------------------------
Portfolio History module for MarketLens.

RESPONSIBILITY:
Persist a daily snapshot of Portfolio Simulator's result, so its
evolution over time can be charted on the Dashboard. Uses the SAME
SQLite database file as NewsDatabase and RecommendationLog — one more
table, no new storage system.

WHY THIS EXISTS: Portfolio Simulator itself only ever computes a
single point-in-time snapshot ("if you'd invested $1000 in every BUY
call, you'd have $X TODAY"). Without persisting that result once per
run, there is no way to show how the simulated return has trended over
time since automation started — this module is exactly that missing
history log.
"""

import sqlite3
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

logger = logging.getLogger("marketlens.portfolio_history")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class PortfolioHistory:
    """
    Logs and retrieves daily Portfolio Simulator snapshots from a
    SQLite database.
    """

    def __init__(self, db_path: str):
        """
        Args:
            db_path: path to the SQLite database file (the SAME file
                used by NewsDatabase/RecommendationLog — SQLite allows
                multiple tables in one file). Use ":memory:" for a
                throwaway in-memory database (tests).
        """
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at TEXT NOT NULL,
                total_invested REAL,
                total_final_value REAL,
                total_return_pct REAL,
                trades_simulated INTEGER
            )
        """)
        self._conn.commit()

    def log_snapshot(self, portfolio_result: Dict[str, Any], recorded_at: Optional[str] = None) -> None:
        """
        Record one snapshot of a Portfolio Simulator result.

        Args:
            portfolio_result: the dict returned by
                PortfolioSimulator.simulate() — logged as-is, even when
                trades_simulated is 0 (a truthful "nothing to simulate
                yet" data point is more honest than skipping it, and
                shows the real ramp-up once automation starts
                accumulating checked recommendations).
            recorded_at: ISO timestamp for this snapshot; defaults to
                the current UTC time. Exposed for deterministic tests.
        """
        recorded_at = recorded_at or datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO portfolio_snapshots "
            "(recorded_at, total_invested, total_final_value, total_return_pct, trades_simulated) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                recorded_at,
                portfolio_result.get("total_invested"),
                portfolio_result.get("total_final_value"),
                portfolio_result.get("total_return_pct"),
                portfolio_result.get("trades_simulated"),
            ),
        )
        self._conn.commit()

    def load_all(self) -> List[Dict[str, Any]]:
        """
        Load every logged snapshot, oldest first — ready to feed
        directly into a line chart.
        """
        cursor = self._conn.execute("SELECT * FROM portfolio_snapshots ORDER BY recorded_at ASC")
        return [dict(row) for row in cursor.fetchall()]

    def close(self) -> None:
        """Close the underlying SQLite connection cleanly."""
        self._conn.close()
