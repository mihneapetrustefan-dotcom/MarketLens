"""
src/domain/memory_models.py
-----------------------------------
Structured experience: what happened, under what conditions, and how
strong the evidence was.

WHAT TRADING MEMORY IS NOT
------------------------------
Not a vector dump, not a list of generated lessons, not chat history.
An experience is a join, not a paraphrase:

    EXPERIENCE = CONTEXT + DECISION + EXPECTATION + OUTCOME
               + ERROR ATTRIBUTION + EVIDENCE

Every field either references a canonical record from Phases 19 and 20
or snapshots a value that was true at decision time. Nothing is
invented, and nothing is summarised into prose that cannot be traced
back to a row.

THE ONE IDEA THIS FILE EXISTS FOR
-------------------------------------
`available_at` — the moment an experience became KNOWABLE, which is
when its outcome window closed, not when we happened to compute it.

Without that distinction point-in-time memory is decorative. If
experiences were dated by `computed_at`, every one of them would appear
at the same instant — today — and `memory_as_of(2026-08-20)` would
return the whole record including outcomes that had not happened yet.
The leakage would be invisible and total.

With it, a signal issued on 5 August with a 10-day horizon enters
memory around 19 August and not before, and a historical study anchored
at 12 August cannot see it. That is the property Phase 22 and every
later learning phase depend on, and it is checked by tests that ask for
memory at a past date and assert the future is absent.

CORRELATION IS NOT CAUSATION
--------------------------------
A pattern here says "these conditions co-occurred with these outcomes,
this many times, over this window". It never says one caused the other,
and it never says the next occurrence will match. `PatternQuality` has
members for weak, conflicting and stale precisely so that the
uncomfortable answers stay available.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ======================================================================
# Versioning (§35)
# ======================================================================

#: Bump when the MEANING of an experience or a pattern changes. Part of
#: every primary key, so a change writes new rows beside the old ones.
MEMORY_METHOD_VERSION = "v1"

#: Bump when the decision-time CONTEXT captured changes shape (§9).
#: Kept separate from the memory version because context definitions and
#: aggregation rules change for different reasons and at different
#: times, and one version number covering both would make "why did this
#: memory change" unanswerable.
CONTEXT_SCHEMA_VERSION = "ctx-v1"


class ExperienceKind(str, Enum):
    """
    What the experience is about (§7).

    One canonical table holds all of them, distinguished by this field.
    Separate tables per kind would duplicate the context, expectation
    and outcome columns eight times over, and the eight copies would
    drift.
    """
    PREDICTION = "prediction"
    SIGNAL = "signal"
    TRADE = "trade"
    EXECUTION = "execution"
    RISK = "risk"
    REGIME = "regime"
    EVENT = "event"
    PORTFOLIO = "portfolio"


class ExperienceClass(str, Enum):
    """
    How it turned out (§13).

    Deliberately NOT profit-based. §13 is explicit: a profitable result
    can still be a weak or lucky decision, so the classification is
    driven by the Phase 20 attribution — whether the reasoning held up —
    with the realised return only distinguishing expected from
    unexpected.

    EXPECTED_LOSS and EXPECTED_WIN are the important members. A loss
    inside the cohort's usual range is not a failure to learn from, and
    a win outside it is not a validation.
    """
    SUCCESSFUL = "successful"
    UNSUCCESSFUL = "unsuccessful"
    MIXED = "mixed"
    EXPECTED_LOSS = "expected_loss"
    EXPECTED_WIN = "expected_win"
    UNEXPECTED_LOSS = "unexpected_loss"
    UNEXPECTED_WIN = "unexpected_win"
    NO_CLEAR_RESULT = "no_clear_result"


class ExperienceQuality(str, Enum):
    """
    How much weight this experience can bear (§6, §33).

    VALIDATED versus EXPERIMENTAL is the Phase 18 distinction carried
    forward. Experimental experience is kept — §6 says do not discard
    useful experimental data — but it is never silently pooled with
    production experience, because a pattern built from unpromoted-model
    output would describe research, not the system.
    """
    #: Complete provenance, a measured outcome, an attributed cause.
    VALIDATED = "validated"
    #: The same, but produced by a model no human promoted.
    EXPERIMENTAL = "experimental"
    #: Missing an outcome, an attribution, or a timestamp.
    INCOMPLETE = "incomplete"
    #: Contradicted by a later correction record.
    SUPERSEDED = "superseded"


class PatternQuality(str, Enum):
    """
    What a pattern is worth (§33).

    CONFLICTING and UNSTABLE exist so the uncomfortable answers stay
    sayable. A pattern that worked in one sub-period and failed in
    another is not a good pattern with noise; it is an unstable one, and
    §29 requires that distinction be preserved rather than averaged
    away.
    """
    CONFIRMED = "confirmed"
    WEAK = "weak"
    CONFLICTING = "conflicting"
    UNSTABLE = "unstable"
    STALE = "stale"
    SUPERSEDED = "superseded"
    REQUIRES_REVIEW = "requires_review"


class MemoryConfidence(str, Enum):
    """
    How much the evidence supports a pattern (§26).

    Kept deliberately separate from model confidence and signal
    confidence (§27) — three different things that would be disastrous
    to conflate. Model confidence is about a prediction, signal
    confidence is a heuristic trust score, and this is about whether a
    historical regularity is real.

    Ordinal, never a probability. Nothing has checked how often a
    pattern holds on data it was not built from.
    """
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CONFLICTING_EVIDENCE = "conflicting_evidence"

    @property
    def rank(self) -> int:
        return {"high": 4, "medium": 3, "low": 2,
                "conflicting_evidence": 1, "insufficient_evidence": 0}[self.value]


# ======================================================================
# Thresholds — reused, not reinvented
# ======================================================================

#: Reached through Phases 19 and 20 rather than redefined, so one number
#: governs "too small to mean anything" across four phases.
from src.domain.model_models import ModelEvaluation  # noqa: E402
MIN_PATTERN_SAMPLE = ModelEvaluation.MIN_EFFECTIVE_SAMPLE

#: A pattern needs this many sub-periods with data before stability can
#: be assessed at all. Two periods is a comparison; one is an anecdote
#: with a date attached.
MIN_STABILITY_PERIODS = 2

#: Sub-periods disagree when their hit rates straddle the coin flip by
#: more than this. Not a significance test — a stated, arbitrary line
#: that makes "unstable" reproducible rather than a matter of opinion.
STABILITY_DIVERGENCE = 0.15

#: Beyond this, a pattern's evidence is old enough that a consumer
#: should be told. NOT deleted (§49): historical existence and current
#: relevance are different things, and the consumer chooses the policy.
STALENESS_DAYS = 90


@dataclass
class ExperienceContext:
    """
    The decision-time snapshot (§8).

    Every field here was knowable BEFORE the outcome. Nothing is
    reconstructed from what happened next — that is the whole point, and
    `tests/memory/test_leakage.py` checks the field list against the
    outcome columns to make sure no result sneaks in as context.
    """
    schema_version: str = CONTEXT_SCHEMA_VERSION

    instrument_id: str = ""
    asset_class: Optional[str] = None
    sector_id: Optional[str] = None
    event_type: Optional[str] = None
    event_id: Optional[str] = None
    market_regime: Optional[str] = None
    volatility_percentile: Optional[float] = None
    relative_volume: Optional[float] = None
    data_quality: Optional[str] = None
    signal_type: Optional[str] = None
    strategy_id: Optional[str] = None
    strategy_version: Optional[str] = None
    horizon: str = ""
    information_cutoff: Optional[datetime] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "instrument_id": self.instrument_id,
            "asset_class": self.asset_class, "sector_id": self.sector_id,
            "event_type": self.event_type, "event_id": self.event_id,
            "market_regime": self.market_regime,
            "volatility_percentile": self.volatility_percentile,
            "relative_volume": self.relative_volume,
            "data_quality": self.data_quality,
            "signal_type": self.signal_type, "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "horizon": self.horizon,
            "information_cutoff": (self.information_cutoff.isoformat()
                                   if self.information_cutoff else None),
        }


@dataclass
class TradingExperience:
    """
    One thing the system went through, joined end to end.

    References rather than copies wherever a canonical record exists
    (§3, §52): the outcome and attribution keys point at Phases 19 and
    20. The handful of values duplicated here — direction, returns,
    excursions — are what a memory query filters and aggregates on, and
    joining three tables for every retrieval would make point-in-time
    search unusable.
    """
    experience_id: str
    kind: ExperienceKind
    memory_version: str = MEMORY_METHOD_VERSION

    # ---- provenance: the canonical records this rests on (§5) -------
    subject_kind: str = ""
    subject_id: str = ""
    horizon: str = ""
    outcome_method_version: str = ""
    attribution_method_version: str = ""
    trained_model_id: Optional[str] = None
    model_status: Optional[str] = None
    strategy_id: Optional[str] = None
    observation_id: Optional[str] = None

    # ---- when it happened, and when it became knowable --------------
    information_cutoff: Optional[datetime] = None
    #: THE point-in-time key. When the outcome window closed — the
    #: moment this experience could first have been known. Never
    #: `computed_at`: dating memory by when we processed it would make
    #: every experience appear at once and destroy the property.
    available_at: Optional[datetime] = None

    # ---- what was expected (§10) ------------------------------------
    expected_direction: str = ""
    expected_return: Optional[float] = None
    expected_horizon: str = ""
    signal_confidence: Optional[float] = None
    signal_strength: Optional[float] = None

    # ---- what happened (§11) ----------------------------------------
    actual_return: Optional[float] = None
    actual_direction: Optional[str] = None
    direction_result: Optional[str] = None
    mfe: Optional[float] = None
    mae: Optional[float] = None
    time_to_mfe_seconds: Optional[float] = None

    # ---- why (§12) ---------------------------------------------------
    primary_error: Optional[str] = None
    contributing_errors: List[str] = field(default_factory=list)
    attribution_confidence: Optional[str] = None
    attribution_severity: Optional[str] = None
    evidence_count: int = 0

    context: ExperienceContext = field(default_factory=ExperienceContext)
    experience_class: ExperienceClass = ExperienceClass.NO_CLEAR_RESULT
    quality: ExperienceQuality = ExperienceQuality.INCOMPLETE
    notes: List[str] = field(default_factory=list)
    created_at: Optional[datetime] = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc)

    @property
    def identity(self) -> tuple:
        """Deterministic (§54): the same subject and horizon under the
        same memory version is one experience, however often it is
        rebuilt."""
        return (self.subject_kind, self.subject_id, self.horizon,
                self.memory_version)

    @property
    def is_usable(self) -> bool:
        return self.quality in (ExperienceQuality.VALIDATED,
                                ExperienceQuality.EXPERIMENTAL)

    def age_days(self, now: Optional[datetime] = None) -> Optional[float]:
        """How old the evidence is (§28). Reported, never used to delete."""
        if self.available_at is None:
            return None
        now = now or datetime.now(timezone.utc)
        return (now - self.available_at).total_seconds() / 86400.0


def experience_id_for(subject_kind: str, subject_id: str, horizon: str,
                      memory_version: str) -> str:
    """
    A deterministic id (§54).

    Derived from the natural key rather than a UUID, so a rebuild
    produces the same ids and a re-run cannot duplicate an experience
    even if the identity check is bypassed.
    """
    digest = hashlib.sha256("|".join([
        subject_kind, subject_id, horizon, memory_version]).encode())
    return f"exp-{digest.hexdigest()[:24]}"


def classify_experience(*, direction_result: Optional[str],
                        primary_error: Optional[str],
                        actual_return: Optional[float],
                        unexpected: Optional[bool]) -> ExperienceClass:
    """
    How an experience is classified (§13).

    Driven by the ATTRIBUTION, not by profit. §13 is explicit that a
    profitable result can be a weak or lucky decision, so the question
    asked is "did the reasoning hold up", and the return only separates
    expected from unexpected.

    `unexpected` comes from Phase 20's cohort percentile judgement and
    is None when the cohort was too small to say — in which case this
    declines to call anything unexpected rather than guessing.
    """
    if primary_error in (None, "") or direction_result is None:
        return ExperienceClass.NO_CLEAR_RESULT
    if primary_error == "unknown":
        return ExperienceClass.NO_CLEAR_RESULT

    if primary_error == "expected_loss":
        return ExperienceClass.EXPECTED_LOSS

    if primary_error == "no_error":
        if actual_return is None:
            return ExperienceClass.NO_CLEAR_RESULT
        if unexpected and actual_return > 0:
            return ExperienceClass.UNEXPECTED_WIN
        if actual_return > 0:
            return ExperienceClass.EXPECTED_WIN
        # A loss with no error found and no cohort judgement available.
        return ExperienceClass.EXPECTED_LOSS

    # An error was attributed. Whether it also lost money is a separate
    # question, and both halves are worth keeping distinct.
    if actual_return is not None and actual_return > 0:
        # Right outcome, faulty reasoning. §13's exact warning.
        return ExperienceClass.MIXED
    if unexpected:
        return ExperienceClass.UNEXPECTED_LOSS
    return ExperienceClass.UNSUCCESSFUL


# ======================================================================
# Patterns (§23, §24)
# ======================================================================

@dataclass
class PatternPeriod:
    """One sub-period of a pattern's history, for stability (§29)."""
    label: str
    sample_size: int = 0
    hit_rate: Optional[float] = None
    mean_return: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"label": self.label, "sample_size": self.sample_size,
                "hit_rate": self.hit_rate, "mean_return": self.mean_return}


