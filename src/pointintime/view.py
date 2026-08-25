"""
src/pointintime/view.py
----------------------------
Structural protection against look-ahead bias.

WHY THIS EXISTS, AND WHY IT COMES BEFORE PHASE 6:
the Phase 0 audit identified one gap that invalidates quantitative
research rather than merely degrading it — the system cannot say "what
did we know on date D, using only what was available by date D".
Every scoring engine reads the full current history. For a dashboard
that is harmless. For an event study it is fatal: the results look
rigorous and are wrong, which is worse than obviously-broken results
because nobody catches it.

THE DESIGN PRINCIPLE — A BARRIER, NOT A CONVENTION:
discipline ("remember to filter by date") fails silently the first
time someone forgets. So this module makes the failure IMPOSSIBLE
instead of unlikely: a PointInTimeView is anchored to a moment T, and
any attempt to read data timestamped after T raises
LookAheadViolation. Code that leaks future data does not produce
subtly wrong numbers — it crashes, loudly, in tests.

THE ONE DISTINCTION EVERYTHING RESTS ON:

    INFORMATION SET AT T   what was knowable at T  -> may drive a decision
    OUTCOME AFTER T        what happened later     -> may ONLY be used
                                                       to evaluate that
                                                       decision

Both are legitimate. Mixing them is not. This module keeps them in
separate, differently-named accessors so mixing them requires
deliberately calling an outcome method — never an accident.

WHAT THIS MODULE DELIBERATELY DOES NOT DO: it does not fetch data, own
a database, or know about market data providers. It is a filtering
lens over records the caller already holds, so it can wrap ANY source
(prices, articles, events, recommendations) without coupling.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Iterable, List, Optional, TypeVar, Generic, Dict

logger = logging.getLogger("marketlens.pointintime")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

T = TypeVar("T")


class LookAheadViolation(Exception):
    """
    Raised when code anchored at time T tries to read data timestamped
    after T.

    This is deliberately an exception rather than a warning or a
    silently-filtered result: a look-ahead attempt is a BUG in the
    calling research code, and a bug that returns plausible numbers is
    far more dangerous than one that stops the run.
    """


def _require_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (got a naive datetime)")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be in UTC (got offset {value.utcoffset()})")
    return value


@dataclass(frozen=True)
class TimeUncertainty:
    """
    An imprecisely-known moment, expressed as a RANGE rather than a
    false point.

    This exists because the timestamp that matters most for an event
    study — when information actually became public — is frequently
    unknown to the minute. An announcement may hit a wire at 09:00 and
    a website at 09:12; which one moved the market is often
    unknowable. Recording a single confident timestamp there would be
    fabricating precision.

    CONSERVATIVE BY CONSTRUCTION: `latest` is what an information-set
    query uses (assume we knew as late as possible, so we never claim
    knowledge we might not have had), while `earliest` is what an
    outcome-measurement window uses (start measuring as early as the
    event might have been visible). The asymmetry is the point.
    """
    earliest: datetime
    latest: datetime
    basis: str = ""     # how this range was derived, e.g. "wire timestamp vs first article"

    def __post_init__(self):
        _require_utc(self.earliest, "earliest")
        _require_utc(self.latest, "latest")
        if self.earliest > self.latest:
            raise ValueError("earliest must not be after latest")

    @property
    def is_precise(self) -> bool:
        return self.earliest == self.latest

    @property
    def uncertainty_seconds(self) -> float:
        return (self.latest - self.earliest).total_seconds()

    @property
    def midpoint(self) -> datetime:
        return self.earliest + (self.latest - self.earliest) / 2

    @classmethod
    def precise(cls, moment: datetime, basis: str = "exact timestamp") -> "TimeUncertainty":
        return cls(earliest=moment, latest=moment, basis=basis)

    @classmethod
    def between(cls, earliest: datetime, latest: datetime, basis: str = "") -> "TimeUncertainty":
        return cls(earliest=earliest, latest=latest, basis=basis)


class PointInTimeView(Generic[T]):
    """
    A read-only lens over timestamped records, anchored at one moment.

    Any record timestamped after the anchor is invisible to the
    information-set accessors, and asking for one explicitly raises
    LookAheadViolation.
    """

    def __init__(
        self,
        as_of: datetime,
        records: Iterable[T],
        timestamp_getter: Callable[[T], Optional[datetime]],
        label: str = "",
    ):
        """
        Args:
            as_of: the anchor. Nothing after this is knowable.
            records: the full record set (past AND future relative to
                the anchor). Holding both is intentional: the view
                needs future records in order to serve OUTCOME queries,
                which are legitimate — it just never lets them reach an
                information-set query.
            timestamp_getter: extracts the relevant timestamp from a
                record. The CALLER chooses which timestamp is
                authoritative (publication vs event vs ingestion),
                because that choice is domain-specific and must be
                explicit rather than guessed here.
            label: name used in error messages, for debuggability.
        """
        self.as_of = _require_utc(as_of, "as_of")
        self._records = list(records)
        self._timestamp_getter = timestamp_getter
        self.label = label or "records"

    # ---------------- information set (safe) ----------------

    def known(self) -> List[T]:
        """
        Every record knowable at the anchor: timestamp present AND at
        or before `as_of`.

        Records with a MISSING timestamp are EXCLUDED. That is
        deliberate and conservative — an undated record cannot be
        proven to have been available, and including it would be
        exactly the silent leak this class exists to prevent.
        """
        result = []
        for record in self._records:
            moment = self._timestamp_getter(record)
            if moment is None:
                continue
            if moment <= self.as_of:
                result.append(record)
        return result

    def known_within(self, lookback: timedelta) -> List[T]:
        """Records knowable at the anchor and no older than `lookback` before it."""
        cutoff = self.as_of - lookback
        return [r for r in self.known() if self._timestamp_getter(r) >= cutoff]

    def most_recent(self) -> Optional[T]:
        """The latest record knowable at the anchor, or None."""
        candidates = self.known()
        if not candidates:
            return None
        return max(candidates, key=self._timestamp_getter)

    def count_known(self) -> int:
        return len(self.known())

    def excluded_count(self) -> int:
        """How many records the anchor hides — useful for diagnostics and for proving the barrier is doing work."""
        return len(self._records) - len(self.known())

    # ---------------- explicit guard ----------------

    def assert_knowable(self, moment: Optional[datetime], what: str = "value") -> None:
        """
        Raise LookAheadViolation if `moment` is after the anchor.

        Call this at any boundary where an external timestamp enters a
        point-in-time calculation. It converts a silent correctness bug
        into an immediate, located failure.
        """
        if moment is None:
            return
        _require_utc(moment, "moment")
        if moment > self.as_of:
            raise LookAheadViolation(
                f"look-ahead in {self.label}: {what} is timestamped {moment.isoformat()}, "
                f"after the point-in-time anchor {self.as_of.isoformat()}. "
                f"Information after the anchor may only be read via outcome accessors."
            )

    def get_knowable(self, record: T) -> T:
        """Return the record, or raise if it postdates the anchor. The strict single-record accessor."""
        self.assert_knowable(self._timestamp_getter(record), "record")
        return record

    # ---------------- outcomes (explicitly after the anchor) ----------------

    def outcome_after(self, horizon: Optional[timedelta] = None) -> List[T]:
        """
        Records occurring AFTER the anchor — what actually happened.

        Deliberately named so it can never be mistaken for an
        information-set query. Legitimate for measuring what followed a
        decision; illegitimate as an input to that decision. The naming
        is the safeguard: `known()` vs `outcome_after()` reads
        differently in review, so a misuse is visible.
        """
        end = self.as_of + horizon if horizon else None
        result = []
        for record in self._records:
            moment = self._timestamp_getter(record)
            if moment is None or moment <= self.as_of:
                continue
            if end is None or moment <= end:
                result.append(record)
        return result

    def outcome_at_or_after(self, moment: datetime) -> List[T]:
        """Records at or after a specific later moment — for measuring a window that starts some time after the anchor."""
        _require_utc(moment, "moment")
        return [r for r in self._records
                if (ts := self._timestamp_getter(r)) is not None and ts >= moment]

    # ---------------- derived views ----------------

    def rewind_to(self, earlier: datetime) -> "PointInTimeView[T]":
        """
        A new view anchored earlier.

        Moving the anchor FORWARD is refused: it would quietly widen an
        information set that a caller had already constrained, which is
        the very mistake this class prevents. Build a fresh view
        instead, so the wider anchor is an explicit, visible choice.
        """
        _require_utc(earlier, "earlier")
        if earlier > self.as_of:
            raise LookAheadViolation(
                f"cannot move a point-in-time anchor forward "
                f"({self.as_of.isoformat()} -> {earlier.isoformat()}); construct a new view explicitly."
            )
        return PointInTimeView(earlier, self._records, self._timestamp_getter, self.label)

    def describe(self) -> Dict[str, Any]:
        """Diagnostics: what this view can and cannot see."""
        return {
            "label": self.label,
            "as_of": self.as_of.isoformat(),
            "total_records": len(self._records),
            "known_at_anchor": self.count_known(),
            "hidden_by_anchor": self.excluded_count(),
        }


def build_view(
    as_of: datetime,
    records: Iterable[T],
    timestamp_getter: Callable[[T], Optional[datetime]],
    label: str = "",
) -> PointInTimeView[T]:
    """Convenience constructor, so callers read as `build_view(t, prices, lambda p: p.observed_at)`."""
    return PointInTimeView(as_of, records, timestamp_getter, label)


def market_visibility_time(
    event_time: Optional[datetime],
    publication_time: Optional[datetime],
    ingestion_time: Optional[datetime] = None,
) -> Optional[TimeUncertainty]:
    """
    Derive WHEN THE MARKET COULD HAVE KNOWN an event — the timestamp
    that actually matters for an event study, and the one the system
    does not currently record.

    It is neither event_time (when it happened — possibly private) nor
    publication_time (when one outlet wrote about it). It is the range
    in which the information plausibly became public, and it is
    returned as a RANGE precisely because collapsing it to a point
    would fabricate precision the data does not support:

      both known, event first  -> [event_time, publication_time]
                                   (public somewhere in between)
      publication only         -> precise at publication_time
      event only               -> [event_time, ingestion_time] if we
                                   have an ingestion time, else precise
      neither                  -> None; the caller must handle an
                                   unusable event rather than receive
                                   an invented timestamp
    """
    if publication_time and event_time:
        if event_time <= publication_time:
            return TimeUncertainty.between(
                event_time, publication_time,
                basis="between reported event time and first publication")
        # Publication BEFORE the stated event time means one of the two
        # is wrong. Refusing to resolve it is correct: silently picking
        # one would bury a data-quality problem inside a study.
        return TimeUncertainty.between(
            publication_time, event_time,
            basis="INCONSISTENT: publication precedes stated event time — treat as low quality")

    if publication_time:
        return TimeUncertainty.precise(publication_time, basis="publication time only")

    if event_time:
        if ingestion_time and ingestion_time >= event_time:
            return TimeUncertainty.between(
                event_time, ingestion_time,
                basis="between event time and our ingestion; true publication unknown")
        return TimeUncertainty.precise(event_time, basis="event time only; publication unknown")

    return None
