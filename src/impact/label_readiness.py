"""
src/impact/label_readiness.py
-------------------------------------------
Mechanical readiness of anchor-v2 labels (Phase 25.9C).

READINESS IS NOT A RESULT
-----------------------------
This module answers "could the label be computed", never "what is it".
It reads timestamps, the existence of candles, the existence of label
rows, and cache request metadata. It computes no return, no
correlation, no spread. `dataset_identity` hashes label rows so a
changed dataset cannot reuse an identity, and a hash reveals nothing
about direction or size.

THE DISTINCTION THIS EXISTS FOR
-----------------------------------
    NOT_YET_OBSERVABLE     the required session has not happened yet.
                           Healthy. Wait.
    STALE_CACHE            the session has happened, but the price
                           cache was not refreshed past it. Run the
                           refresh.
    EXPECTED_DATA_MISSING  the session happened AND the cache was
                           refreshed past it, and there is still no
                           price. A genuine data problem.

Collapsing these is how "waiting for the future" gets reported as
"broken", or -- worse -- how a broken refresh gets mistaken for
patience.

NO SECOND CALENDAR
----------------------
`src/backtest/calendar.py` deliberately has no holiday table: the
project "cannot distinguish a market holiday from a fetch gap". This
module follows it. Known sessions come from cached daily candles.
Beyond them, sessions are PROJECTED -- every calendar day for crypto,
weekdays otherwise -- and a projection is only ever used as a LOWER
BOUND on when something can resolve. A holiday can only push the true
date later. Actual readiness never rests on a projection: it requires
the real candle.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

READY = "READY"
NOT_YET_OBSERVABLE = "NOT_YET_OBSERVABLE"
STALE_CACHE = "STALE_CACHE"
LABEL_NOT_BUILT = "LABEL_NOT_BUILT"
EXPECTED_DATA_MISSING = "EXPECTED_DATA_MISSING"
MISSING_BENCHMARK_PRICE = "MISSING_BENCHMARK_PRICE"
BENCHMARK_CALENDAR_MISMATCH = "BENCHMARK_CALENDAR_MISMATCH"
PRICE_VINTAGE_BREAK = "PRICE_VINTAGE_BREAK"
MISSING_MAPPING = "MISSING_MAPPING"
FEATURE_MISSING = "FEATURE_MISSING"
INVALID_ANCHOR = "INVALID_ANCHOR"

#: States that will change on their own or after routine work.
PENDING_STATES = (NOT_YET_OBSERVABLE, STALE_CACHE, LABEL_NOT_BUILT)
#: Data-quality exclusions: counted against a pre-set cap.
QUALITY_EXCLUSIONS = (EXPECTED_DATA_MISSING, MISSING_BENCHMARK_PRICE,
                      PRICE_VINTAGE_BREAK, MISSING_MAPPING, FEATURE_MISSING,
                      INVALID_ANCHOR)
#: Permanent by construction, not a data fault: a crypto window ending
#: on a weekend has no SPY close to adjust against.
STRUCTURAL_EXCLUSIONS = (BENCHMARK_CALENDAR_MISMATCH,)

#: A daily close for session D is only fetchable after D ends.
CLOSE_AVAILABILITY_LAG = timedelta(days=1)

METHOD_VERSION = "anchor-v2"
WINDOWS = {"d5": 5, "d20": 20}
FEATURE = "market.return_60d"


@dataclass
class ObservationReadiness:
    observation_id: str
    instrument_id: str
    anchor: Optional[datetime]
    state: str
    reasons: List[str] = field(default_factory=list)
    required_end: Dict[str, Optional[str]] = field(default_factory=dict)
    projected: bool = False
    date: str = ""


def _parse(text) -> Optional[datetime]:
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _is_crypto(conn, instrument_id: str) -> bool:
    try:
        row = conn.execute("SELECT asset_class FROM instruments WHERE instrument_id = ?",
                           (instrument_id,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row and row[0]:
        return str(row[0]).lower() == "crypto"
    return instrument_id.startswith("crypto-")


class CacheIndex:
    """Session dates and observable coverage per instrument, loaded once."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._sessions: Dict[str, List[datetime]] = {}
        self._observable_end: Dict[str, Optional[datetime]] = {}
        self._breaks: Dict[str, List[datetime]] = {}

    def sessions(self, instrument_id: str) -> List[datetime]:
        if instrument_id not in self._sessions:
            self._sessions[instrument_id] = [
                _parse(ts) for (ts,) in self.conn.execute("""
                    SELECT timestamp FROM price_candle_cache
                    WHERE instrument_id = ? AND interval = '1d' AND close IS NOT NULL
                    ORDER BY timestamp
                """, (instrument_id,))]
        return self._sessions[instrument_id]

    def observable_end(self, instrument_id: str) -> Optional[datetime]:
        """How far the cache was genuinely refreshed: min(range_end, requested_at)."""
        if instrument_id not in self._observable_end:
            try:
                row = self.conn.execute("""
                    SELECT MAX(MIN(range_end, requested_at)) FROM price_cache_requests
                    WHERE instrument_id = ? AND interval = '1d'
                """, (instrument_id,)).fetchone()
            except sqlite3.OperationalError:
                row = None
            self._observable_end[instrument_id] = _parse(row[0]) if row else None
        return self._observable_end[instrument_id]

    def vintage_breaks(self, instrument_id: str) -> List[datetime]:
        if instrument_id not in self._breaks:
            try:
                rows = self.conn.execute("""
                    SELECT overlap_start FROM price_cache_vintage_checks
                    WHERE instrument_id = ? AND interval = '1d' AND consistent = 0
                """, (instrument_id,)).fetchall()
            except sqlite3.OperationalError:
                rows = []
            self._breaks[instrument_id] = [_parse(r[0]) for r in rows]
        return self._breaks[instrument_id]

    def has_close_on(self, instrument_id: str, day: date) -> bool:
        return any(s.date() == day for s in self.sessions(instrument_id))


