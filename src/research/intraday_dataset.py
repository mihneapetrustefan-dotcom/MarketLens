"""
src/research/intraday_dataset.py
-------------------------------------------
Time-grid intraday labels and the research dataset builder
(Phase 25.9F).

WHY A NEW LABEL FAMILY, AND WHY IT IS KEPT AWAY FROM D20
------------------------------------------------------------
`research_labels` already holds `intraday_5m`, `intraday_15m`,
`intraday_30m` and `intraday_60m`. Those are EVENT-ANCHORED: the return
from a canonical event's anchor over the following N minutes, produced
by `src/impact/anchoring.py` -- the same module the frozen D20
hypothesis depends on.

Intraday MODEL research needs something different: the return from an
arbitrary decision minute T, on a time grid, for any instrument with
bars. Writing those into the same rows would mix two method families
in one table and would touch the module D20 is frozen against.

So this builds a separate family, `grid_<H>m@v1`, computed here, never
written into `research_labels`, and never through `anchoring.py`.
Nothing in this file imports that module (asserted by test).

WHAT A LABEL MUST DECLARE
-----------------------------
Its resolution state. A forward return is not a number that is
sometimes missing -- it is a measurement that has not happened yet
(`UNRESOLVED`), cannot happen (`MISSING_FUTURE_DATA`), or did
(`RESOLVED`). Training on the first two is the leak this file exists
to make impossible: `training_rows()` returns RESOLVED only.

THE FORWARD PATH MUST BE CONTIGUOUS
---------------------------------------
A "thirty-minute forward return" whose endpoint sits on the other side
of a gap or an overnight break is an overnight gap wearing an intraday
name. The endpoint must be the bar exactly H minutes later, inside the
same unbroken run.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.features.engine import FeatureContext, FeatureEngine, FeatureRegistry
from src.features.intraday import (
    INTRADAY_FEATURE_VERSION, build_intraday_registry, intraday_feature_ids,
)
from src.marketdata.calendar import USEquityCalendar
from src.marketdata.intraday import (
    MINUTE, BarIndex, BarQuality, IntradayBar, bars_as_of, contiguous_runs,
    instruments_with_bars, load_research_bars, session_governed, trailing_run,
)

#: Bumping this invalidates every stored label of this family.
INTRADAY_LABEL_VERSION = "v1"

#: Horizons the builder can produce. Supported is not the same as
#: useful: §21 asks for the capability, and §28 decides which of them
#: the real data can actually carry.
SUPPORTED_HORIZONS = (5, 15, 30, 60)

#: The instrument whose bars stand for "the market" in context features.
DEFAULT_BENCHMARK = "benchmark-spy"


class _Lookup:
    """A uniform `.get` over either a dict or an index."""

    def __init__(self, getter):
        self._getter = getter

    def get(self, moment):
        return self._getter(moment)

    def __contains__(self, moment) -> bool:
        return self._getter(moment) is not None


class LabelState(str, Enum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"                  # the future bar has not happened yet
    MISSING_FUTURE_DATA = "missing_future_data"  # it happened; we have no bar
    INVALID = "invalid"


@dataclass(frozen=True)
class IntradayLabel:
    """One forward return, with the reason it is or is not a number."""
    horizon_minutes: int
    state: LabelState
    value: Optional[float] = None
    measured_at: Optional[datetime] = None
    version: str = INTRADAY_LABEL_VERSION

    @property
    def name(self) -> str:
        return f"grid_{self.horizon_minutes}m"

    @property
    def is_usable(self) -> bool:
        return self.state is LabelState.RESOLVED and self.value is not None


@dataclass
class IntradayObservation:
    """
    One (instrument, decision minute) row of the research dataset.

    `cutoff` is the decision moment. Every feature was computed from
    bars that had CLOSED by it; every label is measured strictly after
    it. That separation is the dataset's whole contract.
    """
    instrument_id: str
    cutoff: datetime
    features: Dict[str, Optional[float]] = field(default_factory=dict)
    labels: Dict[str, IntradayLabel] = field(default_factory=dict)
    run_minutes: int = 0
    quality: Tuple[str, ...] = ()
    feature_version: str = INTRADAY_FEATURE_VERSION
    label_version: str = INTRADAY_LABEL_VERSION

    @property
    def observation_id(self) -> str:
        """Stable identity: same instrument, same minute, same versions."""
        raw = (f"{self.instrument_id}|{self.cutoff.astimezone(timezone.utc).isoformat()}"
               f"|{self.feature_version}|{self.label_version}")
        return "iobs-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]

    def label(self, horizon: int) -> Optional[IntradayLabel]:
        return self.labels.get(f"grid_{horizon}m")

    def as_row(self) -> Dict[str, Any]:
        row: Dict[str, Any] = {
            "observation_id": self.observation_id,
            "instrument_id": self.instrument_id,
            "cutoff": self.cutoff.astimezone(timezone.utc).isoformat(),
            "run_minutes": self.run_minutes,
            "quality": list(self.quality),
            "feature_version": self.feature_version,
            "label_version": self.label_version,
        }
        row.update({f"x.{k}": v for k, v in sorted(self.features.items())})
        for name, label in sorted(self.labels.items()):
            row[f"y.{name}"] = label.value
            row[f"y.{name}.state"] = label.state.value
        return row


# ======================================================================
# Labels
# ======================================================================

def forward_return(bars: Sequence[IntradayBar], cutoff: datetime,
                   horizon_minutes: int,
                   now: Optional[datetime] = None,
                   index: Optional[BarIndex] = None) -> IntradayLabel:
    """
    The return from the bar closing at `cutoff` to the bar `horizon`
    minutes later, inside one unbroken run.

    `now` decides UNRESOLVED versus MISSING_FUTURE_DATA, and the
    difference matters: the first will resolve itself as data arrives,
    the second never will and must not be waited for.
    """
    anchor = cutoff.astimezone(timezone.utc)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    target_end = anchor + timedelta(minutes=horizon_minutes)

    # An index answers by lookup; without one the map is built here, so
    # a caller with a single label to compute needs no extra ceremony.
    lookup = (index.bar_ending if index is not None
              else {bar.bar_end: bar for bar in bars if bar.is_usable}.get)
    by_end = _Lookup(lookup)
    start = by_end.get(anchor)
    if start is None or not start.close:
        return IntradayLabel(horizon_minutes, LabelState.INVALID)

    end = by_end.get(target_end)
    if end is None:
        if target_end > now:
            return IntradayLabel(horizon_minutes, LabelState.UNRESOLVED)
        return IntradayLabel(horizon_minutes, LabelState.MISSING_FUTURE_DATA)

    # Every minute between must exist, or the "forward return" spans a
    # gap or a session break and is not an intraday move at all.
    cursor = anchor + MINUTE
    while cursor <= target_end:
        if by_end.get(cursor) is None:
            return IntradayLabel(horizon_minutes, LabelState.MISSING_FUTURE_DATA)
        cursor += MINUTE

    if not end.close:
        return IntradayLabel(horizon_minutes, LabelState.INVALID)
    return IntradayLabel(horizon_minutes, LabelState.RESOLVED,
                         value=end.close / start.close - 1.0,
                         measured_at=target_end)


# ======================================================================
# The builder
# ======================================================================

class IntradayDatasetBuilder:
    """
    The one canonical way to turn one-minute bars into a research
    dataset (§25).

    Model scripts must not assemble X and y themselves: a dataset built
    ad hoc is a dataset whose point-in-time guarantees nobody can
    check.
    """

    def __init__(self, conn: sqlite3.Connection,
                 registry: Optional[FeatureRegistry] = None,
                 benchmark_id: str = DEFAULT_BENCHMARK,
                 calendar: Optional[USEquityCalendar] = None,
                 horizons: Sequence[int] = SUPPORTED_HORIZONS):
        self.conn = conn
        self.registry = registry or build_intraday_registry()
        self.engine = FeatureEngine(self.registry)
        self.benchmark_id = benchmark_id
        self.calendar = calendar or USEquityCalendar()
        self.horizons = tuple(horizons)
        self.feature_ids = intraday_feature_ids(self.registry)
        self._bars: Dict[str, List[IntradayBar]] = {}
        self._index: Dict[str, BarIndex] = {}
        self._session_close_cache: Dict[str, Dict[Any, float]] = {}

    # ---------------- inputs ----------------

    def bars_for(self, instrument_id: str) -> List[IntradayBar]:
        if instrument_id not in self._bars:
            self._bars[instrument_id] = load_research_bars(
                self.conn, instrument_id, calendar=self.calendar,
                governs_session=session_governed(self.conn, instrument_id))
        return self._bars[instrument_id]

    def required_history(self) -> Dict[str, Any]:
        """
        What a restarting process must reload to reproduce a row exactly
        (§17, §18).

        MEASURED, NOT ASSUMED. Reloading a fixed 200-bar tail of real
        AAPL data reproduced 16 of 19 features and silently changed
        three:

            regime.run_minutes     201 -> 200   (the run was truncated)
            market.vwap_distance   differs      (VWAP of a shorter run)
            market.overnight_gap   value -> None (previous session gone)

        So the minimum is not a bar count. It is the WHOLE contiguous
        run containing the cutoff, plus the previous session's last
        close. `load_research_bars` reads the full series from storage
        and therefore satisfies this by construction; the requirement
        matters for any caller that seeds bars by hand.

        `regime.run_minutes` travels in every row precisely so a
        truncated reload is visible rather than merely wrong.
        """
        return {
            "contiguous_run": "the entire unbroken run containing the cutoff",
            "previous_session_close": "the last close of the prior session date",
            "why": ("run_minutes, vwap_distance and overnight_gap are defined "
                    "over the whole run and the session before it"),
            "satisfied_by": "load_research_bars (reads the full stored series)",
        }

    def seed_bars(self, instrument_id: str, bars: Sequence[IntradayBar]) -> None:
        """Inject bars directly. For fixtures and for restart rehearsals."""
        self._bars[instrument_id] = list(bars)
        self._index.pop(instrument_id, None)
        self._session_close_cache.pop(instrument_id, None)

    def index_for(self, instrument_id: str) -> BarIndex:
        """The instrument's bars, indexed once and reused."""
        if instrument_id not in self._index:
            self._index[instrument_id] = BarIndex(self.bars_for(instrument_id))
        return self._index[instrument_id]

    # ---------------- market context ----------------

    def market_returns(self, cutoff: datetime) -> Dict[int, Optional[float]]:
        """
        Benchmark returns at this cutoff, from the benchmark's OWN bars.

        Never derived from the instrument being labelled (§31).
        """
        index = self.index_for(self.benchmark_id)
        out: Dict[int, Optional[float]] = {}
        for minutes in (5, 30):
            run = index.trailing_run(cutoff, minimum=minutes + 1)
            if not run:
                out[minutes] = None
                continue
            window = run[-(minutes + 1):]
            first, last = window[0].close, window[-1].close
            out[minutes] = (last / first - 1.0) if first else None
        return out

    def cross_sectional_dispersion(self, cutoff: datetime,
                                   instrument_ids: Sequence[str]
                                   ) -> Optional[float]:
        """
        Spread of one-minute returns across instruments closed at this
        exact cutoff.

        Only instruments with a bar ending at the cutoff take part, so
        membership is decided by what had happened, not by who is in
        the universe today (§37, §55).
        """
        import statistics
        returns: List[float] = []
        for instrument_id in instrument_ids:
            run = self.index_for(instrument_id).trailing_run(cutoff, minimum=2)
            if len(run) < 2:
                continue
            previous, latest = run[-2].close, run[-1].close
            if previous:
                returns.append(latest / previous - 1.0)
        if len(returns) < 3:
            return None
        return statistics.pstdev(returns)

    def previous_session_close(self, instrument_id: str,
                               cutoff: datetime) -> Optional[float]:
        """
        Last close observed on a session date before the cutoff's.

        Answered from a per-session index built once. The scan version
        was O(bars) for EVERY observation, which on the benchmark's
        25,706 minutes meant re-reading the whole series 25,706 times;
        the result is identical, asserted by test.
        """
        anchor = cutoff.astimezone(timezone.utc)
        closes = self._session_closes(instrument_id)
        earlier = [day for day in closes if day < anchor.date()]
        return closes[earlier[-1]] if earlier else None

    def _session_closes(self, instrument_id: str) -> Dict[Any, float]:
        """Last close per session date, ordered, computed once."""
        cached = self._session_close_cache.get(instrument_id)
        if cached is None:
            cached = {}
            for bar in self.bars_for(instrument_id):
                if bar.close is not None:
                    cached[bar.session_date] = bar.close   # later wins
            cached = dict(sorted(cached.items()))
            self._session_close_cache[instrument_id] = cached
        return cached

    # ---------------- one observation ----------------

    def observation(self, instrument_id: str, cutoff: datetime, *,
                    peers: Sequence[str] = (),
                    now: Optional[datetime] = None
                    ) -> Optional[IntradayObservation]:
        """
        One row, or None when the instrument has no closed bar at the
        cutoff.

        Returning None rather than a row of Nones is deliberate: a row
        that exists but knows nothing still counts in every sample-size
        statistic downstream.
        """
        bars = self.bars_for(instrument_id)
        run = self.index_for(instrument_id).trailing_run(cutoff, minimum=1)
        if not run:
            return None

        context = FeatureContext(
            cutoff=cutoff.astimezone(timezone.utc),
            instrument_id=instrument_id,
            candles=run,
            metadata={
                "market_returns": self.market_returns(cutoff),
                "cross_sectional_dispersion":
                    self.cross_sectional_dispersion(cutoff, peers) if peers else None,
                "previous_session_close":
                    self.previous_session_close(instrument_id, cutoff),
            })

        features: Dict[str, Optional[float]] = {}
        for feature_id in self.feature_ids:
            value = self.engine.compute_one(feature_id, context)
            features[feature_id] = None if value is None else value.value

        labels = {}
        index = self.index_for(instrument_id)
        for horizon in self.horizons:
            label = forward_return(bars, cutoff, horizon, now=now, index=index)
            labels[label.name] = label

        quality = sorted({q.value for bar in run[-1:] for q in bar.quality})
        return IntradayObservation(
            instrument_id=instrument_id, cutoff=cutoff.astimezone(timezone.utc),
            features=features, labels=labels, run_minutes=len(run),
            quality=tuple(quality))

    # ---------------- many ----------------

    def build(self, instrument_ids: Sequence[str],
              cutoffs: Optional[Sequence[datetime]] = None, *,
              now: Optional[datetime] = None,
              minimum_run: int = 1) -> "IntradayDataset":
        """
        Every observation for these instruments at these minutes.

        With no explicit cutoffs, every closed bar end becomes a
        candidate decision minute -- which is what makes the dataset a
        grid rather than a sample somebody chose.
        """
        instrument_ids = list(instrument_ids)
        peers = [i for i in instrument_ids if i != self.benchmark_id]

        rows: List[IntradayObservation] = []
        for instrument_id in instrument_ids:
            # ONLY this instrument's own bar ends. `observation` returns
            # None unless a bar of THIS instrument ends exactly at the
            # cutoff, so every other moment in a shared grid was work
            # that could only produce None -- and on the real corpus the
            # benchmark alone contributes 25,706 such moments to each of
            # its peers. Identical rows, asserted by test.
            if cutoffs is None:
                moments = self.index_for(instrument_id).bar_ends
            else:
                moments = sorted({c.astimezone(timezone.utc) for c in cutoffs})
            for cutoff in moments:
                row = self.observation(instrument_id, cutoff, peers=peers, now=now)
                if row is not None and row.run_minutes >= minimum_run:
                    rows.append(row)
        rows.sort(key=lambda r: (r.cutoff, r.instrument_id))
        return IntradayDataset(rows=rows, feature_ids=list(self.feature_ids),
                               horizons=list(self.horizons),
                               instrument_ids=instrument_ids,
                               benchmark_id=self.benchmark_id)


