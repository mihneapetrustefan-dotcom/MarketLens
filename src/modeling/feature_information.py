"""
src/modeling/feature_information.py
--------------------------------------------------
Phase 25.9I-F.6D: does a feature carry information, and is it stable?

> A MORE COMPLEX MODEL CANNOT CREATE INFORMATION THAT IS NOT IN THE FEATURES.
>
> FEATURE DISCOVERY IS NOT MODEL QUALIFICATION.

WHY RANK CORRELATION AND NOT PEARSON
------------------------------------
The outcome here is an abnormal return, and this corpus contains returns
large enough that a single observation can dominate a Pearson
coefficient. Spearman is monotone-invariant, so it answers "do high
values of the feature go with high outcomes" without letting one
extreme day decide. It is also why no outlier deletion is needed: the
robustness is in the statistic, not in a cleaning step that could be
tuned.

WHY THE NULL IS BLOCK-AWARE
---------------------------
A parametric p-value for a correlation assumes independent
observations. These rows cluster by instrument and by session, so that
assumption is false and the resulting p-value would be far too small.

`block_permutation_pvalue` permutes outcomes WITHIN each evaluation
window, preserving window membership, window size and each window's
outcome distribution. Row-wise shuffling across the whole sample is not
offered by this module at all, because it would destroy exactly the
structure the null is supposed to hold fixed.

NO MAGNITUDE THRESHOLD LIVES HERE
---------------------------------
This module computes effect sizes, sign stability and empirical
p-values. It does not decide what counts as "enough". The deciding
rules are in the phase's pre-registration, so that a number nobody
chose cannot creep in through a default argument.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Fewer pairs than this and a rank correlation is not worth computing.
MIN_PAIRS = 10

#: Correlation at or above which two features are the same evidence.
#: Diagnostic only -- nothing is deleted on its account.
REDUNDANCY_THRESHOLD = 0.90

#: Above this, two features are effectively one vector.
DUPLICATE_THRESHOLD = 0.999


# ======================================================================
# Ranks and rank correlation
# ======================================================================

def average_ranks(values: Sequence[float]) -> List[float]:
    """Ranks with ties sharing their average, which is what Spearman needs."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        stop = index
        while (stop + 1 < len(order)
               and values[order[stop + 1]] == values[order[index]]):
            stop += 1
        shared = (index + stop) / 2.0 + 1.0
        for position in range(index, stop + 1):
            ranks[order[position]] = shared
        index = stop + 1
    return ranks


def _pairs(x: Sequence[Optional[float]],
           y: Sequence[Optional[float]]) -> List[Tuple[float, float]]:
    """
    Usable pairs only.

    A `None` on either side is dropped rather than imputed: a missing
    feature value is not a zero, and silently treating it as one would
    manufacture a relationship out of a missingness pattern.
    """
    return [(float(a), float(b)) for a, b in zip(x, y)
            if a is not None and b is not None]


def spearman(x: Sequence[Optional[float]],
             y: Sequence[Optional[float]]) -> Optional[float]:
    """
    Spearman's rho, or None when it cannot be computed.

    Returns None rather than 0.0 on a constant input: a feature with no
    variation has no measurable relationship, which is different from
    having a measured relationship of zero.
    """
    pairs = _pairs(x, y)
    if len(pairs) < MIN_PAIRS:
        return None
    xs = average_ranks([p[0] for p in pairs])
    ys = average_ranks([p[1] for p in pairs])
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    numerator = sum((a - mean_x) * (b - mean_y) for a, b in zip(xs, ys))
    denominator = (sum((a - mean_x) ** 2 for a in xs)
                   * sum((b - mean_y) ** 2 for b in ys)) ** 0.5
    if denominator == 0.0:
        return None
    return round(numerator / denominator, 6)


def trimmed_spearman(x: Sequence[Optional[float]],
                     y: Sequence[Optional[float]],
                     trim: float = 0.05) -> Optional[float]:
    """
    Spearman after dropping the most extreme `trim` of outcomes at each
    end.

    Reported ALONGSIDE the untrimmed value, never instead of it. The
    comparison is the diagnostic; substituting the trimmed figure would
    be deleting observations because they weakened a result.
    """
    pairs = _pairs(x, y)
    if len(pairs) < MIN_PAIRS:
        return None
    pairs.sort(key=lambda p: p[1])
    cut = int(len(pairs) * trim)
    kept = pairs[cut:len(pairs) - cut] if cut else pairs
    if len(kept) < MIN_PAIRS:
        return None
    return spearman([p[0] for p in kept], [p[1] for p in kept])


