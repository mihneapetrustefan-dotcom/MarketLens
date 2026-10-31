"""
src/research/intraday_context.py
-------------------------------------------
News/event alignment and normalization for intraday research
(Phase 25.9F, §32-§34, §36-§38).

THE JOIN RULE, AND WHY IT IS NOT THE EVENT DATE
---------------------------------------------------
An event that happened at 09:15 and reached this system at 11:40 was
not knowable at 09:20. Joining on the event's own timestamp would hand
a model information the system did not have -- the most ordinary way
an intraday study leaks, because the event date looks like the obvious
key.

So the join is on AVAILABILITY: `available_at <= T`, using the same
notion of "knowable" Phase 21 established and Phase 25.9D enforced for
experiment cohorts. Where a record carries both, the later of the two
wins, because a system cannot act on news before it has ingested it.

NORMALIZATION IS FITTED ON TRAINING ROWS ONLY
-------------------------------------------------
`fit_scaler` takes the rows a caller has already split off as training
data and returns a transform. Nothing here can see a validation row,
so the standard leak -- z-scoring the whole dataset and then splitting
-- is not available through this API.

OUTLIERS ARE FLAGGED, NOT DELETED
-------------------------------------
`winsorized()` returns the clipped value AND the raw one AND a count,
because silently discarding a 12-sigma minute is discarding exactly
the observation most worth explaining (§38).
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: How far back a "recent news" feature looks. Versioned with the
#: feature set: changing it changes what every stored value meant.
DEFAULT_NEWS_WINDOW = timedelta(hours=24)

#: Half-life of the recency weight, stated rather than tuned. It was
#: NOT fitted against any label, protected or otherwise (§34).
DEFAULT_DECAY_HALF_LIFE = timedelta(hours=6)


def _utc(raw: Any) -> Optional[datetime]:
    if raw in (None, ""):
        return None
    try:
        moment = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None \
        else moment.astimezone(timezone.utc)


def _sentiment_of(raw: Any) -> Optional[float]:
    """The numeric score out of Phase 3's JSON sentiment blob."""
    if not raw:
        return None
    try:
        import json
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, dict):
        score = parsed.get("score")
        return float(score) if isinstance(score, (int, float)) else None
    return None


@dataclass(frozen=True)
class NewsRecord:
    """One item of news, with the moment it became usable."""
    article_id: str
    instrument_id: str
    available_at: datetime
    published_at: Optional[datetime] = None
    sentiment: Optional[float] = None
    source_name: str = ""

    def age_minutes(self, cutoff: datetime) -> float:
        return (cutoff - self.available_at).total_seconds() / 60.0


def load_news(conn: sqlite3.Connection, instrument_id: str,
              window: timedelta = DEFAULT_NEWS_WINDOW,
              until: Optional[datetime] = None) -> List[NewsRecord]:
    """
    News for one instrument, keyed on when it became AVAILABLE.

    Reads the existing article/entity tables. A record whose
    availability cannot be established is DROPPED, not assumed
    available: an undated article cannot be proven to have been
    knowable, and including it is the silent leak.
    """
    try:
        # The canonical chain Phase 8 already uses: an article is linked
        # to a COMPANY, and an instrument belongs to a security of that
        # company. Reused rather than re-derived, so intraday news means
        # exactly what daily news means.
        rows = conn.execute("""
            SELECT a.article_id, a.published_at, a.collected_at, a.source,
                   a.sentiment
              FROM articles a
              JOIN article_entities ae ON ae.article_id = a.article_id
              JOIN companies co ON co.company_id = ae.entity_id
              JOIN securities s ON s.company_id = co.company_id
              JOIN instruments i ON i.security_id = s.security_id
             WHERE i.instrument_id = ?
             ORDER BY COALESCE(a.collected_at, a.published_at)
        """, (instrument_id,)).fetchall()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error).lower() or "no such column" in str(error).lower():
            return []
        raise

    out: List[NewsRecord] = []
    for article_id, published_at, collected_at, source_name, sentiment_json in rows:
        published = _utc(published_at)
        collected = _utc(collected_at)
        # The LATER of the two: a system cannot act on an article before
        # it has ingested it, and cannot have ingested it before it was
        # published.
        candidates = [m for m in (published, collected) if m is not None]
        if not candidates:
            continue
        available = max(candidates)
        if until is not None and available > until:
            continue
        out.append(NewsRecord(
            article_id=str(article_id), instrument_id=instrument_id,
            available_at=available, published_at=published,
            sentiment=_sentiment_of(sentiment_json),
            source_name=str(source_name or "")))
    return out


