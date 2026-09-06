"""
src/memory/retrieval.py
-------------------------------
Asking memory what happened before — including as of a past date.

    current context -> structured query -> historical experiences
                    -> patterns -> summary with provenance

`memory_as_of(T)` IS THE POINT OF THIS FILE (§38, §71, §72)
---------------------------------------------------------------
A historical study anchored at time T must see only experience that was
knowable before T. Every function here takes `as_of` and every query
filters `available_at <= as_of`.

The filter is on `available_at` — when the outcome window CLOSED — and
not on `created_at`. That distinction is the whole guarantee. A signal
issued on 5 August with a 10-day horizon becomes knowable around 19
August; a study anchored on 12 August must not see it, even though the
row was written today.

Patterns are REBUILT from the as-of experience set, never read from the
stored table. A stored pattern was computed over everything and would
carry the future inside its averages; recomputing over the visible
subset is the only honest answer, and it is why
`similar_experiences()` and `memory_as_of()` are not simple SELECTs.

NO UNSUPPORTED CONCLUSIONS (§43, §64, §65)
----------------------------------------------
A memory response carries a summary, the supporting experiences, the
sample size, the evidence state, the time range and — always — the
limitations. The summary sentences are assembled from the numbers in
the response and cite them. Nothing generates prose that a reader
cannot trace to a row, and no LLM is involved anywhere in this package.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.data_access.memory_schema import initialize_memory_schema
from src.domain.memory_models import (
    MEMORY_METHOD_VERSION, MIN_PATTERN_SAMPLE, STALENESS_DAYS,
    MemoryConfidence, MemoryPattern, summarise_distribution,
)
from src.memory.patterns import (
    build_pattern, find_contradictions, load_experiences,
)

#: Dimensions a similarity query may match on (§41). Ordered from most
#: to least specific: a query relaxes from the end when it cannot find
#: enough neighbours, and the response says which dimensions it dropped.
SIMILARITY_DIMENSIONS: Tuple[str, ...] = (
    "expected_direction", "horizon", "event_type", "asset_class",
    "market_regime", "instrument_id", "sector_id",
)


@dataclass
class MemoryResponse:
    """
    What memory answers with (§43).

    `limitations` is not optional and is never empty in practice. Every
    response says what it could not see — the as-of cut, the sample
    size, the experimental share, the history depth — because a memory
    answer read without its limits is worse than no answer.
    """
    query: Dict[str, Any] = field(default_factory=dict)
    as_of: Optional[str] = None
    memory_version: str = MEMORY_METHOD_VERSION

    experiences: List[Dict[str, Any]] = field(default_factory=list)
    sample_size: int = 0
    instrument_count: int = 0
    experimental_count: int = 0

    hits: int = 0
    misses: int = 0
    neutrals: int = 0
    hit_rate: Optional[float] = None
    mean_return: Optional[float] = None
    median_return: Optional[float] = None
    stdev_return: Optional[float] = None
    mean_mfe: Optional[float] = None
    mean_mae: Optional[float] = None

    class_counts: Dict[str, int] = field(default_factory=dict)
    error_counts: Dict[str, int] = field(default_factory=dict)
    regime_breakdown: Dict[str, Any] = field(default_factory=dict)

    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    confidence: MemoryConfidence = MemoryConfidence.INSUFFICIENT_EVIDENCE
    relaxed_dimensions: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    summary: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query, "as_of": self.as_of,
            "memory_version": self.memory_version,
            "sample_size": self.sample_size,
            "instrument_count": self.instrument_count,
            "experimental_count": self.experimental_count,
            "hit_rate": self.hit_rate, "mean_return": self.mean_return,
            "median_return": self.median_return,
            "stdev_return": self.stdev_return,
            "mean_mfe": self.mean_mfe, "mean_mae": self.mean_mae,
            "class_counts": self.class_counts,
            "error_counts": self.error_counts,
            "regime_breakdown": self.regime_breakdown,
            "first_seen": self.first_seen, "last_seen": self.last_seen,
            "confidence": self.confidence.value,
            "relaxed_dimensions": self.relaxed_dimensions,
            "limitations": self.limitations, "summary": self.summary,
            "supporting_experience_ids": [e["experience_id"]
                                          for e in self.experiences],
        }


def _summarise(response: MemoryResponse, rows: Sequence[Dict[str, Any]],
               now: Optional[datetime] = None) -> None:
    """Fill the statistics and the limitations from the rows themselves."""
    response.experiences = list(rows)
    response.sample_size = len(rows)
    response.instrument_count = len({r.get("instrument_id") for r in rows
                                     if r.get("instrument_id")})
    response.experimental_count = sum(1 for r in rows
                                      if r.get("quality") == "experimental")

    for row in rows:
        result = row.get("direction_result")
        if result == "hit":
            response.hits += 1
        elif result == "miss":
            response.misses += 1
        elif result == "neutral":
            response.neutrals += 1

    decided = response.hits + response.misses
    if decided:
        response.hit_rate = response.hits / decided

    returns = [r["actual_return"] for r in rows if r.get("actual_return") is not None]
    stats = summarise_distribution(returns)
    response.mean_return = stats["mean"]
    response.median_return = stats["median"]
    response.stdev_return = stats["stdev"]

    mfes = [r["mfe"] for r in rows if r.get("mfe") is not None]
    maes = [r["mae"] for r in rows if r.get("mae") is not None]
    response.mean_mfe = (sum(mfes) / len(mfes)) if mfes else None
    response.mean_mae = (sum(maes) / len(maes)) if maes else None

    response.class_counts = dict(Counter(
        r["experience_class"] for r in rows if r.get("experience_class")))
    response.error_counts = dict(Counter(
        r["primary_error"] for r in rows if r.get("primary_error")))

    moments = sorted(m for m in (r.get("available_at") for r in rows) if m)
    if moments:
        response.first_seen, response.last_seen = moments[0], moments[-1]

    # ---- limitations, always (§43) ----------------------------------
    if response.sample_size < MIN_PATTERN_SAMPLE:
        response.limitations.append(
            f"only {response.sample_size} matching experience(s), under the "
            f"{MIN_PATTERN_SAMPLE} needed to describe a regularity")
    if response.experimental_count:
        response.limitations.append(
            f"{response.experimental_count} of {response.sample_size} came "
            f"from an unpromoted model — research experience, not production")
    if response.relaxed_dimensions:
        response.limitations.append(
            "the query was relaxed to find neighbours; dropped: "
            + ", ".join(response.relaxed_dimensions))
    if response.as_of:
        response.limitations.append(
            f"restricted to experience knowable at {response.as_of}; anything "
            f"whose outcome window closed later is deliberately absent")
    if response.first_seen and response.last_seen:
        span_days = 0
        try:
            span = (datetime.fromisoformat(response.last_seen.replace("Z", "+00:00"))
                    - datetime.fromisoformat(response.first_seen.replace("Z", "+00:00")))
            span_days = span.days
        except (TypeError, ValueError):
            span_days = 0
        if span_days < 90:
            response.limitations.append(
                f"the supporting experience spans only {span_days} day(s); "
                f"a regularity seen inside one window has not been tested "
                f"against a different market")
    if response.neutrals:
        response.limitations.append(
            f"{response.neutrals} experience(s) resolved neutral and are "
            f"excluded from the hit rate")

    conflicting = False
    response.regime_breakdown = {}
    by_regime: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        by_regime.setdefault(str(row.get("market_regime") or "unknown"),
                             []).append(row)
    for regime, members in by_regime.items():
        settled = [m for m in members if m.get("direction_result") in ("hit", "miss")]
        response.regime_breakdown[regime] = {
            "sample_size": len(members),
            "hit_rate": (sum(1 for m in settled
                             if m["direction_result"] == "hit") / len(settled))
            if settled else None,
        }
    usable = {k: v for k, v in response.regime_breakdown.items()
              if k != "unknown" and (v["sample_size"] or 0) >= MIN_PATTERN_SAMPLE
              and v["hit_rate"] is not None}
    if len(usable) >= 2:
        rates = [v["hit_rate"] for v in usable.values()]
        if max(rates) > 0.5 >= min(rates):
            conflicting = True
            response.limitations.append(
                "regimes disagree: this is context-dependent, and one rate "
                "describes neither half")

    from src.domain.memory_models import assess_confidence
    response.confidence = assess_confidence(
        sample_size=response.sample_size,
        stability="insufficient_history",
        stdev_return=response.stdev_return, conflicting=conflicting)

    # ---- the summary sentence, assembled from the numbers -----------
    if not rows:
        response.summary = ("No comparable experience is on record"
                            + (f" as of {response.as_of}." if response.as_of else "."))
    elif response.hit_rate is None:
        response.summary = (
            f"{response.sample_size} comparable experience(s) on record, none "
            f"of which resolved directionally.")
    elif response.sample_size < MIN_PATTERN_SAMPLE:
        response.summary = (
            f"{response.sample_size} comparable experience(s) on record "
            f"({response.hits} correct, {response.misses} wrong). Too few to "
            f"describe a regularity — reported for context only.")
    else:
        response.summary = (
            f"Across {response.sample_size} comparable historical "
            f"experience(s) between {(response.first_seen or '')[:10]} and "
            f"{(response.last_seen or '')[:10]}, the directional call was "
            f"right {response.hit_rate:.0%} of the time with a mean forward "
            f"return of {(response.mean_return or 0):+.2%}. This describes "
            f"what happened under these conditions, not what will happen.")


def memory_as_of(conn: sqlite3.Connection, as_of: Optional[str] = None, *,
                 memory_version: str = MEMORY_METHOD_VERSION,
                 qualities: Sequence[str] = ("validated", "experimental"),
                 rebuild_patterns: bool = True,
                 now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    What the system knew at a moment (§37, §38, §71).

    Patterns are REBUILT from the visible experience rather than read
    from the table. A stored pattern was computed over the whole record
    and carries the future inside its averages; recomputing over the
    as-of subset is the only answer that does not leak.

    `as_of=None` means now — the full record — and the response says so.
    """
    initialize_memory_schema(conn)
    rows = load_experiences(conn, memory_version=memory_version,
                            as_of=as_of, qualities=qualities)

    counts = Counter(r["experience_class"] for r in rows if r.get("experience_class"))
    errors = Counter(r["primary_error"] for r in rows if r.get("primary_error"))
    moments = sorted(m for m in (r.get("available_at") for r in rows) if m)

    patterns: List[MemoryPattern] = []
    if rebuild_patterns and rows:
        from src.memory.patterns import PATTERN_DEFINITIONS
        from collections import defaultdict
        for pattern_type, columns in PATTERN_DEFINITIONS:
            grouped: Dict[Tuple, List[Dict[str, Any]]] = defaultdict(list)
            for row in rows:
                key = tuple(row.get(column) for column in columns)
                if any(value in (None, "") for value in key):
                    continue
                grouped[key].append(row)
            for key, members in grouped.items():
                patterns.append(build_pattern(
                    pattern_type, dict(zip(columns, key)), members,
                    memory_version=memory_version, now=now))

    usable = [p for p in patterns if p.sample_size >= MIN_PATTERN_SAMPLE]
    return {
        "as_of": as_of,
        "memory_version": memory_version,
        "is_current": as_of is None,
        "experience_count": len(rows),
        "validated_count": sum(1 for r in rows if r.get("quality") == "validated"),
        "experimental_count": sum(1 for r in rows if r.get("quality") == "experimental"),
        "class_counts": dict(counts),
        "error_counts": dict(errors),
        "pattern_count": len(patterns),
        "patterns_above_sample_threshold": len(usable),
        "first_experience": moments[0] if moments else None,
        "last_experience": moments[-1] if moments else None,
        "patterns": patterns,
        "note": (
            "Patterns were recomputed from the experience visible at this "
            "moment, not read from storage. A stored pattern was aggregated "
            "over the whole record and would carry later evidence inside its "
            "averages."),
    }


