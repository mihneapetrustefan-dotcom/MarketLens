"""
src/domain/outcome_models.py
------------------------------------
What happened after a prediction or a signal. Facts only.

THE SEPARATION THIS FILE EXISTS TO ENFORCE (Phase 19 §2)
------------------------------------------------------------
A correct prediction is not a profitable trade, and a losing trade is
not a wrong prediction. Four different things get measured, and
collapsing them into one "performance" number destroys the ability to
say which layer failed:

    PREDICTION OUTCOME   was the number right?
    SIGNAL OUTCOME       was the claim right, at the stated horizon?
    EXECUTION OUTCOME    did we get the price the signal assumed?
    PORTFOLIO OUTCOME    did the position make money?

This phase builds the first two. The third and fourth need orders and
positions, and there are none — `order_intents`, `positions` and the
whole Phase 14 execution schema do not exist in the production
database. Inventing them here would produce a portfolio result wearing
a signal's clothes, which is the specific dishonesty §2 forbids.

MEASUREMENT IS NOT ATTRIBUTION
----------------------------------
Nothing here says *why* an outcome happened. `direction_result` says
HIT or MISS; it never says "the model was wrong" or "the timing was
off". Attribution is Phase 20 and it needs this record to exist first.
A field that guesses at cause would be an opinion stored as a fact, and
every later analysis would inherit it.

POINT-IN-TIME (§5, §27)
---------------------------
Outcome measurement is the ONE place in this repository allowed to read
prices dated after an information cutoff — that is what "what happened
next" means. The rule is one-directional:

    future prices  ->  outcome        ALLOWED, that is the job
    outcome        ->  prediction     FORBIDDEN
    outcome        ->  feature        FORBIDDEN
    outcome        ->  training set   FORBIDDEN without an explicit
                                      dataset version and cutoff

Nothing in this module writes to `research_features`, `predictions`,
`signals` or `trained_models`, and `tests/outcomes/test_leakage.py`
asserts that by reading the source.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

# ======================================================================
# Methodology version (§26)
# ======================================================================

#: Bump this when the MEANING of a measurement changes — a different
#: reference price rule, a different MFE definition, a different way of
#: resolving a horizon. It is part of the primary key, so a bump
#: produces NEW rows beside the old ones rather than rewriting history.
#:
#: A measurement whose method nobody can name is not reproducible, and
#: a research record that is not reproducible is an anecdote.
OUTCOME_METHOD_VERSION = "v1"


class OutcomeStatus(str, Enum):
    """
    Whether a measurement can be believed (§32).

    PENDING and INSUFFICIENT_DATA are deliberately distinct. "Not yet"
    and "never" call for opposite responses: one means come back later,
    the other means stop waiting. Collapsing them is how a permanently
    unmeasurable signal sits in a queue forever.
    """
    #: The window has not closed yet. Re-measure later (§33).
    PENDING = "pending"
    #: Measured. Every number on the row is real.
    AVAILABLE = "available"
    #: The window closed but the data cannot support a measurement —
    #: no reference price, no end price, a delisted instrument, a gap.
    #: NEVER silently a zero return (§24).
    INSUFFICIENT_DATA = "insufficient_data"
    #: The inputs were contradictory: a negative price, an end before a
    #: start, an impossible return. Flagged, not clamped (§56).
    INVALID = "invalid"
    #: A later methodology version measured the same subject.
    SUPERSEDED = "superseded"


class DirectionResult(str, Enum):
    """
    Whether the direction was right (§10).

    INSUFFICIENT_DATA is a member on purpose. §10 says do not let NULL
    silently become MISS — an unmeasured signal counted as a failure
    would understate every hit rate in the system, and the bias would
    grow with however many instruments lack price coverage.
    """
    HIT = "hit"
    MISS = "miss"
    NEUTRAL = "neutral"
    INSUFFICIENT_DATA = "insufficient_data"


class ReferencePriceRule(str, Enum):
    """
    Which price the forward return is measured FROM (§8).

    Stored on every row rather than assumed, because a return without a
    stated starting point is not reproducible.

    `FIRST_CLOSE_AT_OR_AFTER_CUTOFF` is the only rule implemented, and
    it is deliberately the conservative one: the first close the market
    printed at or after the moment the signal claimed to know
    something. Using the close BEFORE the cutoff would credit the
    signal with a move that had already happened by the time it spoke.

    An execution price is never a reference price here. Signal quality
    must not depend on whether anyone traded it (§8, §37).
    """
    FIRST_CLOSE_AT_OR_AFTER_CUTOFF = "first_close_at_or_after_cutoff"


class SubjectKind(str, Enum):
    """What is being measured. Predictions and signals are not the same claim."""
    PREDICTION = "prediction"
    SIGNAL = "signal"


# ======================================================================
# Horizons (§6)
# ======================================================================

_HORIZON_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)(m|h|d)$")

#: Units and their meaning. `d` is a TRADING day, not a calendar day —
#: see `OutcomeWindow` for why that distinction is load-bearing (§34).
_UNIT_SECONDS = {"m": 60.0, "h": 3600.0}


@dataclass(frozen=True)
class OutcomeWindow:
    """
    One horizon, resolved into an explicit interval (§6).

    Carries `horizon_value` and `horizon_unit` separately from the key
    so a consumer can sort or compare horizons arithmetically instead of
    parsing strings — "10d" sorts before "5d" as text, and a decay curve
    built on that ordering would be silently wrong.

    THE CALENDAR RULE (§34)
    ---------------------------
    A daily horizon is counted in BARS, not in calendar days. Five
    trading days after a Wednesday is the following Wednesday, and over
    a holiday week it is longer still. Daily candles only exist for
    sessions the market held, so counting bars respects the calendar
    exactly — without this project having to carry a holiday table it
    would then have to maintain per venue.

    Intraday horizons are wall-clock, resolved against intraday bars,
    which are likewise only present during sessions.
    """
    key: str
    horizon_value: float
    horizon_unit: str

    @property
    def is_intraday(self) -> bool:
        return self.horizon_unit in _UNIT_SECONDS

    @property
    def bars(self) -> Optional[int]:
        """Trading bars for a daily horizon; None for an intraday one."""
        return int(self.horizon_value) if self.horizon_unit == "d" else None

    @property
    def duration(self) -> Optional[timedelta]:
        """Wall-clock length for an intraday horizon; None for a daily one."""
        if not self.is_intraday:
            return None
        return timedelta(seconds=self.horizon_value * _UNIT_SECONDS[self.horizon_unit])

    #: Sortable magnitude, in seconds, using a nominal 6.5h session for
    #: daily horizons. FOR ORDERING ONLY — never for measurement, which
    #: is why it is not called `seconds`.
    @property
    def sort_key(self) -> float:
        if self.is_intraday:
            return self.horizon_value * _UNIT_SECONDS[self.horizon_unit]
        return self.horizon_value * 6.5 * 3600.0

    @classmethod
    def parse(cls, key: str) -> "OutcomeWindow":
        match = _HORIZON_PATTERN.match((key or "").strip().lower())
        if not match:
            raise ValueError(
                f"Unrecognised horizon {key!r}. Expected a number and a unit "
                f"of m, h or d — for example '15m', '4h', '5d'.")
        value = float(match.group(1))
        if value <= 0:
            raise ValueError(f"Horizon {key!r} must be positive.")
        unit = match.group(2)
        if unit == "d" and value != int(value):
            raise ValueError(
                f"Horizon {key!r}: a daily horizon counts trading bars, so it "
                f"must be a whole number of days.")
        return cls(key=match.group(1) + unit, horizon_value=value, horizon_unit=unit)


#: The default ladder (§6). Deliberately spans intraday to multi-day:
#: a signal layer that only ever measured 5d could not discover that its
#: edge decays within an hour, which is exactly what §14 asks.
DEFAULT_HORIZONS = ("15m", "1h", "4h", "1d", "3d", "5d", "10d")


def parse_horizons(keys) -> List[OutcomeWindow]:
    """Parse and de-duplicate, preserving the caller's order."""
    seen, out = set(), []
    for key in keys:
        window = OutcomeWindow.parse(key)
        if window.key in seen:
            continue
        seen.add(window.key)
        out.append(window)
    return out


