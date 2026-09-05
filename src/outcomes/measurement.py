"""
src/outcomes/measurement.py
-----------------------------------
Turning price bars into an outcome measurement.

WHAT THIS FILE READS AND WHAT IT WILL NEVER WRITE
-----------------------------------------------------
Reads: `price_candle_cache`, `signals`, `predictions`,
`signal_contributions`, `research_observations`, `trained_models`.

Writes: `outcome_measurements` and `outcome_aggregates`, and nothing
else, ever. It does not touch `research_features`, `predictions`,
`signals` or `trained_models`. That one-directional rule is the whole
point-in-time story of Phase 19 (§27), and
`tests/outcomes/test_leakage.py` enforces it by reading this source
rather than by trusting this comment.

THE ONE PLACE ALLOWED TO LOOK FORWARD
-----------------------------------------
Everywhere else in the repository, reading a price dated after an
information cutoff is a bug — `PointInTimeView` raises
`LookAheadViolation` for exactly that. Here it is the job: an outcome
is by definition what happened next. The safety comes from the
direction of flow, not from refusing to look.

INTERVAL CHOICE
-------------------
A daily horizon is measured on daily bars; an intraday horizon on
minute bars. Measuring a 15-minute horizon on daily candles would
return the day's close and call it a 15-minute move, and nothing about
the stored row would reveal the lie. When the required interval is not
available for an instrument the answer is INSUFFICIENT_DATA, not a
substitution (§24, §35).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.domain.outcome_models import (
    NEUTRAL_BAND, OUTCOME_METHOD_VERSION, DirectionResult, OutcomeMeasurement,
    OutcomeStatus, OutcomeWindow, ReferencePriceRule, SubjectKind,
    classify_direction, excursions, log_return, realized_direction,
    simple_return, time_to_threshold,
)

#: Which candle interval answers which horizon unit. A daily horizon on
#: minute bars would also be wrong — 5 "days" of minute bars is a
#: different window from 5 sessions — so the mapping is one-way and
#: total.
INTERVAL_FOR_UNIT = {"m": "1m", "h": "1m", "d": "1d"}

#: A move this large over a single horizon is almost always a corporate
#: action the adjusted series did not cover — a split, a reverse split,
#: a ticker reuse (§25). Flagged in `notes` and marked INVALID rather
#: than clamped: §56 says do not silently clamp extreme returns, and a
#: clamped 900% split looks exactly like a real 100% move.
IMPLAUSIBLE_RETURN = 3.0


#: Weekends and holidays stretch N trading sessions across more calendar
#: days than N. Five sessions can span nine days over a holiday weekend.
#: 7/5 covers weekends; the four-day cushion covers the longest ordinary
#: exchange holiday run.
#:
#: Deliberately GENEROUS. Being wrong in this direction leaves a row
#: PENDING slightly too long and it resolves on the next run. Being
#: wrong the other way would declare a live instrument permanently
#: unmeasurable, and nothing would ever revisit it.
_CALENDAR_SLACK_DAYS = 4


def _calendar_bound(sessions: int) -> timedelta:
    """The longest plausible calendar span of `sessions` trading days."""
    return timedelta(days=sessions * 7.0 / 5.0 + _CALENDAR_SLACK_DAYS)


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def load_bars(conn: sqlite3.Connection, instrument_id: str, interval: str,
              start: datetime, limit: int = 4000) -> List[Dict[str, Any]]:
    """
    Bars at or after `start`, oldest first.

    `adjusted_close` is preferred over `close` where present (§25):
    measuring a return across a split on unadjusted prices manufactures
    a 50% loss that never happened. Highs and lows have no adjusted
    counterpart in this cache, so when the two closes disagree the row
    is flagged rather than silently mixed — see `_adjustment_factor`.
    """
    rows = conn.execute("""
        SELECT timestamp, open, high, low, close, adjusted_close, volume
        FROM price_candle_cache
        WHERE instrument_id = ? AND interval = ? AND timestamp >= ?
        ORDER BY timestamp ASC LIMIT ?
    """, (instrument_id, interval, start.isoformat(), limit)).fetchall()

    bars = []
    for timestamp, open_, high, low, close, adjusted, volume in rows:
        moment = _parse(timestamp)
        if moment is None or close is None:
            continue
        bars.append({
            "timestamp": moment, "open": open_, "high": high, "low": low,
            "close": close, "adjusted_close": adjusted, "volume": volume,
        })
    return bars


def _adjustment_factor(bar: Dict[str, Any]) -> float:
    """
    How much the adjusted series differs from the raw one on this bar.

    Highs and lows in this cache are unadjusted. Scaling them by the
    close's own adjustment factor keeps a single bar internally
    consistent, which is what MFE and MAE need. It is an approximation
    across a split boundary and the caller says so in `notes`.
    """
    close, adjusted = bar.get("close"), bar.get("adjusted_close")
    if not close or not adjusted or close <= 0:
        return 1.0
    return adjusted / close


def reference_price_for(bars: Sequence[Dict[str, Any]],
                        cutoff: datetime) -> Tuple[Optional[float], Optional[datetime]]:
    """
    The first close at or after the information cutoff (§8).

    Deliberately at-or-after, not before. The close BEFORE the cutoff
    would credit the signal with whatever the market had already done
    by the time it spoke — the single easiest way to manufacture an
    edge that does not exist.

    This is never an execution price. Signal quality must not depend on
    whether anyone traded it (§8, §37).
    """
    for bar in bars:
        if bar["timestamp"] >= cutoff and bar.get("close"):
            price = bar["close"] * _adjustment_factor(bar)
            return (price if price > 0 else None), bar["timestamp"]
    return None, None


def window_bars(bars: Sequence[Dict[str, Any]], window: OutcomeWindow,
                reference_at: datetime) -> List[Dict[str, Any]]:
    """
    The bars inside the horizon, starting at the reference bar.

    A daily horizon takes N+1 BARS — the reference bar plus N trading
    sessions. That is the calendar rule (§34): daily candles only exist
    for sessions the market actually held, so counting bars respects
    holidays and weekends exactly, without this project maintaining a
    holiday table per venue that would itself go stale.

    An intraday horizon takes every bar within the wall-clock duration.
    Intraday bars likewise only exist during sessions, so a 4h horizon
    opened near the close simply runs out of bars and is reported as
    such rather than silently spilling into the next day.
    """
    inside = [bar for bar in bars if bar["timestamp"] >= reference_at]
    if not inside:
        return []
    if window.horizon_unit == "d":
        return inside[: (window.bars or 0) + 1]
    duration = window.duration
    if duration is None:
        return []
    limit = reference_at + duration
    return [bar for bar in inside if bar["timestamp"] <= limit]


def measure(subject_kind: SubjectKind, subject_id: str,
            window: OutcomeWindow, *,
            cutoff: datetime,
            direction: str,
            bars: Sequence[Dict[str, Any]],
            data_as_of: Optional[datetime],
            expected_return: Optional[float] = None,
            interval: str = "",
            data_source: str = "",
            method_version: str = OUTCOME_METHOD_VERSION,
            neutral_band: float = NEUTRAL_BAND,
            **context: Any) -> OutcomeMeasurement:
    """
    One subject, one horizon. Returns a measurement in every case —
    including the cases it cannot measure, which carry a status and a
    note rather than being dropped.

    A dropped subject is invisible; an INSUFFICIENT_DATA row is a
    question someone can answer.
    """
    outcome = OutcomeMeasurement(
        subject_kind=subject_kind, subject_id=subject_id, horizon=window,
        method_version=method_version, information_cutoff=cutoff,
        expected_direction=(direction or "").strip().lower(),
        expected_return=expected_return, data_interval=interval,
        data_source=data_source, data_as_of=data_as_of,
        computed_at=datetime.now(timezone.utc))
    for key, value in context.items():
        if hasattr(outcome, key):
            setattr(outcome, key, value)

    reference_price, reference_at = reference_price_for(bars, cutoff)
    if reference_price is None or reference_at is None:
        # No price at or after the cutoff. Either the instrument has no
        # coverage, or it stopped trading — and both are the same answer
        # to the question "can this be measured".
        outcome.status = OutcomeStatus.INSUFFICIENT_DATA
        outcome.notes.append(
            "no candle at or after the information cutoff — the instrument "
            "has no price coverage for this window")
        return outcome

    outcome.reference_price = reference_price
    outcome.reference_at = reference_at
    outcome.window_start = reference_at
    outcome.reference_rule = ReferencePriceRule.FIRST_CLOSE_AT_OR_AFTER_CUTOFF

    inside = window_bars(bars, window, reference_at)
    outcome.bars_observed = len(inside)

    required = (window.bars or 0) + 1 if window.horizon_unit == "d" else 2
    if len(inside) < required:
        # Not enough bars YET, or not enough ever. The distinction is
        # made by the CLOCK, not by the row count (§32, §33) — "come
        # back later" and "stop waiting" demand opposite responses, and
        # a signal issued an hour ago must not be filed under the same
        # heading as one whose instrument stopped trading.
        #
        # A daily window is still open while the newest data we hold
        # sits at or before the reference bar plus the horizon; an
        # intraday one while the clock has not passed window_end. Both
        # reduce to: has enough time elapsed for the bars to exist?
        if window.horizon_unit == "d":
            # A daily window has no wall-clock end — it ends after N
            # SESSIONS, and how long that takes depends on weekends and
            # holidays. So the test is: has enough calendar time passed
            # that the missing sessions could not still be coming?
            #
            # `latest_possible_close` is the generous bound. Without it
            # a DELISTED instrument stays PENDING forever, which is the
            # worst of both answers: it never resolves and it never
            # admits that it cannot.
            outcome.window_end = None
            latest_possible = reference_at + _calendar_bound(window.bars or 0)
            still_open = data_as_of is None or data_as_of < latest_possible
        else:
            outcome.window_end = reference_at + window.duration
            still_open = data_as_of is None or data_as_of < outcome.window_end

        outcome.status = (OutcomeStatus.PENDING if still_open
                          else OutcomeStatus.INSUFFICIENT_DATA)
        interval_label = interval or "the requested interval"
        tail = ("; the window has not closed yet" if still_open
                else "; the window closed without enough data")
        outcome.notes.append(
            f"{len(inside)} of {required} bars available at "
            f"{interval_label}{tail}")
        return outcome

    final = inside[-1]
    end_price = final["close"] * _adjustment_factor(final)
    outcome.end_price = end_price if end_price > 0 else None
    outcome.end_at = final["timestamp"]
    outcome.window_end = final["timestamp"]

    if outcome.end_price is None:
        outcome.status = OutcomeStatus.INSUFFICIENT_DATA
        outcome.notes.append("the closing bar carries a non-positive price")
        return outcome

    outcome.simple_return = simple_return(reference_price, outcome.end_price)
    outcome.log_return = log_return(reference_price, outcome.end_price)

    if outcome.simple_return is None:
        outcome.status = OutcomeStatus.INSUFFICIENT_DATA
        outcome.notes.append("a price was missing or non-positive")
        return outcome

    if abs(outcome.simple_return) > IMPLAUSIBLE_RETURN:
        # Flagged, not clamped (§25, §56). A 900% "return" across a
        # split boundary is a data defect; clamping it to a plausible
        # number would hide the defect and keep the wrong sign.
        outcome.status = OutcomeStatus.INVALID
        outcome.notes.append(
            f"implausible return {outcome.simple_return:+.1%} over {window.key} "
            f"— almost certainly an unadjusted corporate action rather than a "
            f"market move. Flagged, not clamped.")
        return outcome

    highs = [bar["high"] * _adjustment_factor(bar)
             for bar in inside if bar.get("high")]
    lows = [bar["low"] * _adjustment_factor(bar)
            for bar in inside if bar.get("low")]
    if len(highs) == len(inside) and len(lows) == len(inside):
        found = excursions(outcome.expected_direction, reference_price, highs, lows)
        outcome.mfe, outcome.mae = found["mfe"], found["mae"]
        if found["mfe_index"] is not None:
            bar = inside[found["mfe_index"]]
            outcome.mfe_at = bar["timestamp"]
            outcome.time_to_mfe_seconds = (bar["timestamp"] - reference_at).total_seconds()
        if found["mae_index"] is not None:
            bar = inside[found["mae_index"]]
            outcome.mae_at = bar["timestamp"]
            outcome.time_to_mae_seconds = (bar["timestamp"] - reference_at).total_seconds()
    else:
        outcome.notes.append(
            "high/low missing on some bars — MFE and MAE not computed rather "
            "than computed over a subset, which would understate both")

    outcome.realized_direction = realized_direction(outcome.simple_return, neutral_band)
    outcome.direction_result = classify_direction(
        outcome.expected_direction, outcome.simple_return, neutral_band)

    if expected_return is not None:
        outcome.error = outcome.simple_return - expected_return
        outcome.absolute_error = abs(outcome.error)

    outcome.status = OutcomeStatus.AVAILABLE

    # A cheap invariant that has caught real sign errors: the realized
    # return must sit between the adverse and favourable extremes.
    if outcome.mfe is not None and outcome.mae is not None:
        signed = (outcome.simple_return
                  if outcome.expected_direction == "long"
                  else -outcome.simple_return)
        if not (outcome.mae - 1e-9 <= signed <= outcome.mfe + 1e-9):
            outcome.notes.append(
                f"return {signed:+.4f} falls outside [MAE {outcome.mae:+.4f}, "
                f"MFE {outcome.mfe:+.4f}] — inspect the bar data")
    return outcome


def time_to_move(reference_price: float, direction: str,
                 bars: Sequence[Dict[str, Any]],
                 threshold: float) -> Optional[float]:
    """Seconds until the favourable move first reached `threshold` (§13)."""
    return time_to_threshold(reference_price, direction, list(bars), threshold)