@dataclass
class MemoryPattern:
    """
    A recurring combination of conditions, and what happened under it.

    A pattern is an OBSERVATION about co-occurrence. It is not a rule,
    not a prediction, and not a claim of causation — §0 forbids all
    three, and the wording of `describe()` is written to keep the
    distinction visible in whatever surface renders it.
    """
    pattern_id: str
    pattern_type: str
    conditions: Dict[str, Any] = field(default_factory=dict)
    memory_version: str = MEMORY_METHOD_VERSION
    context_schema_version: str = CONTEXT_SCHEMA_VERSION

    sample_size: int = 0
    instrument_count: int = 0
    experiment_count: int = 0

    hits: int = 0
    misses: int = 0
    neutrals: int = 0
    hit_rate: Optional[float] = None

    mean_return: Optional[float] = None
    median_return: Optional[float] = None
    stdev_return: Optional[float] = None
    mean_mfe: Optional[float] = None
    mean_mae: Optional[float] = None

    error_counts: Dict[str, int] = field(default_factory=dict)
    class_counts: Dict[str, int] = field(default_factory=dict)

    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    periods: List[PatternPeriod] = field(default_factory=list)

    quality: PatternQuality = PatternQuality.WEAK
    confidence: MemoryConfidence = MemoryConfidence.INSUFFICIENT_EVIDENCE
    stability: str = "unknown"
    regime_breakdown: Dict[str, Any] = field(default_factory=dict)
    contradictions: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    created_at: Optional[datetime] = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc)

    @property
    def decided(self) -> int:
        """
        Hits plus misses.

        Neutrals are excluded for the reason Phase 19 gave: a market
        that did not move is not evidence for or against a directional
        claim, and counting it would make a hit rate a measure of
        volatility.
        """
        return self.hits + self.misses

    def describe(self) -> str:
        """
        The finding, in language that cannot be mistaken for a rule.

        "co-occurred with", never "causes". "historically", never "will".
        Below the sample threshold it refuses to give a rate at all,
        because a rate reads as a finding and eleven observations are
        not one.
        """
        conditions = ", ".join(f"{k}={v}" for k, v in
                               sorted(self.conditions.items()) if v is not None)
        if self.sample_size < MIN_PATTERN_SAMPLE:
            return (f"{conditions}: only {self.sample_size} experience(s) — "
                    f"too few to describe a regularity")
        if self.quality == PatternQuality.CONFLICTING:
            return (f"{conditions}: {self.sample_size} experiences, but the "
                    f"evidence conflicts across sub-periods or regimes — no "
                    f"single rate describes it")
        if self.hit_rate is None:
            return (f"{conditions}: {self.sample_size} experiences, none of "
                    f"which resolved directionally")
        return (f"{conditions}: over {self.sample_size} historical "
                f"experience(s), a {self.hit_rate:.0%} directional hit rate "
                f"co-occurred with these conditions. This describes what "
                f"happened, not what will happen, and not a cause.")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "pattern_id": self.pattern_id, "pattern_type": self.pattern_type,
            "conditions": self.conditions, "sample_size": self.sample_size,
            "hit_rate": self.hit_rate, "mean_return": self.mean_return,
            "quality": self.quality.value, "confidence": self.confidence.value,
            "stability": self.stability, "description": self.describe(),
        }


