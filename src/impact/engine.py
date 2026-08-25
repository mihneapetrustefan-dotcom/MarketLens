"""
src/impact/engine.py
-------------------------
The Event Study engine (Phase 6, spec §2-§6, §14-§20, §30, §31).

    EVENT -> MARKET VISIBILITY TIME -> WINDOWS -> RETURNS
          -> ABNORMAL RETURNS -> VOLUME/VOLATILITY -> CONTEXT
          -> CONFOUNDERS -> IMPACT DIMENSIONS

THE ARCHITECTURAL COMMITMENT (spec §31): the information set for every
pre-event calculation is drawn through a PointInTimeView anchored at
the event's LATEST plausible visibility. Post-event measurement uses
the explicitly-named outcome accessors. The two cannot be confused,
because a leak raises LookAheadViolation rather than returning a
number.

WHY THE ANCHOR IS THE *LATEST* VISIBILITY, NOT THE EARLIEST: when the
public moment is uncertain, assuming we knew it EARLY would credit the
system with information it might not have had. Anchoring late is the
conservative direction, and conservatism here means "never overstate
what was knowable".
"""

import uuid
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Callable

from src.domain.impact_models import (
    EventStudy, EventWindow, WindowKind, WindowUnit, DEFAULT_WINDOWS,
    ReturnMeasurement, ReturnMethod, BenchmarkModel, VolumeReaction, VolatilityReaction,
    GapAnalysis, DataQuality, DataQualityIssue, DataQualityLevel,
    SectorContext, PeerContext, MacroContext, MarketRegime, RegimeTrend, RegimeVolatility,
    ConfoundingEvent, ImpactDimensions, ImpactDirection, ImpactMagnitude, ImpactSpeed,
    ImpactDuration, ImpactBreadth, ImpactLevel, ImpactProfile, ReactionDistribution,
)
from src.impact import calculations as calc
from src.pointintime.view import PointInTimeView, build_view, market_visibility_time, LookAheadViolation

logger = logging.getLogger("marketlens.impact.engine")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


#: Minimum observations before a baseline is trustworthy. Below this,
#: the study is marked INSUFFICIENT_HISTORY and becomes unusable rather
#: than producing a statistic from four data points.
MIN_BASELINE_OBSERVATIONS = 20

#: Abnormal-return magnitude bands. Chosen as round, defensible
#: thresholds for equities and documented as such — not calibrated
#: against this dataset, because calibrating bands on the same data
#: they then describe is circular.
MAGNITUDE_SMALL = 0.01
MAGNITUDE_LARGE = 0.05


class Candle:
    """
    Minimal OHLCV record the engine consumes.

    Deliberately structural rather than tied to Phase 1's
    MarketObservation: the engine must work with any price source
    (yfinance today, a licensed provider later) without changing, and
    duplicating the canonical model here would create a second source
    of truth.
    """

    def __init__(self, timestamp: datetime, open_: Optional[float] = None, high: Optional[float] = None,
                 low: Optional[float] = None, close: Optional[float] = None,
                 volume: Optional[float] = None, adjusted_close: Optional[float] = None,
                 is_halted: bool = False):
        if timestamp.tzinfo is None:
            raise ValueError("candle timestamp must be timezone-aware")
        self.timestamp = timestamp
        self.open = open_
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume
        self.adjusted_close = adjusted_close
        self.is_halted = is_halted

    @property
    def price(self) -> Optional[float]:
        """
        Adjusted close preferred over raw close (spec §27).

        Using unadjusted prices across a split produces a fabricated
        -50% return. Preferring the adjusted series is the single most
        effective guard against that, and the fallback to raw close is
        flagged by the caller as a data-quality issue rather than used
        silently.
        """
        return self.adjusted_close if self.adjusted_close is not None else self.close

    @property
    def uses_adjusted(self) -> bool:
        return self.adjusted_close is not None


