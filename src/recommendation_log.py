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
                generated_at        TEXT NOT NULL,
                checked_at          TEXT,
                was_correct         INTEGER
            )
        """)
        self._conn.commit()
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """
        Safely add any column introduced by a NEWER version of this
        module to a `recommendations` table created by an OLDER one (a
        real, already-deployed database won't have it yet). Checked
        via PRAGMA table_info first, since `ALTER TABLE ADD COLUMN`
        fails outright if the column already exists — safe to run on
        every startup, on a brand-new database (where CREATE TABLE
        above already includes every column, so this is a no-op) or an
        existing one alike. Every row logged before a given migration
        simply gets NULL for that column — no data is lost or altered.

        v1.4 ADDITIONS — checked_at / was_correct: previously, Backtest
        Engine results were never written back to the log — every run
        recomputed outcomes for whichever historical rows happened to
        be "due" that day from scratch, and the Dashboard's "verified"
        badge ended up reflecting whichever row that was — possibly a
        much older logged call than the entity's CURRENT recommendation,
        which looked like inconsistent/wrong results in practice. These
        two columns let each row's outcome be checked exactly ONCE and
        remembered permanently (see mark_checked() and
        load_latest_verified_outcome_per_entity()).
        """
        existing_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(recommendations)")}
        needed_columns = {"time_horizon": "TEXT", "checked_at": "TEXT", "was_correct": "INTEGER"}
        migrated_any = False
        for column, sql_type in needed_columns.items():
            if column not in existing_columns:
                self._conn.execute(f"ALTER TABLE recommendations ADD COLUMN {column} {sql_type}")
                migrated_any = True
                logger.info("RecommendationLog: migrated schema — added '%s' column", column)
        if migrated_any:
            self._conn.commit()

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
        Load BUY/SELL (or STRONG_BUY/STRONG_SELL) recommendations that
        have NEVER been checked yet (`checked_at IS NULL`) and whose
        OWN appropriate holding period — based on EACH row's stored
        time_horizon — has fully elapsed. This is what makes a
        "long-term" call get checked after a long-term horizon and a
        "short-term" call get checked after a short one, instead of
        grading every recommendation on the same fixed clock regardless
        of what it actually claimed.

        WHY `checked_at IS NULL` MATTERS (v1.4): without this, the same
        historical row would be re-checked on every single run forever
        — wasteful, and (worse) it meant the Dashboard's "verified"
        badge for an entity could end up reflecting whichever
        historical row happened to be due on a given day, not
        necessarily its most recent one. Each row is now checked
        EXACTLY ONCE (see mark_checked()).

        Args:
            holding_period_days_by_horizon: e.g.
                {"short-term": 5, "mixed": 15, "long-term": 45} — how
                many days to wait for each possible time_horizon value
                before that recommendation is considered checkable.
            default_holding_period_days: used when a row's
                time_horizon is NULL (recommendations logged before
                that column existed) or not present in the mapping.

        Returns:
            The list of recommendation rows that are due for a
            Backtest Engine check right now.
        """
        now = datetime.now(timezone.utc)
        cursor = self._conn.execute(
            "SELECT * FROM recommendations "
            "WHERE recommendation IN ('BUY', 'SELL', 'STRONG_BUY', 'STRONG_SELL') AND checked_at IS NULL"
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

    def mark_checked(self, row_id: int, was_correct: Optional[bool], checked_at: Optional[str] = None) -> None:
        """
        Persist a Backtest Engine outcome back onto the SPECIFIC
        recommendation row it belongs to, so it is never re-checked
        again, and so the outcome stays permanently traceable to
        exactly which logged call it refers to.

        Args:
            row_id: the `id` of the row in the `recommendations` table
                (present in every dict returned by
                load_actionable_due_for_check()).
            was_correct: True/False if Backtest Engine could check it,
                None if it had to be skipped (e.g. no ticker, no price
                data available) — a skipped row still gets `checked_at`
                set (so it's not retried forever) but `was_correct`
                stays NULL, so it never falsely claims an outcome.
            checked_at: ISO timestamp; defaults to the current UTC time.
        """
        checked_at = checked_at or datetime.now(timezone.utc).isoformat()
        was_correct_int = None if was_correct is None else int(was_correct)
        self._conn.execute(
            "UPDATE recommendations SET checked_at = ?, was_correct = ? WHERE id = ?",
            (checked_at, was_correct_int, row_id),
        )
        self._conn.commit()

    def load_latest_verified_outcome_per_entity(self) -> Dict[str, bool]:
        """
        For each entity, return the outcome of its MOST RECENTLY
        LOGGED recommendation that has actually been checked (i.e.
        `was_correct IS NOT NULL`) — this is what the Dashboard's
        "verified" badge should reflect: one specific, identifiable
        prior call per entity, not whichever historical row happened
        to come due for checking on a given day (the confusing
        behavior this method replaces).

        Returns:
            entity -> True/False. An entity with no checked history at
            all is simply absent from the dict (not included as None)
            — the same convention Dashboard's verified_track_record
            parameter already expects.
        """
        cursor = self._conn.execute(
            "SELECT entity, was_correct, generated_at FROM recommendations "
            "WHERE was_correct IS NOT NULL ORDER BY generated_at ASC"
        )
        latest: Dict[str, bool] = {}
        for row in cursor.fetchall():
            # Ascending order means the LAST time we see a given entity
            # in this loop is its most recently generated checked row
            # — a plain overwrite naturally keeps only that one.
            latest[row["entity"]] = bool(row["was_correct"])
        return latest

    def close(self) -> None:
        """Close the underlying SQLite connection cleanly."""
        self._conn.close()