# ======================================================================
# The measurement
# ======================================================================

@dataclass
class OutcomeMeasurement:
    """
    One subject, one horizon, one methodology version. A fact.

    Every field is either measured or None. There is no default that
    stands in for a missing measurement — a zero return where no price
    existed is the single most damaging thing this record could contain
    (§24), because it is indistinguishable from a real flat move and it
    biases every aggregate toward the middle.
    """
    subject_kind: SubjectKind
    subject_id: str
    horizon: OutcomeWindow
    method_version: str = OUTCOME_METHOD_VERSION

    status: OutcomeStatus = OutcomeStatus.PENDING

    # ---- the window, explicit (§6) ----
    information_cutoff: Optional[datetime] = None
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None

    # ---- prices, explicit (§7, §8) ----
    reference_price: Optional[float] = None
    reference_at: Optional[datetime] = None
    reference_rule: ReferencePriceRule = ReferencePriceRule.FIRST_CLOSE_AT_OR_AFTER_CUTOFF
    end_price: Optional[float] = None
    end_at: Optional[datetime] = None

    # ---- returns (§7) ----
    simple_return: Optional[float] = None
    log_return: Optional[float] = None

    # ---- excursions (§11, §12) ----
    #: Signed so that favourable is POSITIVE and adverse is NEGATIVE for
    #: both directions. A short whose price fell 3% has mfe = +0.03.
    #: Without this the two directions cannot be pooled in one average.
    mfe: Optional[float] = None
    mae: Optional[float] = None
    mfe_at: Optional[datetime] = None
    mae_at: Optional[datetime] = None
    time_to_mfe_seconds: Optional[float] = None
    time_to_mae_seconds: Optional[float] = None

    # ---- direction (§9, §10) ----
    expected_direction: str = ""
    realized_direction: Optional[str] = None
    direction_result: DirectionResult = DirectionResult.INSUFFICIENT_DATA

    # ---- expectation vs reality ----
    expected_return: Optional[float] = None
    error: Optional[float] = None
    absolute_error: Optional[float] = None

    # ---- provenance of the measurement itself (§26, §56) ----
    data_source: str = ""
    data_interval: str = ""
    bars_observed: int = 0
    data_as_of: Optional[datetime] = None

    # ---- context carried for slicing without a join (§16-§22) ----
    instrument_id: str = ""
    trained_model_id: Optional[str] = None
    model_status: Optional[str] = None
    strategy_id: Optional[str] = None
    market_regime: Optional[str] = None
    event_type: Optional[str] = None
    confidence: Optional[float] = None
    strength: Optional[float] = None
    signal_status: Optional[str] = None

    notes: List[str] = field(default_factory=list)
    computed_at: Optional[datetime] = None

    @property
    def identity(self) -> tuple:
        """
        The idempotency key (§30). Measuring the same subject twice with
        the same methodology must replace, never accumulate.
        """
        return (self.subject_kind.value, self.subject_id,
                self.horizon.key, self.method_version)

    @property
    def is_measured(self) -> bool:
        return self.status == OutcomeStatus.AVAILABLE

    def as_dict(self) -> Dict[str, Any]:
        def iso(value):
            return value.isoformat() if value else None
        return {
            "subject_kind": self.subject_kind.value,
            "subject_id": self.subject_id,
            "horizon": self.horizon.key,
            "method_version": self.method_version,
            "status": self.status.value,
            "simple_return": self.simple_return,
            "log_return": self.log_return,
            "mfe": self.mfe, "mae": self.mae,
            "direction_result": self.direction_result.value,
            "reference_price": self.reference_price,
            "end_price": self.end_price,
            "window_end": iso(self.window_end),
            "bars_observed": self.bars_observed,
        }