def pattern_id_for(pattern_type: str, conditions: Dict[str, Any],
                   memory_version: str) -> str:
    """Deterministic, so a rebuild reproduces the same pattern ids."""
    payload = json.dumps({"t": pattern_type, "c": conditions,
                          "v": memory_version}, sort_keys=True, default=str)
    return f"pat-{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


def assess_stability(periods: Sequence[PatternPeriod]) -> Tuple[str, List[str]]:
    """
    Did the pattern behave the same way across sub-periods (§29)?

    Returns `(verdict, notes)`. The verdict is `insufficient_history`
    when fewer than two periods carry enough data — which is the honest
    answer for a record 27 days deep, and is what this project will
    report until it has more history.

    A pattern whose sub-periods disagree is UNSTABLE, not "good with
    noise". §29 asks for exactly that distinction, because averaging a
    good period with a bad one produces a mediocre number that describes
    neither.
    """
    usable = [p for p in periods
              if p.sample_size >= MIN_PATTERN_SAMPLE and p.hit_rate is not None]
    if len(usable) < MIN_STABILITY_PERIODS:
        return "insufficient_history", [
            f"only {len(usable)} sub-period(s) carry at least "
            f"{MIN_PATTERN_SAMPLE} experiences; stability cannot be assessed"]

    rates = [p.hit_rate for p in usable]
    spread = max(rates) - min(rates)
    if spread > STABILITY_DIVERGENCE:
        detail = "; ".join(f"{p.label} {p.hit_rate:.0%} (n={p.sample_size})"
                           for p in usable)
        return "unstable", [
            f"hit rate varies by {spread:.0%} across sub-periods: {detail}. "
            f"An average over these would describe none of them."]
    return "stable", [
        f"hit rate varies by only {spread:.0%} across {len(usable)} "
        f"sub-periods"]