def similar_experiences(conn: sqlite3.Connection, context: Dict[str, Any], *,
                        as_of: Optional[str] = None,
                        memory_version: str = MEMORY_METHOD_VERSION,
                        qualities: Sequence[str] = ("validated", "experimental"),
                        min_sample: int = MIN_PATTERN_SAMPLE,
                        limit: int = 200,
                        now: Optional[datetime] = None) -> MemoryResponse:
    """
    "What happened last time under conditions like these?" (§40, §41)

    Structured filtering, not vector search. §40 asks to start with
    structured filtering, and there is a stronger reason: an embedding
    match cannot say WHICH dimensions matched, so a response could not
    honestly report what it relaxed.

    Relaxation is progressive and recorded. The query starts fully
    specific and drops dimensions from the least specific end until it
    has `min_sample` neighbours; the response names every dimension it
    dropped, because "20 similar experiences" means something very
    different when similarity was reduced to "any short signal".
    """
    rows = load_experiences(conn, memory_version=memory_version,
                            as_of=as_of, qualities=qualities)

    requested = [d for d in SIMILARITY_DIMENSIONS
                 if context.get(d) not in (None, "")]
    active = list(requested)
    relaxed: List[str] = []
    matches: List[Dict[str, Any]] = []

    while True:
        matches = [row for row in rows
                   if all(row.get(d) == context.get(d) for d in active)]
        if len(matches) >= min_sample or not active:
            break
        relaxed.append(active.pop())

    response = MemoryResponse(
        query={d: context.get(d) for d in requested},
        as_of=as_of, memory_version=memory_version,
        relaxed_dimensions=list(reversed(relaxed)))
    _summarise(response, matches[:limit], now=now)
    if not active and requested:
        response.limitations.append(
            "every requested dimension was relaxed; these are all experiences "
            "on record, not comparable ones")
    return response