def sign_consistency(values: Sequence[Optional[float]]
                     ) -> Dict[str, Any]:
    """
    How often the relationship points the same way.

    A feature whose sign flips across windows is not information, however
    large any single window's coefficient was.
    """
    present = [v for v in values if v is not None]
    if not present:
        return {"windows": 0, "scored": 0, "mean": None,
                "same_sign_fraction": None, "all_same_sign": False,
                "positive": 0, "negative": 0}
    mean = sum(present) / len(present)
    positive = sum(1 for v in present if v > 0)
    negative = sum(1 for v in present if v < 0)
    agreeing = positive if mean >= 0 else negative
    return {
        "windows": len(values),
        "scored": len(present),
        "mean": round(mean, 6),
        "worst": round(min(present), 6),
        "best": round(max(present), 6),
        "spread": round(max(present) - min(present), 6),
        "positive": positive,
        "negative": negative,
        "same_sign_fraction": round(agreeing / len(present), 4),
        "all_same_sign": agreeing == len(present) and len(present) > 0,
    }


# ======================================================================
# The block-aware null
# ======================================================================

def block_permutation_pvalue(
        windows: Sequence[Tuple[Sequence[Optional[float]],
                                Sequence[Optional[float]]]],
        *, permutations: int = 500, seed: int = 0) -> Dict[str, Any]:
    """
    Empirical two-sided p-value for |mean rho| under within-window
    permutation.

    Each window's outcomes are shuffled among that window's own rows, so
    window membership, window size and the window's outcome distribution
    are all preserved; only the pairing with the feature is broken.
    Permuting across windows would additionally destroy the temporal
    structure, which is the thing the null has to keep.

    Returns None for the p-value when the observed statistic cannot be
    computed -- not 1.0, which would read as a measured non-result.
    """
    observed_per_window = [spearman(feature, outcome)
                           for feature, outcome in windows]
    present = [v for v in observed_per_window if v is not None]
    if not present:
        return {"observed_mean_rho": None, "p_value": None,
                "permutations": 0,
                "status": "NOT_COMPUTED: no window yielded a coefficient"}

    observed = abs(sum(present) / len(present))
    generator = random.Random(seed)
    at_least_as_extreme = 0
    null_statistics: List[float] = []

    for _ in range(permutations):
        values: List[float] = []
        for feature, outcome in windows:
            pairs = _pairs(feature, outcome)
            if len(pairs) < MIN_PAIRS:
                continue
            shuffled = [p[1] for p in pairs]
            generator.shuffle(shuffled)
            rho = spearman([p[0] for p in pairs], shuffled)
            if rho is not None:
                values.append(rho)
        if not values:
            continue
        statistic = abs(sum(values) / len(values))
        null_statistics.append(statistic)
        if statistic >= observed:
            at_least_as_extreme += 1

    if not null_statistics:
        return {"observed_mean_rho": round(observed, 6), "p_value": None,
                "permutations": 0,
                "status": "NOT_COMPUTED: no permutation yielded a statistic"}

    # The +1 convention keeps the p-value strictly positive: with 500
    # permutations the smallest reportable value is 1/501, and claiming
    # p = 0 from a finite sample would overstate the evidence.
    p_value = (at_least_as_extreme + 1) / (len(null_statistics) + 1)
    null_statistics.sort()
    return {
        "observed_mean_rho": round(observed, 6),
        "p_value": round(p_value, 6),
        "permutations": len(null_statistics),
        "null_median": round(null_statistics[len(null_statistics) // 2], 6),
        "null_p95": round(
            null_statistics[min(len(null_statistics) - 1,
                                int(0.95 * len(null_statistics)))], 6),
        "status": "COMPUTED",
    }


# ======================================================================
# Multiple testing
# ======================================================================

def benjamini_hochberg(p_values: Dict[str, Optional[float]],
                       q: float = 0.05) -> Dict[str, Any]:
    """
    Benjamini-Hochberg step-up over a declared family.

    Hypotheses whose p-value could not be computed stay in the family
    count -- dropping them would shrink the denominator and make the
    correction weaker than the family actually is.
    """
    ordered = sorted(((name, p) for name, p in p_values.items()
                      if p is not None), key=lambda item: item[1])
    family_size = len(p_values)
    rejected: List[str] = []
    adjusted: Dict[str, Optional[float]] = {name: None for name in p_values}

    threshold_index = 0
    for rank, (name, p) in enumerate(ordered, start=1):
        if p <= q * rank / family_size:
            threshold_index = rank
    for rank, (name, p) in enumerate(ordered, start=1):
        if rank <= threshold_index:
            rejected.append(name)

    # Step-up adjusted values, enforced monotone from the largest down.
    running = 1.0
    for rank in range(len(ordered), 0, -1):
        name, p = ordered[rank - 1]
        running = min(running, p * family_size / rank)
        adjusted[name] = round(min(1.0, running), 6)

    return {
        "family_size": family_size,
        "tested": len(ordered),
        "uncomputable": family_size - len(ordered),
        "q": q,
        "rejected": sorted(rejected),
        "rejected_count": len(rejected),
        "adjusted": adjusted,
        "nominal_below_q": sum(1 for _, p in ordered if p <= q),
    }


# ======================================================================
# Redundancy and identity
# ======================================================================

def vector_fingerprint(values: Sequence[Optional[float]]) -> str:
    """
    SHA-256 of the effective value vector, with `None` encoded
    distinctly from any number.

    Two feature names producing this fingerprint are one source of
    evidence, not two, however differently they are described.
    """
    payload = json.dumps(
        ["null" if v is None else round(float(v), 10) for v in values],
        separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class Redundancy(str, Enum):
    UNIQUE = "UNIQUE"
    DUPLICATE = "DUPLICATE"
    HIGHLY_REDUNDANT = "HIGHLY_REDUNDANT"
    ZERO_VARIANCE = "ZERO_VARIANCE"
    NO_USABLE_VALUE = "NO_USABLE_VALUE"
    UNKNOWN = "UNKNOWN"


def redundancy_map(columns: Dict[str, Sequence[Optional[float]]], *,
                   threshold: float = REDUNDANCY_THRESHOLD,
                   duplicate_threshold: float = DUPLICATE_THRESHOLD
                   ) -> Dict[str, Any]:
    """
    Pairwise rank correlation between features, and the resulting
    classification.

    Uses no outcome values. Nothing is deleted: a redundant feature stays
    in the audit and is simply barred from being counted as independent
    evidence of its partner.
    """
    names = sorted(columns)
    fingerprints = {name: vector_fingerprint(columns[name]) for name in names}
    duplicates: List[Tuple[str, str, float]] = []
    redundant: List[Tuple[str, str, float]] = []
    identical_vectors: List[Tuple[str, str]] = []

    for i, left in enumerate(names):
        for right in names[i + 1:]:
            if fingerprints[left] == fingerprints[right]:
                identical_vectors.append((left, right))
            rho = spearman(columns[left], columns[right])
            if rho is None:
                continue
            if abs(rho) > duplicate_threshold:
                duplicates.append((left, right, round(rho, 6)))
            elif abs(rho) > threshold:
                redundant.append((left, right, round(rho, 6)))

    classification: Dict[str, str] = {}
    partner: Dict[str, List[str]] = {name: [] for name in names}
    for left, right, _rho in duplicates + redundant:
        partner[left].append(right)
        partner[right].append(left)

    # An identical fingerprint is a duplicate whether or not rho could be
    # computed. Two CONSTANT features correlate to None -- there is no
    # variation to correlate -- so a correlation-only rule would have
    # called them two independent sources. Section 22 of the brief asks
    # for the opposite, and the fingerprint is what delivers it.
    for left, right in identical_vectors:
        partner[left].append(right)
        partner[right].append(left)
    fingerprint_duplicates = {n for pair in identical_vectors for n in pair}

    for name in names:
        usable = [v for v in columns[name] if v is not None]
        if not usable:
            classification[name] = Redundancy.NO_USABLE_VALUE.value
        elif len(set(usable)) <= 1:
            classification[name] = Redundancy.ZERO_VARIANCE.value
        elif (name in fingerprint_duplicates
              or any(name in (l, r) for l, r, _ in duplicates)):
            classification[name] = Redundancy.DUPLICATE.value
        elif partner[name]:
            classification[name] = Redundancy.HIGHLY_REDUNDANT.value
        else:
            classification[name] = Redundancy.UNIQUE.value

    # Effective unique count: collapse each redundancy cluster to one.
    remaining = set(names)
    clusters = 0
    while remaining:
        seed = remaining.pop()
        stack = [seed]
        while stack:
            current = stack.pop()
            for other in partner.get(current, []):
                if other in remaining:
                    remaining.discard(other)
                    stack.append(other)
        clusters += 1

    return {
        "features": len(names),
        "exact_or_near_duplicates": duplicates,
        "high_redundancy_pairs": redundant,
        "identical_fingerprints": identical_vectors,
        "classification": classification,
        "fingerprints": fingerprints,
        "effective_unique_count": clusters,
        "threshold": threshold,
        "duplicate_threshold": duplicate_threshold,
    }


# ======================================================================
# Classification
# ======================================================================

class FeatureVerdict(str, Enum):
    STABLE_INFORMATION_CANDIDATE = "STABLE_INFORMATION_CANDIDATE"
    WEAK_INFORMATION = "WEAK_INFORMATION"
    UNSTABLE = "UNSTABLE"
    REDUNDANT = "REDUNDANT"
    NO_INFORMATION = "NO_INFORMATION"
    NO_USABLE_VALUE = "NO_USABLE_VALUE"
    TEMPORAL_INTEGRITY_FAILED = "TEMPORAL_INTEGRITY_FAILED"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
    UNKNOWN = "UNKNOWN"

    @property
    def is_candidate(self) -> bool:
        return self is FeatureVerdict.STABLE_INFORMATION_CANDIDATE


class GroupVerdict(str, Enum):
    PROMISING_INFORMATION_GROUP = "PROMISING_INFORMATION_GROUP"
    WEAK = "WEAK"
    UNSTABLE = "UNSTABLE"
    REDUNDANT = "REDUNDANT"
    NO_EVIDENCE = "NO_EVIDENCE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class InformationResult:
    """
    One feature on one horizon: the effect, its stability, the null, and
    the resulting label.

    `verdict` is assigned by the caller from the pre-registered rules.
    This container records the evidence and refuses to invent a label of
    its own.
    """
    feature: str
    group: str
    horizon: str
    windows: int
    pooled_pairs: int
    pooled_clusters: int
    availability: float
    per_window_rho: Tuple[Optional[float], ...]
    stability: Dict[str, Any]
    null: Dict[str, Any]
    trimmed_mean_rho: Optional[float] = None
    adjusted_p_value: Optional[float] = None
    fdr_rejected: bool = False
    concentration: Dict[str, Any] = field(default_factory=dict)
    distinct_values: Optional[int] = None
    constant_value: Optional[float] = None
    redundancy: str = Redundancy.UNKNOWN.value
    timing: str = "OK"
    verdict: str = FeatureVerdict.UNKNOWN.value
    notes: Tuple[str, ...] = ()

    @property
    def mean_rho(self) -> Optional[float]:
        return self.stability.get("mean")

    def describe(self) -> Dict[str, Any]:
        return {
            "feature": self.feature, "group": self.group,
            "horizon": self.horizon, "windows": self.windows,
            "pooled_pairs": self.pooled_pairs,
            "pooled_clusters": self.pooled_clusters,
            "availability": self.availability,
            "distinct_values": self.distinct_values,
            "constant_value": self.constant_value,
            "per_window_rho": list(self.per_window_rho),
            "mean_rho": self.mean_rho,
            "trimmed_mean_rho": self.trimmed_mean_rho,
            "stability": self.stability,
            "null": self.null,
            "adjusted_p_value": self.adjusted_p_value,
            "fdr_rejected": self.fdr_rejected,
            "concentration": self.concentration,
            "redundancy": self.redundancy,
            "timing": self.timing,
            "verdict": self.verdict,
            "notes": list(self.notes),
        }


def classify(result: InformationResult, *,
             min_windows: int, min_clusters: int,
             redundant_partner_is_candidate: bool = False) -> Tuple[str, List[str]]:
    """
    The pre-registered decision rule, applied mechanically.

    Deliberately contains no magnitude threshold on rho. What decides is
    the block-aware null, the sign consistency, the concentration check
    and the redundancy map -- each a property of the data or the design
    rather than a number chosen here.
    """
    notes: List[str] = []

    if result.timing != "OK":
        return FeatureVerdict.TEMPORAL_INTEGRITY_FAILED.value, [result.timing]
    if result.redundancy == Redundancy.NO_USABLE_VALUE.value:
        return FeatureVerdict.NO_USABLE_VALUE.value, [
            "null in every observation; there is nothing to audit"]
    if result.distinct_values is not None and result.distinct_values <= 1:
        # A constant carries no information, and that is not the same
        # thing as a thin sample. INSUFFICIENT_SAMPLE is reserved for a
        # design that is too small -- few windows or few clusters --
        # which is the condition the pre-registration actually names.
        # A feature with 5 windows and 237 clusters and one single value
        # has a perfectly adequate design and nothing to measure.
        return FeatureVerdict.NO_INFORMATION.value, [
            f"constant across every observation (value "
            f"{result.constant_value}); zero variance, so no relationship "
            f"is measurable by any model"]
    if result.pooled_clusters < min_clusters:
        return FeatureVerdict.INSUFFICIENT_SAMPLE.value, [
            f"{result.pooled_clusters} pooled clusters; {min_clusters} required"]
    if result.stability.get("scored", 0) < min_windows:
        return FeatureVerdict.INSUFFICIENT_SAMPLE.value, [
            f"only {result.stability.get('scored', 0)} scored window(s); "
            f"{min_windows} required"]

    consistent = bool(result.stability.get("all_same_sign"))
    significant = bool(result.fdr_rejected)
    concentrated = result.concentration.get("survives") is False
    if concentrated:
        notes.append("CONCENTRATION_WARNING: the effect does not survive "
                     "removing its largest-contributing instrument")

    if significant and consistent and not concentrated:
        if redundant_partner_is_candidate:
            notes.append("a more-complete partner above the redundancy "
                         "threshold is already a candidate")
            return FeatureVerdict.REDUNDANT.value, notes
        return FeatureVerdict.STABLE_INFORMATION_CANDIDATE.value, notes
    if significant and consistent and concentrated:
        return FeatureVerdict.WEAK_INFORMATION.value, notes
    if significant and not consistent:
        notes.append("distinguishable from the null but the sign flips "
                     "across windows")
        return FeatureVerdict.UNSTABLE.value, notes
    if consistent:
        notes.append("sign is consistent but the effect is not "
                     "distinguishable from a block-aware null")
        return FeatureVerdict.WEAK_INFORMATION.value, notes
    return FeatureVerdict.NO_INFORMATION.value, notes


def information_funnel(results: Sequence[InformationResult]
                       ) -> Dict[str, int]:
    """The stage-by-stage count, in one unit: feature-horizon cells."""
    timing_ok = [r for r in results
                 if r.verdict != FeatureVerdict.TEMPORAL_INTEGRITY_FAILED.value]
    usable = [r for r in timing_ok
              if r.verdict != FeatureVerdict.NO_USABLE_VALUE.value]
    adequate = [r for r in usable
                if r.verdict != FeatureVerdict.INSUFFICIENT_SAMPLE.value]
    varying = [r for r in adequate
               if r.redundancy != Redundancy.ZERO_VARIANCE.value]
    non_redundant = [r for r in varying
                     if r.redundancy != Redundancy.DUPLICATE.value]
    with_effect = [r for r in non_redundant
                   if r.mean_rho not in (None, 0.0)]
    consistent = [r for r in with_effect if r.stability.get("all_same_sign")]
    survived_null = [r for r in consistent if r.fdr_rejected]
    candidates = [r for r in survived_null
                  if r.verdict == FeatureVerdict.STABLE_INFORMATION_CANDIDATE.value]
    stages = [
        ("inventoried", len(results)),
        ("time_safe", len(timing_ok)),
        ("usable_values", len(usable)),
        ("adequate_sample", len(adequate)),
        ("has_variance", len(varying)),
        ("non_duplicate", len(non_redundant)),
        ("measurable_effect", len(with_effect)),
        ("sign_consistent", len(consistent)),
        ("survived_fdr_and_null", len(survived_null)),
        ("stable_candidates", len(candidates)),
    ]
    # `stage_order` travels with the counts because the result artifact
    # is written with sorted keys, which alphabetises a funnel and makes
    # it read as though it did not reconcile. The order is part of the
    # evidence, so it is stored rather than inferred.
    return {
        "stage_order": [name for name, _ in stages],
        "stages": [{"stage": name, "count": count} for name, count in stages],
        **dict(stages),
    }