def assess_confidence(*, sample_size: int, stability: str,
                      stdev_return: Optional[float],
                      conflicting: bool) -> MemoryConfidence:
    """
    How much a pattern's evidence supports it (§26).

    Inputs, in the order they can veto:

      * conflicting evidence     -> CONFLICTING_EVIDENCE, always
      * sample below threshold   -> INSUFFICIENT_EVIDENCE, always
      * unstable across periods  -> at most LOW
      * high dispersion          -> at most MEDIUM
      * otherwise                -> HIGH

    Nothing here is manufactured: each step is a stated rule over a
    measured quantity, and none of them is a probability.
    """
    if conflicting:
        return MemoryConfidence.CONFLICTING_EVIDENCE
    if sample_size < MIN_PATTERN_SAMPLE:
        return MemoryConfidence.INSUFFICIENT_EVIDENCE
    if stability == "unstable":
        return MemoryConfidence.LOW
    if stability == "insufficient_history":
        # Enough observations, not enough time. Real but provisional:
        # a regularity seen only inside one month has not been tested
        # against a different market.
        return MemoryConfidence.MEDIUM
    if stdev_return is not None and stdev_return > 0.10:
        return MemoryConfidence.MEDIUM
    return MemoryConfidence.HIGH


def summarise_distribution(values: Sequence[float]) -> Dict[str, Optional[float]]:
    """Mean, median and dispersion. None throughout on an empty series."""
    if not values:
        return {"mean": None, "median": None, "stdev": None}
    return {
        "mean": sum(values) / len(values),
        "median": statistics.median(values),
        "stdev": statistics.pstdev(values) if len(values) > 1 else None,
    }