# ======================================================================
# The dataset
# ======================================================================

@dataclass
class IntradayDataset:
    rows: List[IntradayObservation]
    feature_ids: List[str]
    horizons: List[int]
    instrument_ids: List[str]
    benchmark_id: str = DEFAULT_BENCHMARK
    feature_version: str = INTRADAY_FEATURE_VERSION
    label_version: str = INTRADAY_LABEL_VERSION

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def data_cutoff(self) -> Optional[str]:
        """The newest decision minute represented. Part of the identity."""
        if not self.rows:
            return None
        return max(r.cutoff for r in self.rows).isoformat()

    def training_rows(self, horizon: int) -> List[IntradayObservation]:
        """
        Only rows whose label for this horizon actually resolved.

        The single most important accessor in this file: an unresolved
        label is the future, and a model trained on it has been handed
        the answer sheet (§23).
        """
        name = f"grid_{horizon}m"
        return [r for r in self.rows
                if (r.labels.get(name) is not None and r.labels[name].is_usable)]

    def matrix(self, horizon: int, feature_ids: Optional[Sequence[str]] = None
               ) -> Tuple[List[List[Optional[float]]], List[float],
                          List[datetime], List[str]]:
        """`(X, y, cutoffs, instrument_ids)` for one horizon, resolved only."""
        columns = list(feature_ids or self.feature_ids)
        rows = self.training_rows(horizon)
        X = [[r.features.get(c) for c in columns] for r in rows]
        y = [r.labels[f"grid_{horizon}m"].value for r in rows]
        return X, y, [r.cutoff for r in rows], [r.instrument_id for r in rows]

    def temporal_split(self, fraction: float = 0.7, horizon: int = 0
                       ) -> Tuple[List[IntradayObservation],
                                  List[IntradayObservation]]:
        """
        Split in time, cutting BETWEEN minutes and never inside one.

        THE MISTAKE THIS PREVENTS. Slicing a time-ordered list by row
        index cuts wherever the index lands -- and because a cross
        section puts several instruments on the SAME minute, that minute
        then appears in both halves. Measured on the real rehearsal
        dataset: a 70% index split produced a scaler whose
        `fitted_through` was exactly the first validation cutoff, i.e.
        the boundary minute was on both sides.

        `horizon` additionally embargoes the label window: a training
        row whose forward return runs past the boundary has seen the
        validation period, so it is dropped rather than quietly kept.
        """
        if not self.rows:
            return [], []
        moments = sorted({r.cutoff for r in self.rows})
        boundary = moments[max(0, min(len(moments) - 1,
                                      int(len(moments) * fraction)))]
        embargo = boundary - timedelta(minutes=horizon)
        training = [r for r in self.rows if r.cutoff < embargo]
        validation = [r for r in self.rows if r.cutoff >= boundary]
        return training, validation

    def cross_section(self, cutoff: datetime) -> List[IntradayObservation]:
        """Every instrument observed at exactly this minute (§30)."""
        anchor = cutoff.astimezone(timezone.utc)
        return [r for r in self.rows if r.cutoff == anchor]

    def label_states(self, horizon: int) -> Dict[str, int]:
        name = f"grid_{horizon}m"
        counts: Dict[str, int] = {}
        for row in self.rows:
            label = row.labels.get(name)
            state = label.state.value if label else "absent"
            counts[state] = counts.get(state, 0) + 1
        return counts

    def feature_missingness(self) -> Dict[str, float]:
        """Share of rows where each feature could not be computed."""
        if not self.rows:
            return {}
        return {fid: sum(1 for r in self.rows if r.features.get(fid) is None)
                / len(self.rows) for fid in self.feature_ids}

    # ---------------- identity ----------------

    def fingerprint(self) -> str:
        """
        Identity over CONTENT, the Phase 25.9D rule applied here.

        Changes when eligible rows, feature values, label values,
        versions or the cutoff change -- because a dataset whose
        identity is only its definition is exactly how a stale result
        gets served as current research.
        """
        payload = json.dumps({
            "feature_version": self.feature_version,
            "label_version": self.label_version,
            "feature_ids": sorted(self.feature_ids),
            "horizons": sorted(self.horizons),
            "benchmark": self.benchmark_id,
            "data_cutoff": self.data_cutoff,
            "rows": [
                [r.observation_id,
                 [r.features.get(f) for f in sorted(self.feature_ids)],
                 [(n, l.state.value, l.value) for n, l in sorted(r.labels.items())]]
                for r in sorted(self.rows, key=lambda x: (x.cutoff, x.instrument_id))
            ],
        }, sort_keys=True, default=str, separators=(",", ":"))
        return "ids-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def identity(self) -> Dict[str, Any]:
        return {
            "fingerprint": self.fingerprint(),
            "rows": len(self.rows),
            "instruments": len(self.instrument_ids),
            "feature_version": self.feature_version,
            "label_version": self.label_version,
            "data_cutoff": self.data_cutoff,
        }
