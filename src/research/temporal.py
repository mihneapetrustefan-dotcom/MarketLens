"""
src/research/temporal.py
-----------------------------
Safe temporal joins (Phase 7, spec §25, §27, §28).

THE FAILURE THIS PREVENTS: the most common leakage bug in financial
research is not exotic. It is joining a table on entity id and taking
the CURRENT value — today's sector classification, today's index
membership, today's revised GDP figure — for an observation dated
2019. The join looks correct, the code reads fine, and the resulting
model quietly knows the future.

THE RULE ENFORCED HERE: a temporal join returns the LATEST value whose
own timestamp is at or before the cutoff. Never the current value.
Never the nearest value. If no value existed by the cutoff, the join
returns None — an honest gap, which downstream code marks as a data
quality issue rather than silently filling.

POINT-IN-TIME LIMITATION, STATED PLAINLY (spec §25): this module can
only be as point-in-time as the data given to it. If a source stores
only its latest revision (as FRED observations currently do in this
project — see fred_connector.py, which fetches the newest value of
each series), then no join can recover what the value looked like
before revision. `assert_point_in_time_capable()` exists so a caller
can require that guarantee explicitly and fail loudly when the data
cannot provide it, instead of assuming it silently.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, TypeVar

logger = logging.getLogger("marketlens.research.temporal")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

T = TypeVar("T")


class TemporalJoinError(Exception):
    """Raised when a join is asked for a point-in-time guarantee the data cannot support."""


def _require_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (got a naive datetime)")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be in UTC (got offset {value.utcoffset()})")
    return value


def latest_known_at(
    records: Sequence[T],
    cutoff: datetime,
    timestamp_getter: Callable[[T], Optional[datetime]],
) -> Optional[T]:
    """
    The most recent record whose timestamp is at or before `cutoff`.

    This is THE temporal join primitive. Records with no timestamp are
    excluded — an undated record cannot be proven to have existed by
    the cutoff, and including it would be the exact silent leak this
    module exists to prevent.
    """
    _require_utc(cutoff, "cutoff")
    eligible = []
    for record in records:
        moment = timestamp_getter(record)
        if moment is None:
            continue
        _require_utc(moment, "record timestamp")
        if moment <= cutoff:
            eligible.append((moment, record))
    if not eligible:
        return None
    return max(eligible, key=lambda pair: pair[0])[1]


def all_known_at(
    records: Sequence[T],
    cutoff: datetime,
    timestamp_getter: Callable[[T], Optional[datetime]],
) -> List[T]:
    """Every record knowable at the cutoff, chronologically ordered."""
    _require_utc(cutoff, "cutoff")
    eligible = []
    for record in records:
        moment = timestamp_getter(record)
        if moment is not None and moment <= cutoff:
            eligible.append((moment, record))
    return [record for _, record in sorted(eligible, key=lambda pair: pair[0])]


def strictly_after(
    records: Sequence[T],
    cutoff: datetime,
    timestamp_getter: Callable[[T], Optional[datetime]],
    until: Optional[datetime] = None,
) -> List[T]:
    """
    Records strictly AFTER the cutoff — the outcome side.

    Deliberately named so its use is visible in review: a call to
    `strictly_after` inside feature construction is an obvious bug,
    whereas a generic `get_data()` would hide the same mistake.
    """
    _require_utc(cutoff, "cutoff")
    if until is not None:
        _require_utc(until, "until")
    result = []
    for record in records:
        moment = timestamp_getter(record)
        if moment is None or moment <= cutoff:
            continue
        if until is not None and moment > until:
            continue
        result.append(record)
    return sorted(result, key=timestamp_getter)


def assert_point_in_time_capable(
    records: Sequence[T],
    timestamp_getter: Callable[[T], Optional[datetime]],
    source_name: str = "source",
    minimum_distinct_timestamps: int = 2,
) -> None:
    """
    Verify a source can actually support point-in-time joins.

    A source holding only ONE timestamp per entity (i.e. just its
    latest revision) cannot answer "what did this look like in 2019".
    Calling this makes that limitation an explicit, loud failure
    instead of a silent wrong answer.

    Raises:
        TemporalJoinError: the data has too few distinct timestamps to
            represent revision history.
    """
    timestamps = {timestamp_getter(r) for r in records if timestamp_getter(r) is not None}
    if len(timestamps) < minimum_distinct_timestamps:
        raise TemporalJoinError(
            f"'{source_name}' has only {len(timestamps)} distinct timestamp(s) — it stores current "
            f"values, not revision history, so it cannot support a point-in-time join. "
            f"Either supply historical revisions or mark this feature as a documented limitation."
        )


class TemporalIndex:
    """
    An indexed, reusable temporal lookup for one keyed source.

    Built once and queried many times, so a dataset build over
    thousands of observations does not re-scan the same series per
    observation (spec §46). Records are pre-sorted per key; each
    lookup is then a bounded scan of that key's own series.
    """

    def __init__(self, timestamp_getter: Callable[[T], Optional[datetime]],
                 key_getter: Callable[[T], str], name: str = "index"):
        self.name = name
        self._timestamp_getter = timestamp_getter
        self._key_getter = key_getter
        self._by_key: Dict[str, List[T]] = {}

    def add_all(self, records: Sequence[T]) -> None:
        for record in records:
            if self._timestamp_getter(record) is None:
                continue   # undated records are never indexed
            self._by_key.setdefault(self._key_getter(record), []).append(record)
        for key in self._by_key:
            self._by_key[key].sort(key=self._timestamp_getter)

    def latest_at(self, key: str, cutoff: datetime) -> Optional[T]:
        """The latest record for `key` at or before `cutoff`, or None."""
        series = self._by_key.get(key)
        if not series:
            return None
        return latest_known_at(series, cutoff, self._timestamp_getter)

    def series_at(self, key: str, cutoff: datetime) -> List[T]:
        """The full history for `key` up to `cutoff`."""
        series = self._by_key.get(key)
        if not series:
            return []
        return all_known_at(series, cutoff, self._timestamp_getter)

    def keys(self) -> List[str]:
        return list(self._by_key.keys())

    def size(self) -> int:
        return sum(len(series) for series in self._by_key.values())