def project_session(known_after_anchor: Sequence[datetime], anchor: datetime,
                    index: int, crypto: bool) -> date:
    """
    The date of the index-th session at or after the anchor, projecting
    past the last known session. A LOWER BOUND whenever projection is
    used: holidays can only make the true date later.
    """
    if index < len(known_after_anchor):
        return known_after_anchor[index].date()
    cursor = (known_after_anchor[-1].date() if known_after_anchor
              else anchor.date() - timedelta(days=1))
    needed = index - len(known_after_anchor) + 1
    while needed:
        cursor += timedelta(days=1)
        if crypto or cursor.weekday() < 5:
            needed -= 1
    return cursor


def classify(conn: sqlite3.Connection, index: CacheIndex, observation_id: str,
             instrument_id: str, benchmark_id: Optional[str], anchor_text,
             now: datetime, labels_present: Dict[str, bool],
             feature_present: bool) -> ObservationReadiness:
    anchor = _parse(anchor_text)
    result = ObservationReadiness(observation_id, instrument_id, anchor, READY)
    if anchor is None:
        result.state, result.reasons = INVALID_ANCHOR, ["no parseable market-visibility anchor"]
        return result
    sessions = index.sessions(instrument_id)
    if not sessions:
        result.state, result.reasons = MISSING_MAPPING, ["no daily price series for the instrument"]
        return result
    if not any(s.date() < anchor.date() for s in sessions):
        result.state, result.reasons = INVALID_ANCHOR, ["no daily close before the event date (anchor-v2 base)"]
        return result
    if not feature_present:
        result.state, result.reasons = FEATURE_MISSING, [f"{FEATURE} is absent"]
        return result

    crypto = _is_crypto(conn, instrument_id)
    future = [s for s in sessions if s >= anchor]
    states = []
    for name, offset in WINDOWS.items():
        end_day = project_session(future, anchor, offset, crypto)
        projected = offset >= len(future)
        result.required_end[name] = end_day.isoformat()
        result.projected = result.projected or projected
        close_fetchable = datetime.combine(end_day, datetime.min.time(),
                                           tzinfo=timezone.utc) + CLOSE_AVAILABILITY_LAG
        if projected:
            if close_fetchable > now:
                states.append((NOT_YET_OBSERVABLE, f"{name}: session {end_day} not yet closed"))
                continue
            refreshed = index.observable_end(instrument_id)
            # A refresh DURING session D cannot contain D's close. Measured:
            # 226 of 305 US-equity caches end exactly one day before their
            # request date. D is only observable if refreshed strictly after.
            if refreshed is None or refreshed.date() <= end_day:
                states.append((STALE_CACHE, f"{name}: {end_day} has passed but the cache "
                                            f"was refreshed only to {refreshed}"))
            else:
                states.append((EXPECTED_DATA_MISSING, f"{name}: cache refreshed past {end_day} "
                                                      f"and no close exists"))
            continue

        if any(b is not None and b.date() <= end_day
               for b in index.vintage_breaks(instrument_id)):
            states.append((PRICE_VINTAGE_BREAK, f"{name}: instrument adjustment vintage changed "
                                                f"inside the window"))
            continue
        if benchmark_id:
            if not index.has_close_on(benchmark_id, end_day):
                if end_day.weekday() >= 5:
                    states.append((BENCHMARK_CALENDAR_MISMATCH,
                                   f"{name}: window ends on {end_day}, a day the benchmark does not trade"))
                    continue
                refreshed = index.observable_end(benchmark_id)
                if close_fetchable > now:
                    states.append((NOT_YET_OBSERVABLE, f"{name}: benchmark session {end_day} not yet closed"))
                elif refreshed is None or refreshed.date() <= end_day:
                    states.append((STALE_CACHE, f"{name}: benchmark cache refreshed only to {refreshed}"))
                else:
                    states.append((MISSING_BENCHMARK_PRICE, f"{name}: no benchmark close on {end_day}"))
                continue
            if any(b is not None and b.date() <= end_day
                   for b in index.vintage_breaks(benchmark_id)):
                states.append((PRICE_VINTAGE_BREAK, f"{name}: benchmark adjustment vintage changed"))
                continue
        if not labels_present.get(name):
            states.append((LABEL_NOT_BUILT, f"{name}: prices available but no {METHOD_VERSION} label row"))
            continue
        states.append((READY, ""))

    order = [EXPECTED_DATA_MISSING, MISSING_BENCHMARK_PRICE, PRICE_VINTAGE_BREAK,
             BENCHMARK_CALENDAR_MISMATCH, STALE_CACHE, NOT_YET_OBSERVABLE,
             LABEL_NOT_BUILT, READY]
    result.state = min((s for s, _r in states), key=order.index)
    result.reasons = [r for s, r in states if r]
    return result