def model_memory(conn: sqlite3.Connection, trained_model_id: str, *,
                 as_of: Optional[str] = None,
                 memory_version: str = MEMORY_METHOD_VERSION) -> MemoryResponse:
    """
    What this model has historically done (§16).

    Never declares a model good or bad. It reports the record, the
    sample size and what failed; the judgement belongs to the human
    holding the promotion decision.
    """
    rows = [r for r in load_experiences(conn, memory_version=memory_version,
                                        as_of=as_of)
            if r.get("trained_model_id") == trained_model_id]
    response = MemoryResponse(query={"trained_model_id": trained_model_id},
                              as_of=as_of, memory_version=memory_version)
    _summarise(response, rows)
    response.limitations.append(
        "this is a historical record, not a verdict: one sample does not make "
        "a model good or bad, and promotion remains a human decision")
    return response


def regime_memory(conn: sqlite3.Connection, regime: str, *,
                  as_of: Optional[str] = None,
                  memory_version: str = MEMORY_METHOD_VERSION) -> MemoryResponse:
    """Experience under one regime (§17)."""
    rows = [r for r in load_experiences(conn, memory_version=memory_version,
                                        as_of=as_of)
            if str(r.get("market_regime") or "") == regime]
    response = MemoryResponse(query={"market_regime": regime}, as_of=as_of,
                              memory_version=memory_version)
    _summarise(response, rows)
    if not rows:
        response.limitations.append(
            "no experience carries this regime. `market_regime` is unpopulated "
            "throughout the production record, so regime memory is empty by "
            "data, not by design")
    return response


