"""
src/adaptive/universe.py
-------------------------------------
The parent universe, and as-of-safe access to its history (25.9I-B).

WHAT THIS MODULE GUARANTEES
-------------------------------
Two things, and it exists because both are easy to get wrong quietly.

1. THE ARTIFACT IS WHAT IT CLAIMS TO BE. `ParentUniverse.load` recomputes
   the members' sha256 and refuses a file whose contents no longer match
   the hash written into it. A universe that drifted from its recorded
   version would make every downstream decision unreproducible while
   still looking fine.

2. NOTHING READS PAST `as_of`. `MarketHistory` bounds every query by the
   decision timestamp. Not "usually", not "when the caller remembers":
   the connection is opened read-only and every statement carries the
   bound, because a selector that can see today's close can justify
   anything about today's open.

WHAT IT IS NOT
------------------
Not a watchlist and not a portfolio. Membership means an instrument is
ALLOWED to be evaluated. Most members will be absent from most sessions,
which is the point of having a parent universe at all.

BENCHMARKS ARE CARRIED, NEVER ADMITTED
------------------------------------------
`benchmark-spy` is absent from this project's instrument master, is
skipped by name in `capture/universe.py`, and is used by
`features/intraday.py` for market context "on the benchmark, never on
self". It stays in `benchmarks` so market-relative features keep their
input, and it can never consume a session slot.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.domain.adaptive_models import require_utc

#: Written by scripts/build_parent_universe.py.
DEFAULT_ARTIFACT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config", "parent_universe_v1.json")


class ParentUniverseError(ValueError):
    """The artifact cannot be trusted, so nothing may be selected from it."""


@dataclass(frozen=True)
class ParentUniverseMember:
    """One instrument the adaptive layer is permitted to evaluate."""
    instrument_id: str
    ticker: str
    sector_id: str
    asset_class: str = "stock"
    sec_type: str = "STK"
    currency: str = "USD"
    role: str = "candidate"
    daily_candles_at_as_of: Optional[int] = None
    median_dollar_volume: Optional[float] = None
    sector_rank: Optional[int] = None
    inclusion_reason: str = ""

    def __post_init__(self):
        if not self.instrument_id:
            raise ParentUniverseError("instrument_id is required")
        if not self.sector_id:
            raise ParentUniverseError(
                f"{self.instrument_id} has no sector metadata")

    @property
    def is_benchmark(self) -> bool:
        return self.role == "benchmark"


@dataclass(frozen=True)
class ParentUniverse:
    """A versioned, hash-verified membership with its provenance."""
    version: str
    as_of: datetime
    rule: str
    members: Tuple[ParentUniverseMember, ...]
    benchmarks: Tuple[ParentUniverseMember, ...] = ()
    members_sha256: str = ""
    built_at: Optional[datetime] = None
    source: str = ""
    excluded: Tuple[Dict[str, str], ...] = ()
    newest_candle_at_as_of: Optional[str] = None

    def __post_init__(self):
        object.__setattr__(self, "as_of", require_utc(self.as_of, "as_of"))
        object.__setattr__(self, "built_at", require_utc(self.built_at, "built_at"))
        ids = [m.instrument_id for m in self.members]
        if len(set(ids)) != len(ids):
            raise ParentUniverseError("parent universe members must be unique")
        for benchmark in self.benchmarks:
            if benchmark.instrument_id in set(ids):
                raise ParentUniverseError(
                    f"{benchmark.instrument_id} is both a candidate and a "
                    f"benchmark; a benchmark never competes for a slot")

    # ---------------- loading ----------------

    @classmethod
    def load(cls, path: str = DEFAULT_ARTIFACT) -> "ParentUniverse":
        if not os.path.exists(path):
            raise ParentUniverseError(f"parent universe artifact not found: {path}")
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "ParentUniverse":
        members = tuple(_member(entry) for entry in raw.get("members", ()))
        recorded = raw.get("members_sha256") or ""
        if recorded:
            computed = hashlib.sha256(json.dumps(
                raw["members"], sort_keys=True).encode("utf-8")).hexdigest()
            if computed != recorded:
                raise ParentUniverseError(
                    f"parent universe {raw.get('version')!r} does not match its "
                    f"recorded sha256 ({computed[:16]} != {recorded[:16]}): the "
                    f"artifact was edited after it was written")
        return cls(
            version=raw.get("version") or "",
            as_of=_parse(raw.get("as_of")),
            rule=raw.get("rule") or "",
            members=members,
            benchmarks=tuple(_member(entry, role="benchmark")
                             for entry in raw.get("benchmarks", ())),
            members_sha256=recorded,
            built_at=_parse(raw.get("built_at"), required=False),
            source=raw.get("source") or "",
            excluded=tuple(raw.get("excluded", ())),
            newest_candle_at_as_of=raw.get("newest_candle_at_as_of"))

    # ---------------- reading ----------------

    def member(self, instrument_id: str) -> Optional[ParentUniverseMember]:
        for candidate in self.members:
            if candidate.instrument_id == instrument_id:
                return candidate
        return None

    @property
    def instrument_ids(self) -> Tuple[str, ...]:
        return tuple(m.instrument_id for m in self.members)

    @property
    def sectors(self) -> Tuple[str, ...]:
        return tuple(sorted({m.sector_id for m in self.members}))

    def by_sector(self) -> Dict[str, Tuple[ParentUniverseMember, ...]]:
        grouped: Dict[str, List[ParentUniverseMember]] = {}
        for member in self.members:
            grouped.setdefault(member.sector_id, []).append(member)
        return {sector: tuple(members) for sector, members in sorted(grouped.items())}

    def describe(self) -> Dict[str, Any]:
        return {"version": self.version, "as_of": self.as_of.isoformat(),
                "members": len(self.members), "sectors": len(self.sectors),
                "benchmarks": [b.instrument_id for b in self.benchmarks],
                "members_sha256": self.members_sha256[:16],
                "excluded": len(self.excluded), "rule": self.rule}


def _member(entry: Dict[str, Any], role: Optional[str] = None
            ) -> ParentUniverseMember:
    return ParentUniverseMember(
        instrument_id=entry.get("instrument_id", ""),
        ticker=entry.get("ticker", ""),
        sector_id=entry.get("sector_id") or ("benchmark" if role else ""),
        asset_class=entry.get("asset_class", "stock"),
        sec_type=entry.get("sec_type", "STK"),
        currency=entry.get("currency", "USD"),
        role=role or entry.get("role", "candidate"),
        daily_candles_at_as_of=entry.get("daily_candles_at_as_of"),
        median_dollar_volume=entry.get("median_dollar_volume"),
        sector_rank=entry.get("sector_rank"),
        inclusion_reason=entry.get("inclusion_reason", ""))


def _parse(raw: Optional[str], required: bool = True) -> Optional[datetime]:
    if not raw:
        if required:
            raise ParentUniverseError("a timestamp is missing from the artifact")
        return None
    stamp = datetime.fromisoformat(raw)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


# ======================================================================
# As-of-safe market history
# ======================================================================


@dataclass(frozen=True)
class InstrumentHistory:
    """Daily candles at or before an `as_of`, newest last."""
    instrument_id: str
    as_of: datetime
    timestamps: Tuple[str, ...] = ()
    closes: Tuple[float, ...] = ()
    volumes: Tuple[float, ...] = ()

    @property
    def count(self) -> int:
        return len(self.closes)

    @property
    def newest_timestamp(self) -> Optional[str]:
        return self.timestamps[-1] if self.timestamps else None

    def data_age_days(self) -> Optional[float]:
        """How stale the newest candle is at `as_of`. None when empty."""
        if not self.timestamps:
            return None
        newest = _parse(self.newest_timestamp)
        return round((self.as_of - newest).total_seconds() / 86400.0, 3)

    def last_close(self) -> Optional[float]:
        return self.closes[-1] if self.closes else None

    def median_dollar_volume(self, window: int = 60) -> Optional[float]:
        pairs = list(zip(self.closes, self.volumes))[-window:]
        values = [c * v for c, v in pairs if c is not None and v is not None]
        return round(statistics.median(values), 4) if values else None

    def realized_volatility(self, window: int = 20) -> Optional[float]:
        """Stdev of daily simple returns over the window. None when short."""
        closes = self.closes[-(window + 1):]
        if len(closes) < 3:
            return None
        returns = [(closes[i] / closes[i - 1]) - 1.0
                   for i in range(1, len(closes)) if closes[i - 1]]
        if len(returns) < 2:
            return None
        return round(statistics.pstdev(returns), 8)

    def trailing_return(self, window: int = 20) -> Optional[float]:
        closes = self.closes[-(window + 1):]
        if len(closes) < 2 or not closes[0]:
            return None
        return round((closes[-1] / closes[0]) - 1.0, 8)

    def volume_ratio(self, recent: int = 5, baseline: int = 60
                     ) -> Optional[float]:
        """Recent volume against its own trailing median. None when short."""
        if len(self.volumes) < baseline // 2 or len(self.volumes) < recent:
            return None
        base = statistics.median(self.volumes[-baseline:])
        if not base:
            return None
        return round(statistics.median(self.volumes[-recent:]) / base, 6)


class MarketHistory:
    """
    Read-only, as-of-bounded access to the research candle cache.

    Every query carries `timestamp <= as_of`. The connection is opened
    `mode=ro`, so this object cannot write even by accident, and results
    are cached per instrument for the life of one selection run.
    """

    def __init__(self, db_path: str, as_of: datetime, *,
                 max_candles: int = 400):
        self.db_path = db_path
        self.as_of = require_utc(as_of, "as_of")
        self.max_candles = max_candles
        self._cache: Dict[str, InstrumentHistory] = {}
        uri = "file:%s?mode=ro" % os.path.abspath(db_path).replace("\\", "/")
        self._conn = sqlite3.connect(uri, uri=True)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "MarketHistory":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def history(self, instrument_id: str) -> InstrumentHistory:
        if instrument_id in self._cache:
            return self._cache[instrument_id]
        rows = self._conn.execute("""
            SELECT timestamp, close, volume FROM price_candle_cache
             WHERE instrument_id = ? AND interval = '1d' AND timestamp <= ?
          ORDER BY timestamp DESC LIMIT ?
        """, (instrument_id, self.as_of.isoformat(), self.max_candles)).fetchall()
        rows.reverse()                       # oldest first, newest last
        history = InstrumentHistory(
            instrument_id=instrument_id, as_of=self.as_of,
            timestamps=tuple(r[0] for r in rows),
            closes=tuple(r[1] for r in rows if r[1] is not None),
            volumes=tuple(r[2] for r in rows if r[2] is not None))
        self._cache[instrument_id] = history
        return history