def assess(conn: sqlite3.Connection, start: str, end: str,
           now: Optional[datetime] = None) -> List[ObservationReadiness]:
    """Readiness of every observation whose information cutoff is in [start, end]."""
    now = now or datetime.now(timezone.utc)
    index = CacheIndex(conn)
    rows = conn.execute("""
        SELECT o.observation_id, o.instrument_id, s.benchmark_id,
               s.market_visibility_latest, o.information_cutoff
        FROM research_observations o
        LEFT JOIN event_studies s
          ON s.event_id = o.event_id AND s.instrument_id = o.instrument_id
        WHERE o.information_cutoff >= ? AND o.information_cutoff <= ?
          AND o.quality_level != 'invalid'
        ORDER BY o.information_cutoff, o.observation_id
    """, (start, end)).fetchall()
    ids = [r[0] for r in rows]
    present_labels = defaultdict(set)
    present_features = set()
    if ids:
        marks = ",".join("?" * len(ids))
        names = [f"{w}.abnormal_return.{METHOD_VERSION}" for w in WINDOWS]
        for oid, name in conn.execute(f"""
            SELECT observation_id, name FROM research_labels
            WHERE observation_id IN ({marks}) AND name IN ({",".join("?" * len(names))})
              AND label_version = 'v2' AND calculation = ?
              AND value_json IS NOT NULL AND value_json != 'null'
        """, (*ids, *names, METHOD_VERSION)):
            present_labels[oid].add(name.split(".")[0])
        for (oid,) in conn.execute(f"""
            SELECT observation_id FROM research_features
            WHERE observation_id IN ({marks}) AND qualified_name = ?
              AND value_json IS NOT NULL AND value_json != 'null'
        """, (*ids, FEATURE)):
            present_features.add(oid)

    out = []
    for oid, instrument_id, benchmark_id, anchor_text, cutoff in rows:
        item = classify(conn, index, oid, instrument_id, benchmark_id, anchor_text, now,
                        {w: w in present_labels[oid] for w in WINDOWS},
                        oid in present_features)
        item.date = (cutoff or "")[:10]
        out.append(item)
    return out