# ======================================================================
# Pure calculations — no database, no clock
# ======================================================================

#: Below this absolute move a realized return is treated as flat rather
#: than as a direction. Without a dead band, a 0.0001% drift decides a
#: HIT, and hit rate becomes a measure of floating-point noise.
#:
#: 10 basis points is under a typical single-name bid-ask spread, so a
#: move smaller than this was not tradeable in either direction.
NEUTRAL_BAND = 0.001


def simple_return(reference_price: Optional[float],
                  end_price: Optional[float]) -> Optional[float]:
    """
    (end / reference) - 1.

    None on any missing or non-positive price. A non-positive price is
    not a cheap stock; it is bad data, and dividing by it produces a
    number that looks like a return (§56).
    """
    if reference_price is None or end_price is None:
        return None
    if reference_price <= 0 or end_price <= 0:
        return None
    return (end_price / reference_price) - 1.0


def log_return(reference_price: Optional[float],
               end_price: Optional[float]) -> Optional[float]:
    """
    ln(end / reference).

    Offered alongside the simple return rather than instead of it: log
    returns add across time, simple returns add across a portfolio, and
    which one is correct depends on the question. Storing both costs one
    column and removes a whole class of quiet error.
    """
    if reference_price is None or end_price is None:
        return None
    if reference_price <= 0 or end_price <= 0:
        return None
    return math.log(end_price / reference_price)


