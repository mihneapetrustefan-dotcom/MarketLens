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
table, in the same database), but is kept as a SEPARATE class —
logging recommendations and storing articles are different
responsibilities, even though they share physical storage for
simplicity (one file).

DESIGN DECISION — ticker lookup at logging time, not backtest time:
Recommendations are keyed by entity NAME ("Tesla"), but Backtesting
needs an actual TICKER SYMBOL ("TSLA") to fetch real price data. Rather
than re-deriving that mapping later, it's resolved once, at logging
time, via an injectable lookup dict (naturally built from
ticker_registry.py's data). Entities with no known ticker are still
logged (ticker column NULL) — they just can't be backtested later,
which the Backtest Engine handles gracefully rather than erroring.

CHANGE LOG (v1.1) — WHY time_horizon IS NOW STORED:
Previously, every recommendation was checked by Backtest Engine after
the exact same fixed holding period, regardless of whether it was
tagged short-term or long-term — meaning a "long-term BUY" got
graded as "wrong" after a mere 5 days of short-term noise, before its
actual thesis had any real chance to play out. Storing each
recommendation's time_horizon at logging time lets it later be
checked after ITS OWN appropriate holding period (see
load_actionable_due_for_check() and BacktestEngine's
holding_period_days_by_horizon). The schema migration below adds this
column safely to an EXISTING, already-populated database — real
accumulated history is preserved, not wiped.
"""

import sqlite3
import logging
from datetime import datetime, timedelta, timezone
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
        """Create the `recommendations` table if it doesn't already exist, and migrate older tables to the current schema."""
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS recommendations (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                entity              TEXT NOT NULL,
                ticker              TEXT,
                recommendation      TEXT NOT NULL,
                confidence_score    REAL,
                time_horizon        TEXT,
                generated_at        TEXT NOT NULL
            )
        """)
        self._conn.commit()
        self._migrate_add_time_horizon_column()

    def _migrate_add_time_horizon_column(self) -> None:
        """
        Safely add the `time_horizon` column to a `recommendations`
        table created by an OLDER version of this module (a real,
        already-deployed database won't have it yet). Checked via
        PRAGMA table_info first, since `ALTER TABLE ADD COLUMN` fails
        outright if the column already exists — this makes the
        migration safe to run on every startup, on a brand-new
        database (where CREATE TABLE above already included the
        column, so this is a no-op) or an existing one alike. Every
        row logged before this migration simply has `time_horizon =
        NULL`, which load_actionable_due_for_check() treats as "use
        the default holding period" — no data is lost or altered.
        """
        existing_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(recommendations)")}
        if "time_horizon" not in existing_columns:
            self._conn.execute("ALTER TABLE recommendations ADD COLUMN time_horizon TEXT")
            self._conn.commit()
            logger.info("RecommendationLog: migrated schema — added 'time_horizon' column")

    def log_recommendations(
        self,
        recommendations: List[Dict[str, Any]],
        ticker_lookup: Optional[Dict[str, str]] = None,
    ) -> int:
        """
        Persist a batch of recommendations (the output of
        RecommendationEngine.recommend_all(), optionally already
        tagged with `time_horizon` by TimeHorizonClassifier), stamped
        with the current UTC time.

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
                   (entity, ticker, recommendation, confidence_score, time_horizon, generated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (rec["entity"], ticker, rec["recommendation"], rec["confidence_score"], rec.get("time_horizon"), generated_at),
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

        KEPT for backward compatibility (a single, fixed cutoff for
        every recommendation regardless of its time_horizon). Prefer
        load_actionable_due_for_check() for new code — it judges each
        recommendation against ITS OWN declared horizon instead.
        """
        cursor = self._conn.execute(
            "SELECT * FROM recommendations WHERE recommendation IN ('BUY', 'SELL', 'STRONG_BUY', 'STRONG_SELL') AND generated_at <= ?",
            (cutoff_iso,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def load_actionable_due_for_check(
        self,
        holding_period_days_by_horizon: Dict[str, int],
        default_holding_period_days: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Load BUY/SELL recommendations whose OWN appropriate holding
        period — based on EACH row's stored time_horizon — has fully
        elapsed. This is what makes a "long-term" call get checked
        after a long-term horizon and a "short-term" call get checked
        after a short one, instead of grading every recommendation on
        the same fixed clock regardless of what it actually claimed.

        Args:
            holding_period_days_by_horizon: e.g.
                {"short-term": 5, "mixed": 15, "long-term": 45} — how
                many days to wait for each possible time_horizon value
                before that recommendation is considered checkable.
            default_holding_period_days: used when a row's
                time_horizon is NULL (recommendations logged before
                this column existed) or not present in the mapping —
                same 5-day behavior as the original, single-cutoff
                method, so old pending rows aren't disrupted by this
                change.

        Returns:
            The list of recommendation rows that are due for a
            Backtest Engine check right now.
        """
        now = datetime.now(timezone.utc)
        cursor = self._conn.execute(
            "SELECT * FROM recommendations WHERE recommendation IN ('BUY', 'SELL', 'STRONG_BUY', 'STRONG_SELL')"
        )

        due = []
        for row in cursor.fetchall():
            rec = dict(row)
            horizon = rec.get("time_horizon")
            days = holding_period_days_by_horizon.get(horizon, default_holding_period_days)
            try:
                generated_at = datetime.fromisoformat(str(rec["generated_at"]).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if now - generated_at >= timedelta(days=days):
                due.append(rec)
        return due

    def close(self) -> None:
        """Close the underlying SQLite connection cleanly."""
        self._conn.close()