class EventStudyEngine:
    """Computes event studies with structurally-enforced look-ahead protection."""

    def __init__(self, windows: Optional[List[EventWindow]] = None,
                 return_method: ReturnMethod = ReturnMethod.SIMPLE,
                 min_baseline: int = MIN_BASELINE_OBSERVATIONS):
        self.windows = windows or list(DEFAULT_WINDOWS)
        self.return_method = return_method
        self.min_baseline = min_baseline

    # ---------------- timing ----------------

    def resolve_visibility(self, event_time: Optional[datetime], publication_time: Optional[datetime],
                            ingestion_time: Optional[datetime] = None):
        """
        Determine when the market could have known (spec §3, §5).

        Returns the TimeUncertainty range, or None when neither
        timestamp exists — in which case the study cannot be anchored
        at all and must be rejected, not estimated.
        """
        return market_visibility_time(event_time, publication_time, ingestion_time)

    # ---------------- windows ----------------

    def _candle_at_or_before(self, candles: Sequence[Candle], moment: datetime) -> Optional[Candle]:
        eligible = [c for c in candles if c.timestamp <= moment and c.price is not None]
        return max(eligible, key=lambda c: c.timestamp) if eligible else None

    def _candle_at_or_after(self, candles: Sequence[Candle], moment: datetime) -> Optional[Candle]:
        eligible = [c for c in candles if c.timestamp >= moment and c.price is not None]
        return min(eligible, key=lambda c: c.timestamp) if eligible else None

    def window_bounds(self, anchor: datetime, window: EventWindow,
                       session_timestamps: Optional[Sequence[datetime]] = None):
        """
        Resolve a window's start and end moments.

        MINUTES is wall-clock arithmetic. TRADING_DAYS walks an actual
        session calendar — supplied by the caller, because only the
        real price series knows which days were sessions. Counting
        calendar days instead would silently shorten every window that
        spans a weekend or holiday (spec §2, §38.17).
        """
        if window.unit == WindowUnit.MINUTES:
            return (anchor + timedelta(minutes=window.start_offset),
                    anchor + timedelta(minutes=window.end_offset))

        if not session_timestamps:
            return None, None

        sessions = sorted(set(session_timestamps))
        future = [s for s in sessions if s >= anchor]
        past = [s for s in sessions if s <= anchor]

        def nth_session(offset: float) -> Optional[datetime]:
            index = int(offset)
            if index >= 0:
                return future[index] if index < len(future) else None
            back = past[index:] if abs(index) <= len(past) else None
            return back[0] if back else None

        return nth_session(window.start_offset), nth_session(window.end_offset)

    # ---------------- core study ----------------

    def build_study(
        self,
        event_id: str,
        instrument_id: str,
        candles: Sequence[Candle],
        event_time: Optional[datetime] = None,
        publication_time: Optional[datetime] = None,
        ingestion_time: Optional[datetime] = None,
        benchmark_id: Optional[str] = None,
        benchmark_candles: Optional[Sequence[Candle]] = None,
        benchmark_model: BenchmarkModel = BenchmarkModel.MARKET_ADJUSTED,
        is_direct: bool = True,
        session_timestamps: Optional[Sequence[datetime]] = None,
    ) -> EventStudy:
        """
        Compute one event study.

        Never raises on bad data: every problem becomes a DataQuality
        issue on the returned study, and an UNUSABLE study is one the
        caller must exclude from statistics (spec §26).
        """
        visibility = self.resolve_visibility(event_time, publication_time, ingestion_time)
        study = EventStudy(
            study_id=f"es-{uuid.uuid4().hex[:16]}",
            event_id=event_id, instrument_id=instrument_id, benchmark_id=benchmark_id,
            event_time=event_time, publication_time=publication_time, ingestion_time=ingestion_time,
            computed_at=datetime.now(timezone.utc), is_direct=is_direct,
        )

        if visibility is None:
            study.quality.add_issue(DataQualityIssue.BAD_TIMESTAMP,
                                     "no event or publication time — study cannot be anchored")
            return study

        study.market_visibility_earliest = visibility.earliest
        study.market_visibility_latest = visibility.latest
        study.visibility_basis = visibility.basis
        if not visibility.is_precise:
            study.quality.add_issue(
                DataQualityIssue.UNCERTAIN_EVENT_TIME,
                f"public moment known only within {visibility.uncertainty_seconds:.0f}s ({visibility.basis})")
        if "INCONSISTENT" in visibility.basis:
            study.quality.add_issue(DataQualityIssue.BAD_TIMESTAMP, visibility.basis)
            return study

        # ANCHOR CONSERVATIVELY: latest plausible visibility.
        anchor = visibility.latest
        view: PointInTimeView[Candle] = build_view(
            anchor, candles, lambda c: c.timestamp, label=f"candles:{instrument_id}")

        known = view.known()
        if len(known) < self.min_baseline:
            study.quality.observations_available = len(known)
            study.quality.observations_expected = self.min_baseline
            study.quality.add_issue(
                DataQualityIssue.INSUFFICIENT_HISTORY,
                f"only {len(known)} pre-event observations, need {self.min_baseline}")
            return study

        study.quality.observations_available = len(known)
        study.quality.observations_expected = self.min_baseline

        # DERIVE THE SESSION CALENDAR FROM THE PRICE SERIES ITSELF when
        # the caller did not supply one. Without this, every
        # TRADING_DAYS window silently produced no result at all — a
        # caller would see an empty study and no error, which is the
        # worst possible failure mode. The candles ARE the record of
        # which moments were tradeable, so they are the correct default.
        if session_timestamps is None:
            session_timestamps = sorted({c.timestamp for c in candles if c.price is not None})

        if any(not c.uses_adjusted for c in known):
            study.quality.add_issue(
                DataQualityIssue.UNADJUSTED_CORPORATE_ACTION,
                "some candles lack an adjusted close; split/dividend distortion possible")
        if any(c.is_halted for c in candles):
            study.quality.add_issue(DataQualityIssue.TRADING_HALT, "a trading halt overlaps the study period")

        self._compute_returns(study, view, candles, benchmark_candles, benchmark_model, session_timestamps, anchor)
        self._compute_volume(study, view, candles, session_timestamps, anchor)
        self._compute_volatility(study, view, candles, session_timestamps, anchor)
        self._compute_gap(study, view, candles, anchor)
        return study

    def _compute_returns(self, study, view, candles, benchmark_candles, benchmark_model,
                          session_timestamps, anchor) -> None:
        baseline = self._candle_at_or_before(view.known(), anchor)
        if baseline is None:
            study.quality.add_issue(DataQualityIssue.MISSING_CANDLES, "no pre-event price available")
            return

        for window in self.windows:
            start, end = self.window_bounds(anchor, window, session_timestamps)
            if start is None or end is None:
                continue

            if window.kind == WindowKind.PRE_EVENT:
                # Pre-event windows read ONLY the information set.
                before = self._candle_at_or_before(view.known(), start)
                after = self._candle_at_or_before(view.known(), end)
            else:
                before = baseline
                after = self._candle_at_or_after(candles, end)

            measurement = ReturnMeasurement(window_name=window.name, method=self.return_method,
                                             benchmark_id=study.benchmark_id, benchmark_model=benchmark_model)
            if before is None or after is None:
                measurement.quality.add_issue(DataQualityIssue.MISSING_CANDLES,
                                               f"no price for window '{window.name}'")
                study.returns[window.name] = measurement
                continue

            measurement.price_before = Decimal(str(before.price))
            measurement.price_after = Decimal(str(after.price))
            measurement.raw_return = calc.compute_return(before.price, after.price, self.return_method)

            if benchmark_candles:
                b_before = self._candle_at_or_before(benchmark_candles, before.timestamp)
                b_after = (self._candle_at_or_before(benchmark_candles, after.timestamp)
                            if window.kind == WindowKind.PRE_EVENT
                            else self._candle_at_or_after(benchmark_candles, after.timestamp))
                if b_before and b_after:
                    measurement.benchmark_return = calc.compute_return(b_before.price, b_after.price, self.return_method)
                    measurement.expected_return = calc.expected_return_market_adjusted(measurement.benchmark_return)
                    measurement.abnormal_return = calc.compute_abnormal_return(
                        measurement.raw_return, measurement.expected_return)
                else:
                    measurement.quality.add_issue(DataQualityIssue.NO_BENCHMARK_DATA,
                                                   "benchmark prices unavailable for this window")
            study.returns[window.name] = measurement

    def _compute_volume(self, study, view, candles, session_timestamps, anchor) -> None:
        baseline_volumes = [c.volume for c in view.known() if c.volume is not None]
        mean, std = calc.baseline_statistics(baseline_volumes)
        if mean is None:
            return

        for window in self.windows:
            if window.kind == WindowKind.PRE_EVENT:
                continue
            _, end = self.window_bounds(anchor, window, session_timestamps)
            if end is None:
                continue
            event_candle = self._candle_at_or_after(candles, anchor)
            if event_candle is None or event_candle.volume is None:
                continue
            reaction = VolumeReaction(
                window_name=window.name, event_volume=event_candle.volume,
                baseline_mean_volume=mean, baseline_std_volume=std,
                relative_volume=calc.relative_volume(event_candle.volume, mean),
                volume_zscore=calc.volume_zscore(event_candle.volume, mean, std),
            )
            study.volume[window.name] = reaction
            break   # one volume reaction per study; the event-day figure is the meaningful one

    def _compute_volatility(self, study, view, candles, session_timestamps, anchor) -> None:
        known = view.known()
        pre_returns = self._series_returns(known)
        pre_vol = calc.realized_volatility(pre_returns)

        post_candles = [c for c in candles if c.timestamp > anchor and c.price is not None]
        post_returns = self._series_returns(sorted(post_candles, key=lambda c: c.timestamp))
        post_vol = calc.realized_volatility(post_returns)

        if pre_vol is None and post_vol is None:
            return
        study.volatility["overall"] = VolatilityReaction(
            window_name="overall", pre_volatility=pre_vol, post_volatility=post_vol,
            volatility_change_pct=calc.volatility_change_pct(pre_vol, post_vol),
        )

    def _series_returns(self, candles: Sequence[Candle]) -> List[float]:
        ordered = sorted([c for c in candles if c.price is not None], key=lambda c: c.timestamp)
        returns = []
        for previous, current in zip(ordered, ordered[1:]):
            value = calc.compute_return(previous.price, current.price, self.return_method)
            if value is not None:
                returns.append(value)
        return returns

    def _compute_gap(self, study, view, candles, anchor) -> None:
        """Overnight behaviour (spec §13) — matters because events cluster outside session hours."""
        previous = self._candle_at_or_before(view.known(), anchor)
        following = self._candle_at_or_after(candles, anchor)
        if previous is None or following is None:
            return
        during_session = following.timestamp.date() == anchor.date()
        gap = GapAnalysis(occurred_during_session=during_session,
                           previous_close=Decimal(str(previous.price)) if previous.price else None,
                           next_open=Decimal(str(following.open)) if following.open else None)
        if previous.price and following.open:
            gap.gap_return = calc.gap_return(previous.price, following.open, self.return_method)
        if following.open and following.close:
            gap.intraday_followthrough = calc.compute_return(following.open, following.close, self.return_method)
        study.gap = gap

    # ---------------- context ----------------

    def compute_market_regime(self, benchmark_candles: Sequence[Candle], anchor: datetime,
                               benchmark_id: Optional[str] = None,
                               lookback: int = 60) -> MarketRegime:
        """
        Classify the market backdrop (spec §14).

        NOT "bull = price up". Trend compares the benchmark's latest
        level to its own trailing mean; the volatility regime is a
        percentile of trailing realized volatility. Both use ONLY
        pre-anchor data, and both report UNKNOWN when history is too
        short rather than guessing from a handful of points.
        """
        view = build_view(anchor, benchmark_candles, lambda c: c.timestamp, label="benchmark")
        known = sorted(view.known(), key=lambda c: c.timestamp)[-lookback:]
        regime = MarketRegime(benchmark_id=benchmark_id,
                               method=f"trailing {lookback} observations vs their own mean; volatility percentile")
        if len(known) < 10:
            return regime

        prices = [c.price for c in known if c.price is not None]
        if len(prices) < 10:
            return regime

        mean_price = sum(prices) / len(prices)
        latest = prices[-1]
        regime.trailing_return = calc.compute_return(prices[0], latest, self.return_method)
        deviation = (latest - mean_price) / mean_price if mean_price else 0.0
        if deviation > 0.02:
            regime.trend = RegimeTrend.UPTREND
        elif deviation < -0.02:
            regime.trend = RegimeTrend.DOWNTREND
        else:
            regime.trend = RegimeTrend.RANGEBOUND

        returns = self._series_returns(known)
        regime.trailing_volatility = calc.realized_volatility(returns)
        if regime.trailing_volatility is not None and len(returns) >= 20:
            rolling = [calc.realized_volatility(returns[i:i + 10]) for i in range(len(returns) - 10)]
            rolling = [r for r in rolling if r is not None]
            if rolling:
                below = sum(1 for r in rolling if r < regime.trailing_volatility)
                regime.volatility_percentile = round(below / len(rolling), 4)
                if regime.volatility_percentile >= 0.8:
                    regime.volatility_regime = RegimeVolatility.ELEVATED
                elif regime.volatility_percentile <= 0.2:
                    regime.volatility_regime = RegimeVolatility.LOW
                else:
                    regime.volatility_regime = RegimeVolatility.NORMAL
        return regime

    def detect_confounders(self, study: EventStudy, other_events: Sequence[Dict[str, Any]],
                            window_hours: float = 24.0) -> List[ConfoundingEvent]:
        """
        Find other catalysts overlapping the study window (spec §18).

        Any overlap suspends ATTRIBUTION — not measurement. The move is
        still recorded; what becomes impermissible is assigning any
        part of it to this event.
        """
        anchor = study.market_visibility_latest
        if anchor is None:
            return []
        found = []
        for other in other_events:
            moment = other.get("occurred_at")
            if not isinstance(moment, datetime) or moment.tzinfo is None:
                continue
            if abs((moment - anchor).total_seconds()) <= window_hours * 3600:
                found.append(ConfoundingEvent(
                    event_id=other.get("event_id"), description=other.get("description", ""),
                    kind=other.get("kind", "unknown"), occurred_at=moment,
                    severity=other.get("severity", "unknown"),
                ))
        study.confounding_events.extend(found)
        return found

    # ---------------- dimensions & profile ----------------

    def classify_dimensions(self, study: EventStudy,
                             primary_window: str = "d1",
                             persistence_window: str = "d5") -> ImpactDimensions:
        """
        Express impact across independent axes (spec §19).

        Uses ABNORMAL return where available and falls back to raw only
        when no benchmark existed — with the fallback reflected in
        measurement_confidence, since a raw move without a benchmark is
        a much weaker statement about the event.
        """
        dimensions = ImpactDimensions()
        measurement = study.returns.get(primary_window)
        if measurement is None:
            return dimensions

        value = measurement.abnormal_return if measurement.has_abnormal else measurement.raw_return
        if value is None:
            return dimensions

        if value > MAGNITUDE_SMALL:
            dimensions.direction = ImpactDirection.POSITIVE
        elif value < -MAGNITUDE_SMALL:
            dimensions.direction = ImpactDirection.NEGATIVE
        else:
            dimensions.direction = ImpactDirection.NEUTRAL

        size = abs(value)
        if size >= MAGNITUDE_LARGE:
            dimensions.magnitude = ImpactMagnitude.LARGE
        elif size >= MAGNITUDE_SMALL:
            dimensions.magnitude = ImpactMagnitude.MEDIUM
        else:
            dimensions.magnitude = ImpactMagnitude.SMALL

        intraday = study.returns.get("intraday_60m")
        if intraday and intraday.raw_return is not None and value != 0:
            captured = abs(intraday.raw_return) / abs(value)
            dimensions.speed = ImpactSpeed.IMMEDIATE if captured >= 0.5 else ImpactSpeed.DELAYED

        later = study.returns.get(persistence_window)
        if later:
            later_value = later.abnormal_return if later.has_abnormal else later.raw_return
            if later_value is not None:
                if abs(later_value) >= abs(value) * 0.75:
                    dimensions.duration = ImpactDuration.PERSISTENT
                elif abs(later_value) >= abs(value) * 0.3:
                    dimensions.duration = ImpactDuration.MEDIUM
                else:
                    dimensions.duration = ImpactDuration.SHORT

        volume = next(iter(study.volume.values()), None)
        if volume and volume.relative_volume is not None:
            dimensions.volume_impact = (ImpactLevel.HIGH if volume.relative_volume >= 2.0
                                         else ImpactLevel.MEDIUM if volume.relative_volume >= 1.3
                                         else ImpactLevel.LOW)

        volatility = study.volatility.get("overall")
        if volatility and volatility.volatility_change_pct is not None:
            change = volatility.volatility_change_pct
            dimensions.volatility_impact = (ImpactLevel.HIGH if change >= 0.5
                                             else ImpactLevel.MEDIUM if change >= 0.15
                                             else ImpactLevel.LOW)

        confidence = 1.0
        if not measurement.has_abnormal:
            confidence -= 0.3         # no benchmark: a much weaker claim
        if study.has_confounders:
            confidence -= 0.4         # attribution impossible
        if study.quality.level == DataQualityLevel.LOW:
            confidence -= 0.2
        elif study.quality.level == DataQualityLevel.MEDIUM:
            confidence -= 0.1
        dimensions.measurement_confidence = round(max(0.0, confidence), 4)
        return dimensions

    def build_profile(self, event_id: str, studies: Sequence[EventStudy],
                       event_confidence: Optional[float] = None,
                       comparable_count: int = 0) -> ImpactProfile:
        """
        Summarize an event's measured impact (spec §25).

        `event_confidence` is carried through untouched and never mixed
        into the impact score — they answer different questions (spec
        §25), and blending them would make a well-evidenced non-event
        indistinguishable from a dubious large move.
        """
        primary = next((s for s in studies if s.is_direct and s.quality.is_usable), None)
        profile = ImpactProfile(
            profile_id=f"ip-{uuid.uuid4().hex[:16]}", event_id=event_id,
            primary_instrument_id=primary.instrument_id if primary else None,
            study_ids=[s.study_id for s in studies],
            event_confidence=event_confidence, comparable_event_count=comparable_count,
            computed_at=datetime.now(timezone.utc),
            methodology_note=("abnormal return = asset return - benchmark return (market-adjusted, beta assumed 1.0); "
                               "impact score combines abnormal magnitude, volume and volatility abnormality, "
                               "scaled by measurement confidence"),
        )
        if primary is None:
            profile.quality.add_issue(DataQualityIssue.INSUFFICIENT_HISTORY, "no usable direct study")
            return profile

        profile.dimensions = self.classify_dimensions(primary)
        profile.quality = primary.quality
        profile.impact_score = self.compute_impact_score(primary, profile.dimensions)

        breadth = ImpactBreadth.COMPANY
        indirect = [s for s in studies if not s.is_direct and s.quality.is_usable]
        if indirect:
            moved = sum(1 for s in indirect
                         if (s.abnormal_return("d1") or 0) and abs(s.abnormal_return("d1")) >= MAGNITUDE_SMALL)
            if moved >= max(1, len(indirect) // 2):
                breadth = ImpactBreadth.SECTOR
        profile.dimensions.breadth = breadth
        return profile

    def compute_impact_score(self, study: EventStudy, dimensions: ImpactDimensions) -> Optional[float]:
        """
        A normalized 0-1 impact score (spec §20).

        EXPLICITLY NOT sentiment x price move — sentiment is not an
        input at all. Inputs are measured market behaviour only:
        abnormal magnitude, volume abnormality, volatility change, each
        capped, then scaled by measurement confidence so a confounded
        or benchmark-less study cannot score highly.

        A HIGH SCORE MEANS "the market moved a lot around this event",
        NOT "this is a good opportunity" — the score is descriptive,
        and Phase 6 produces no forward-looking claim of any kind.
        """
        measurement = study.returns.get("d1")
        if measurement is None:
            return None
        value = measurement.abnormal_return if measurement.has_abnormal else measurement.raw_return
        if value is None:
            return None

        magnitude_component = min(1.0, abs(value) / MAGNITUDE_LARGE)

        volume = next(iter(study.volume.values()), None)
        volume_component = 0.0
        if volume and volume.relative_volume is not None:
            volume_component = min(1.0, max(0.0, (volume.relative_volume - 1.0) / 2.0))

        volatility = study.volatility.get("overall")
        volatility_component = 0.0
        if volatility and volatility.volatility_change_pct is not None:
            volatility_component = min(1.0, max(0.0, volatility.volatility_change_pct))

        raw = 0.55 * magnitude_component + 0.25 * volume_component + 0.20 * volatility_component
        return round(min(1.0, raw * max(0.1, dimensions.measurement_confidence)), 4)

    # ---------------- distributions ----------------

    def reaction_distribution(self, studies: Sequence[EventStudy], window_name: str = "d1",
                               thresholds: Sequence[float] = (0.01, 0.03)) -> ReactionDistribution:
        """
        Descriptive statistics over comparable events' reactions (spec
        §21, §24).

        Only USABLE studies contribute — a distribution silently
        including unusable ones is worse than no distribution. Small
        samples are flagged, never quietly presented as if robust, and
        no probability here is a forecast: they describe what happened,
        not what will.
        """
        values = []
        for study in studies:
            if not study.quality.is_usable:
                continue
            measurement = study.returns.get(window_name)
            if measurement is None:
                continue
            value = measurement.abnormal_return if measurement.has_abnormal else measurement.raw_return
            if value is not None:
                values.append(value)

        distribution = ReactionDistribution(window_name=window_name, sample_size=len(values))
        distribution.small_sample = len(values) < ReactionDistribution.MIN_MEANINGFUL_SAMPLE
        if not values:
            return distribution

        import statistics as stats
        distribution.mean = round(stats.fmean(values), 6)
        distribution.median = round(stats.median(values), 6)
        distribution.min_value = round(min(values), 6)
        distribution.max_value = round(max(values), 6)
        if len(values) >= 2:
            distribution.std_dev = round(stats.stdev(values), 6)
        distribution.p25 = calc.percentile(values, 25)
        distribution.p75 = calc.percentile(values, 75)

        for threshold in thresholds:
            key = f"+{threshold:.0%}"
            distribution.probability_above[key] = round(sum(1 for v in values if v >= threshold) / len(values), 4)
            distribution.probability_below[f"-{threshold:.0%}"] = round(
                sum(1 for v in values if v <= -threshold) / len(values), 4)
        return distribution

    def find_comparables(self, target: EventStudy, candidates: Sequence[EventStudy],
                          event_type_by_id: Optional[Dict[str, str]] = None,
                          max_results: int = 100) -> List[EventStudy]:
        """
        Find historical events comparable to the target (spec §22, §23).

        THE CRITICAL CONSTRAINT: only studies whose event PRECEDES the
        target's anchor are eligible. Selecting comparables from the
        future is look-ahead bias in its purest form — it would let a
        historical analysis be shaped by events that had not happened
        yet.

        Similarity uses structural features (event type, sector,
        regime), never headline text, because wording is the least
        reliable indicator of whether two events are alike.
        """
        anchor = target.market_visibility_latest
        if anchor is None:
            return []

        event_type_by_id = event_type_by_id or {}
        target_type = event_type_by_id.get(target.event_id)
        scored = []
        for candidate in candidates:
            if candidate.study_id == target.study_id:
                continue
            candidate_anchor = candidate.market_visibility_latest
            if candidate_anchor is None or candidate_anchor >= anchor:
                continue   # strictly historical only
            if not candidate.quality.is_usable:
                continue

            score = 0.0
            if target_type and event_type_by_id.get(candidate.event_id) == target_type:
                score += 0.5
            if (target.sector_context and candidate.sector_context
                    and target.sector_context.sector_id == candidate.sector_context.sector_id):
                score += 0.3
            if (target.market_regime and candidate.market_regime
                    and target.market_regime.trend == candidate.market_regime.trend):
                score += 0.2
            if score > 0:
                scored.append((score, candidate))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [candidate for _, candidate in scored[:max_results]]