def realized_direction(return_value: Optional[float],
                       band: float = NEUTRAL_BAND) -> Optional[str]:
    """What the market actually did: 'long', 'short' or 'neutral'."""
    if return_value is None:
        return None
    if abs(return_value) < band:
        return "neutral"
    return "long" if return_value > 0 else "short"


def classify_direction(expected: str, return_value: Optional[float],
                       band: float = NEUTRAL_BAND) -> DirectionResult:
    """
    Compare the claim with what happened (§9, §10).

    NEUTRAL SEMANTICS, stated explicitly because §9 asks for them:

      * A directional signal (long/short) whose realized move is inside
        the dead band is **NEUTRAL, not MISS**. The market did not move
        enough to say the claim was wrong; recording a miss would punish
        a signal for an absence of evidence.

      * A NEUTRAL signal is an active claim that nothing much will
        happen. It is a **HIT** when the move stays inside the band and
        a **MISS** when it does not. Direction is irrelevant to it —
        a neutral call broken upward is as wrong as one broken down.

      * `no_signal` is not a claim at all and is never scored. Counting
        abstentions as hits or misses would make abstaining a strategy
        for improving one's hit rate.
    """
    if return_value is None:
        return DirectionResult.INSUFFICIENT_DATA

    expected = (expected or "").strip().lower()
    inside_band = abs(return_value) < band

    if expected in ("neutral", "flat"):
        return DirectionResult.HIT if inside_band else DirectionResult.MISS
    if expected in ("no_signal", "none", ""):
        return DirectionResult.INSUFFICIENT_DATA
    if expected not in ("long", "short"):
        return DirectionResult.INSUFFICIENT_DATA

    if inside_band:
        return DirectionResult.NEUTRAL
    moved_up = return_value > 0
    wanted_up = expected == "long"
    return DirectionResult.HIT if moved_up == wanted_up else DirectionResult.MISS


def excursions(direction: str, reference_price: float,
               highs: List[float], lows: List[float]) -> Dict[str, Optional[float]]:
    """
    Maximum favourable and adverse excursion (§11, §12).

    Signed so favourable is positive and adverse is negative FOR BOTH
    DIRECTIONS, which is what lets long and short measurements be pooled
    into one distribution:

        long   mfe = max(high)/ref - 1      mae = min(low)/ref - 1
        short  mfe = 1 - min(low)/ref       mae = 1 - max(high)/ref

    Returns indices too, so the caller can attach timestamps without
    re-deriving which bar produced the extreme.

    MFE and MAE bound the realized return on either side by
    construction, and the pipeline asserts that rather than trusting it.
    """
    result: Dict[str, Optional[float]] = {
        "mfe": None, "mae": None, "mfe_index": None, "mae_index": None}
    if reference_price is None or reference_price <= 0:
        return result
    if not highs or not lows or len(highs) != len(lows):
        return result

    direction = (direction or "").strip().lower()
    if direction not in ("long", "short"):
        return result

    high_index = max(range(len(highs)), key=lambda i: highs[i])
    low_index = min(range(len(lows)), key=lambda i: lows[i])
    highest, lowest = highs[high_index], lows[low_index]
    if highest <= 0 or lowest <= 0:
        return result

    if direction == "long":
        result["mfe"] = (highest / reference_price) - 1.0
        result["mae"] = (lowest / reference_price) - 1.0
        result["mfe_index"], result["mae_index"] = high_index, low_index
    else:
        result["mfe"] = 1.0 - (lowest / reference_price)
        result["mae"] = 1.0 - (highest / reference_price)
        result["mfe_index"], result["mae_index"] = low_index, high_index
    return result


def time_to_threshold(reference_price: float, direction: str,
                      bars: List[Dict[str, Any]],
                      threshold: float) -> Optional[float]:
    """
    Seconds until the favourable move first reached `threshold` (§13).

    Uses each bar's favourable extreme, so it answers "when was this
    first reachable" rather than "when did it close there". None when
    the threshold was never reached — never the window length, which
    would silently claim it was reached at the end.
    """
    if reference_price is None or reference_price <= 0 or not bars:
        return None
    direction = (direction or "").strip().lower()
    if direction not in ("long", "short"):
        return None
    start = bars[0].get("timestamp")
    if start is None:
        return None
    for bar in bars:
        if direction == "long":
            move = (bar.get("high", 0.0) / reference_price) - 1.0
        else:
            move = 1.0 - (bar.get("low", 0.0) / reference_price)
        if move >= threshold:
            moment = bar.get("timestamp")
            return (moment - start).total_seconds() if moment else None
    return None
