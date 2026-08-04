"""
recommendation_log.py
------------------------
Recommendation Log module for MarketLens.

RESPONSIBILITY:
Persist every recommendation MarketLens ever issues, with a timestamp,
so Backtesting can later look back and check what actually happened to
the price afterward. Without this log, a recommendation is a one-off
in-memory result that vanishes the moment the notebook session ends —
there would be nothing left to check against reality days later.

DESIGN DECISION: uses the same SQLite FILE as NewsDatabase (a second
table, in the same database on Google Drive), but is kept as a
SEPARATE class — logging recommendations and storing articles are
different responsibilities, even though they share physical storage
for simplicity (one file, one Drive folder).

DESIGN DECISION — ticker lookup at logging time, not backtest time:
Recommendations are keyed by entity NAME ("Tesla"), but Backtesting
needs an actual TICKER SYMBOL ("TSLA") to fetch real price data. Rather
than re-deriving that mapping later, it's resolved once, at logging
time, via an injectable lookup dict (naturally built from
ticker_registry.py's data). Entities with no known ticker are still
logged (ticker column NULL) — they just can't be backtested later,
which the Backtest Engine handles gracefully rather than erroring.
"""

import sqlite3
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

logger = logging.getLogger("marketlens.recommendation_log")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class RecommendationLog:
    """
    SQLite-backed log of every recommendation MarketLens has issued,
    with the timestamp it was issued at.
    """

    def __init__(self, db_path: str):
        """
        Args:
            db_path: filesystem path to the SQLite database file
                (typically the SAME path used for NewsDatabase). Pass
                ":memory:" for a temporary, in-process database (used
                by this module's own unit tests).
        """
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        """Create the `recommendations` table if it doesn't already exist."""
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS recommendations (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                entity              TEXT NOT NULL,
                ticker              TEXT,
                recommendation      TEXT NOT NULL,
                confidence_score    REAL,
                generated_at        TEXT NOT NULL
            )
        """)
        self._conn.commit()

    def log_recommendations(
        self,
        recommendations: List[Dict[str, Any]],
        ticker_lookup: Optional[Dict[str, str]] = None,
    ) -> int:
        """
        Persist a batch of recommendations (the output of
        RecommendationEngine.recommend_all()), stamped with the
        current UTC time.

        Args:
            ticker_lookup: optional dict mapping entity name -> ticker
                symbol. Entities not found in the lookup are still
                logged, with `ticker` set to NULL.

        Returns:
            The number of recommendations logged.
        """
        ticker_lookup = ticker_lookup or {}
        generated_at = datetime.now(timezone.utc).isoformat()

        for rec in recommendations:
            ticker = ticker_lookup.get(rec["entity"])
            self._conn.execute(
                """INSERT INTO recommendations
                   (entity, ticker, recommendation, confidence_score, generated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (rec["entity"], ticker, rec["recommendation"], rec["confidence_score"], generated_at),
            )
        self._conn.commit()

        logger.info("RecommendationLog: %d recommendation(s) logged at %s", len(recommendations), generated_at)
        return len(recommendations)

    def load_all(self) -> List[Dict[str, Any]]:
        """Load every recommendation ever logged."""
        cursor = self._conn.execute("SELECT * FROM recommendations")
        return [dict(row) for row in cursor.fetchall()]

    def load_actionable_before(self, cutoff_iso: str) -> List[Dict[str, Any]]:
        """
        Load BUY/SELL recommendations logged AT OR BEFORE `cutoff_iso`
        — i.e. old enough that their outcome could plausibly be
        checked already. HOLD recommendations are excluded since
        Backtest Engine only evaluates directional calls.
        """
        cursor = self._conn.execute(
            "SELECT * FROM recommendations WHERE recommendation IN ('BUY', 'SELL') AND generated_at <= ?",
            (cutoff_iso,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def close(self) -> None:
        """Close the underlying SQLite connection cleanly."""
        self._conn.close()