def news_features(records: Sequence[NewsRecord], cutoff: datetime,
                  window: timedelta = DEFAULT_NEWS_WINDOW,
                  half_life: timedelta = DEFAULT_DECAY_HALF_LIFE
                  ) -> Dict[str, Optional[float]]:
    """
    Point-in-time news context at `cutoff`.

    Every record is filtered on `available_at <= cutoff` HERE as well as
    at load time -- the same defence-in-depth the feature context uses,
    so a caller who passed an unfiltered list still cannot leak.
    """
    anchor = cutoff.astimezone(timezone.utc)
    visible = [r for r in records
               if r.available_at <= anchor and anchor - r.available_at <= window]

    if not visible:
        # Zero articles genuinely IS zero -- this is the
        # ZERO_IS_SEMANTIC case Phase 8 made authors declare -- but
        # "minutes since" has no value when nothing has arrived.
        return {"news.count_24h": 0.0, "news.minutes_since_last": None,
                "news.decayed_intensity_24h": 0.0,
                "news.distinct_sources_24h": 0.0,
                "news.mean_sentiment_24h": None}

    newest = max(r.available_at for r in visible)
    half_life_minutes = half_life.total_seconds() / 60.0
    intensity = sum(0.5 ** (r.age_minutes(anchor) / half_life_minutes)
                    for r in visible)
    return {
        "news.count_24h": float(len(visible)),
        "news.minutes_since_last": (anchor - newest).total_seconds() / 60.0,
        "news.decayed_intensity_24h": intensity,
        "news.distinct_sources_24h": float(len({r.source_name for r in visible
                                                if r.source_name})),
        "news.mean_sentiment_24h": (
            statistics.fmean(scores) if (scores := [r.sentiment for r in visible
                                                    if r.sentiment is not None])
            else None),
    }


# ======================================================================
# Normalization (§36, §37)
# ======================================================================

@dataclass
class Scaler:
    """
    A fitted transform, carrying the rows it was fitted on.

    `fitted_rows` and `fitted_through` exist so a stored model can be
    audited later: a scaler whose `fitted_through` is after a
    validation fold's start was fitted on the future, and that is
    checkable rather than merely promised.
    """
    means: Dict[str, float] = field(default_factory=dict)
    deviations: Dict[str, float] = field(default_factory=dict)
    fitted_rows: int = 0
    fitted_through: Optional[str] = None

    def transform(self, features: Dict[str, Optional[float]]
                  ) -> Dict[str, Optional[float]]:
        out: Dict[str, Optional[float]] = {}
        for name, value in features.items():
            deviation = self.deviations.get(name)
            if value is None or not deviation:
                out[name] = None if value is None else 0.0
                continue
            out[name] = (value - self.means.get(name, 0.0)) / deviation
        return out

    def as_dict(self) -> Dict[str, Any]:
        return {"means": dict(self.means), "deviations": dict(self.deviations),
                "fitted_rows": self.fitted_rows,
                "fitted_through": self.fitted_through}


def fit_scaler(training_rows: Sequence[Any],
               feature_ids: Sequence[str]) -> Scaler:
    """
    Fit on training rows ONLY.

    The function has no access to anything else: a caller who wants to
    leak has to do it deliberately by passing validation rows in, which
    a reviewer can see in one line.
    """
    means: Dict[str, float] = {}
    deviations: Dict[str, float] = {}
    for name in feature_ids:
        values = [r.features.get(name) for r in training_rows]
        present = [v for v in values if v is not None]
        if len(present) < 2:
            continue
        means[name] = statistics.fmean(present)
        deviations[name] = statistics.pstdev(present)
    through = max((r.cutoff for r in training_rows), default=None)
    return Scaler(means=means, deviations=deviations,
                  fitted_rows=len(training_rows),
                  fitted_through=through.isoformat() if through else None)


def cross_sectional_zscore(values: Dict[str, Optional[float]]
                           ) -> Dict[str, Optional[float]]:
    """
    Z-score across instruments observed at ONE cutoff (§37).

    The input is what was observable at that minute and nothing else,
    so universe look-ahead is structurally impossible: an instrument
    with no closed bar simply is not a key here.
    """
    present = [v for v in values.values() if v is not None]
    if len(present) < 3:
        return {k: None for k in values}
    mean = statistics.fmean(present)
    deviation = statistics.pstdev(present)
    if not deviation:
        return {k: (0.0 if v is not None else None) for k, v in values.items()}
    return {k: ((v - mean) / deviation if v is not None else None)
            for k, v in values.items()}


def winsorized(value: Optional[float], history: Sequence[Optional[float]],
               limit: float = 3.0) -> Tuple[Optional[float], bool]:
    """
    `(clipped, was_clipped)` — the raw value is never destroyed (§38).

    Returns the flag so a caller can count how many rows a robust
    transform actually touched, which is the number that decides
    whether the transform is a detail or the whole result.
    """
    present = [v for v in history if v is not None]
    if value is None or len(present) < 3:
        return value, False
    mean = statistics.fmean(present)
    deviation = statistics.pstdev(present)
    if not deviation:
        return value, False
    ceiling, floor = mean + limit * deviation, mean - limit * deviation
    if value > ceiling:
        return ceiling, True
    if value < floor:
        return floor, True
    return value, False