def earliest_theoretical_date(items: Sequence[ObservationReadiness]) -> Optional[date]:
    """
    The earliest date on which every required d20 close could exist.

    Lower bound: projected sessions ignore holidays, which can only push
    it later. The close of the last required session must also have
    happened, hence the one-day lag.
    """
    ends = [date.fromisoformat(i.required_end["d20"]) for i in items
            if i.required_end.get("d20")]
    return (max(ends) + CLOSE_AVAILABILITY_LAG) if ends else None


def dataset_identity(conn: sqlite3.Connection, items: Sequence[ObservationReadiness]) -> str:
    """
    Deterministic identity of everything the protected test would read.

    Hashes the observations, the frozen feature, the anchor-v2 labels,
    and each instrument's price-cache horizon. Same inputs, same
    identity; any change, a different one. A hash discloses no
    statistic.
    """
    digest = hashlib.sha256()
    digest.update(METHOD_VERSION.encode())
    for item in sorted(items, key=lambda i: i.observation_id):
        digest.update(f"|{item.observation_id}|{item.instrument_id}|{item.anchor}".encode())
        for (value,) in conn.execute(
                "SELECT value_json FROM research_features WHERE observation_id=? AND qualified_name=?",
                (item.observation_id, FEATURE)):
            digest.update(f"|f:{value}".encode())
        for name, value, version, calc in conn.execute("""
                SELECT name, value_json, label_version, calculation FROM research_labels
                WHERE observation_id = ? AND calculation = ? ORDER BY name
                """, (item.observation_id, METHOD_VERSION)):
            digest.update(f"|l:{name}:{value}:{version}:{calc}".encode())
        horizon = conn.execute(
            "SELECT MAX(timestamp) FROM price_candle_cache WHERE instrument_id=? AND interval='1d'",
            (item.instrument_id,)).fetchone()[0]
        digest.update(f"|p:{horizon}".encode())
    return digest.hexdigest()[:32]


def summarize(items: Sequence[ObservationReadiness]) -> Dict[str, object]:
    counts = Counter(i.state for i in items)
    ready = [i for i in items if i.state == READY]
    per_date = Counter(i.date for i in ready)
    return {
        "total": len(items),
        "states": dict(sorted(counts.items())),
        "pending": sum(counts[s] for s in PENDING_STATES),
        "quality_exclusions": sum(counts[s] for s in QUALITY_EXCLUSIONS),
        "structural_exclusions": sum(counts[s] for s in STRUCTURAL_EXCLUSIONS),
        "resolvable": len(ready),
        "resolvable_dates_with_10": sum(1 for n in per_date.values() if n >= 10),
        "projected_ends": sum(1 for i in items if i.projected),
    }