def event_memory(conn: sqlite3.Connection, event_type: str, *,
                 as_of: Optional[str] = None,
                 memory_version: str = MEMORY_METHOD_VERSION) -> MemoryResponse:
    """
    Experience around one event type (§18).

    Reports co-occurrence. It does not claim the event caused the move —
    §18 forbids causal claims, and an event study is not a causal
    identification strategy.
    """
    rows = [r for r in load_experiences(conn, memory_version=memory_version,
                                        as_of=as_of)
            if r.get("event_type") == event_type]
    response = MemoryResponse(query={"event_type": event_type}, as_of=as_of,
                              memory_version=memory_version)
    _summarise(response, rows)
    response.limitations.append(
        "co-occurrence only: nothing here establishes that the event caused "
        "the move")
    return response


def instrument_memory(conn: sqlite3.Connection, instrument_id: str, *,
                      as_of: Optional[str] = None,
                      memory_version: str = MEMORY_METHOD_VERSION) -> MemoryResponse:
    """Experience for one instrument (§19). Sample size always exposed."""
    rows = [r for r in load_experiences(conn, memory_version=memory_version,
                                        as_of=as_of)
            if r.get("instrument_id") == instrument_id]
    response = MemoryResponse(query={"instrument_id": instrument_id},
                              as_of=as_of, memory_version=memory_version)
    _summarise(response, rows)
    response.limitations.append(
        "a single instrument is the easiest place to overfit; treat this as "
        "context rather than as a finding")
    return response


