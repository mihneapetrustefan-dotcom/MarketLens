"""
time_decay.py
----------------
Time Decay module for MarketLens.

RESPONSIBILITY:
Compute how much an article's information should still "count" as time
passes, so that Confidence Score properly favors recent news over old
news. Implements exponential decay: an article's weight halves every
`half_life_hours` hours since it was collected.

WHY THIS MODULE EXISTS: once Database persists articles across many
days, Confidence Score's simple counts/averages would otherwise treat
a 2-month-old article exactly the same as one from this morning —
clearly wrong for a system meant to reflect CURRENT market sentiment.
This module is the fix, consumed by Confidence Score wherever articles
are aggregated across time.
"""

from datetime import datetime, timezone
from typing import Optional, Union, Dict


class TimeDecayCalculator:
    """
    Computes an exponential recency weight for an article, in (0.0, 1.0].
    """

    def __init__(self, half_life_hours: float = 480.0, category_half_lives: Optional[Dict[str, float]] = None):
        """
        Args:
            half_life_hours: DEFAULT number of hours after which an
                article's weight drops to 0.5, used for any category
                not listed in `category_half_lives` (or when no
                category is given at all). Default 480h (20 days) —
                chosen so that historically-backfilled news (via Google
                News Historical Backfill, up to ~60 days back) still
                contributes meaningfully: a story from a month ago
                retains roughly 35% of its original weight, rather than
                being almost entirely discounted. (An earlier default
                of 72h/3 days was tried first, but was found to
                discount month-old backfilled articles too heavily —
                down to single-digit percent weight — undermining the
                very purpose of pulling in historical coverage.)
            category_half_lives: optional per-category override, e.g.
                {"crypto": 120.0} — crypto markets move much faster
                than traditional stocks/BVB, so a month-old crypto
                story arguably should fade faster than a month-old
                earnings story. Any category not present here falls
                back to `half_life_hours`. Empty/omitted by default,
                so a plain `TimeDecayCalculator()` behaves exactly as
                before this parameter was added.
        """
        if half_life_hours <= 0:
            raise ValueError("half_life_hours must be positive")
        for category, value in (category_half_lives or {}).items():
            if value <= 0:
                raise ValueError(f"category_half_lives['{category}'] must be positive, got {value}")
        self.half_life_hours = half_life_hours
        self.category_half_lives = category_half_lives or {}

    def _half_life_for(self, category: Optional[str]) -> float:
        """Look up the half-life to use for a given category, falling back to the default."""
        if category and category in self.category_half_lives:
            return self.category_half_lives[category]
        return self.half_life_hours

    def _parse_timestamp(self, value: Union[str, datetime, None]) -> Optional[datetime]:
        """
        Parse a timestamp that may already be a datetime, an ISO
        string, or missing/malformed — never raises, returns None for
        anything it can't confidently parse (consistent with the
        resilience discipline used throughout MarketLens' collectors).
        """
        if value is None:
            return None
        if isinstance(value, datetime):
            dt = value
        else:
            try:
                dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def compute_weight(
        self,
        collected_at: Union[str, datetime, None],
        reference_time: Optional[datetime] = None,
        category: Optional[str] = None,
    ) -> float:
        """
        Compute the recency weight for one article.

        Args:
            collected_at: the article's `collected_at` timestamp (ISO
                string or datetime).
            reference_time: the "now" to measure age against. Defaults
                to the actual current UTC time; exposed as a parameter
                so callers (and tests) can get fully deterministic
                results without depending on real wall-clock time, and
                so a report could be regenerated "as of" a specific
                past moment if ever needed.
            category: optional market category (e.g. "crypto",
                "stocks", "bvb") — if it matches a key in
                `category_half_lives`, that half-life is used instead
                of the default. Omitted or unmatched categories use
                `half_life_hours` as before.

        Returns:
            A weight in (0.0, 1.0]. A missing or unparseable timestamp
            returns 1.0 (full weight) rather than 0 — treating
            "unknown age" as "don't penalize it" is safer than silently
            discarding an article's influence entirely due to a data
            quality issue elsewhere in the pipeline.
        """
        timestamp = self._parse_timestamp(collected_at)
        if timestamp is None:
            return 1.0

        reference = reference_time or datetime.now(timezone.utc)
        age_hours = (reference - timestamp).total_seconds() / 3600.0
        if age_hours < 0:
            # An article that appears to be "from the future" (clock
            # skew between machines) is treated as brand new, not
            # given a weight above 1.0.
            age_hours = 0.0

        half_life = self._half_life_for(category)
        return 0.5 ** (age_hours / half_life)