def signal_memory(conn: sqlite3.Connection, *,
                  direction: Optional[str] = None,
                  horizon: Optional[str] = None,
                  strength_min: Optional[float] = None,
                  strength_max: Optional[float] = None,
                  confidence_min: Optional[float] = None,
                  as_of: Optional[str] = None,
                  memory_version: str = MEMORY_METHOD_VERSION) -> MemoryResponse:
    """Historical behaviour of signals matching a shape (§15)."""
    rows = load_experiences(conn, memory_version=memory_version, as_of=as_of)
    matches = []
    for row in rows:
        if row.get("subject_kind") != "signal":
            continue
        if direction and row.get("expected_direction") != direction:
            continue
        if horizon and row.get("horizon") != horizon:
            continue
        strength = row.get("signal_strength")
        if strength_min is not None and (strength is None or strength < strength_min):
            continue
        if strength_max is not None and (strength is None or strength > strength_max):
            continue
        score = row.get("signal_confidence")
        if confidence_min is not None and (score is None or score < confidence_min):
            continue
        matches.append(row)

    response = MemoryResponse(
        query={"direction": direction, "horizon": horizon,
               "strength_min": strength_min, "strength_max": strength_max,
               "confidence_min": confidence_min},
        as_of=as_of, memory_version=memory_version)
    _summarise(response, matches)
    return response


def risk_memory(conn: sqlite3.Connection, *,
                as_of: Optional[str] = None,
                memory_version: str = MEMORY_METHOD_VERSION) -> Dict[str, Any]:
    """
    Signals that were withheld, and what happened next (§21).

    The distinction §21 asks for — a good opportunity rejected versus a
    correct rejection — is exactly what the suppressed cohort answers,
    and NEITHER is labelled a mistake here. The numbers are reported and
    the judgement is left open.

    `risk_decisions` does not exist in this database, so this is signal
    suppression rather than portfolio risk. That is stated in the
    response rather than glossed.
    """
    rows = [r for r in load_experiences(conn, memory_version=memory_version,
                                        as_of=as_of)
            if r.get("subject_kind") == "signal"]
    withheld = [r for r in rows if r.get("primary_error") == "signal_error"]
    correct = [r for r in rows
               if r.get("direction_result") == "miss"
               and r.get("primary_error") not in (None, "signal_error")]

    response = {
        "as_of": as_of, "memory_version": memory_version,
        "withheld_but_right": len(withheld),
        "withheld_examples": [r["experience_id"] for r in withheld[:20]],
        "wrong_calls_not_withheld": len(correct),
        "note": (
            "Neither number is a verdict. A suppression that avoided a loss "
            "is the rule working; a suppression that withheld a correct call "
            "has a cost. Both are recorded so the trade-off can be studied, "
            "and neither is labelled a mistake."),
        "limitations": [
            "no `risk_decisions` table exists in this database, so this is "
            "signal-layer suppression rather than portfolio risk",
        ],
    }
    return response


def execution_memory(conn: sqlite3.Connection, *,
                     memory_version: str = MEMORY_METHOD_VERSION) -> Dict[str, Any]:
    """
    Execution experience (§20).

    Structured and empty. `order_intents`, `execution_orders` and
    `execution_fills` do not exist in this database and no order has
    ever been placed, so there is nothing to remember — and saying that
    explicitly is better than omitting the concept, because the shape is
    what Phase 22 will fill.
    """
    initialize_memory_schema(conn)
    present = {
        name for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = [t for t in ("order_intents", "execution_orders",
                           "execution_fills", "positions")
               if t not in present]
    return {
        "memory_version": memory_version,
        "available": not missing,
        "missing_tables": missing,
        "records": [],
        "note": (
            "Execution memory is defined and empty. No order has ever been "
            "placed, so there is no reference price, fill, slippage, latency "
            "or fee to remember. The shape exists so that the first real "
            "execution has somewhere to go."),
    }


def portfolio_memory(conn: sqlite3.Connection, *,
                     memory_version: str = MEMORY_METHOD_VERSION) -> Dict[str, Any]:
    """Portfolio experience (§22). Structured and empty, for the same reason."""
    initialize_memory_schema(conn)
    present = {
        name for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = [t for t in ("portfolios", "positions", "allocation_changes")
               if t not in present]
    return {
        "memory_version": memory_version,
        "available": not missing,
        "missing_tables": missing,
        "records": [],
        "note": (
            "Portfolio memory is defined and empty. No portfolio, allocation "
            "or position exists, so there is no exposure, concentration or "
            "correlation to remember."),
    }
