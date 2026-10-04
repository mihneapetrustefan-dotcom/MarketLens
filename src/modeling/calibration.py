"""
src/modeling/calibration.py
--------------------------------------------------
Phase 25.9I-F.6C: probability calibration and the metrics that judge it.

> CALIBRATION IS NOT A TOOL FOR TURNING A FAILED MODEL INTO A PASSING ONE.
> It answers a narrower question: WHEN THE MODEL SAYS 70%, DOES 70% MEAN
> SOMETHING?

Two dimensions, kept apart on purpose:

  DISCRIMINATION  can the model separate up from down at all?
                  `roc_auc`, `balanced_accuracy`
  CALIBRATION     when it says P, is P trustworthy?
                  `brier_score`, `brier_skill_score`, `log_loss`,
                  `expected_calibration_error`, `reliability_bins`,
                  `calibration_curve`

A model can be perfectly calibrated and useless (predict the base rate
every time: calibration ideal, discrimination nil), or sharply
discriminative and wildly overconfident. Collapsing the two into one
number hides exactly the distinction this phase exists to measure, so
nothing here returns a combined score.

WHY 0.5 AND 0 ARE NOT INVENTED THRESHOLDS
-----------------------------------------
`balanced_accuracy` is the mean of sensitivity and specificity, so ANY
constant predictor scores exactly 0.5 whatever the class balance. Same
for `roc_auc`. And `brier_skill_score` is zero exactly when the model
matches its base-rate baseline. Those three zero-points are properties
of the metrics, not numbers anyone chose -- which is what makes them
usable as an outcome-blind quality floor.

That matters because Phase 25.9I-F.6B exposed a cell credited with a
baseline "win" at 31.2% raw accuracy against a 26.0% baseline. Raw
accuracy inherits the fold's class balance; balanced accuracy does not.

PURE STANDARD LIBRARY
---------------------
No new dependency. The calibrators are the two standard choices --
parametric (Platt) and non-parametric (isotonic) -- and both are small
enough to read.

Platt scaling and the calibration curve are the SAME regression: fitting
`y ~ sigmoid(a * logit(p) + b)` gives the calibration slope `a` and
intercept `b` directly, with `a = 1, b = 0` meaning already calibrated.
So one fitter serves both, and the diagnostic cannot disagree with the
calibrator.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Probabilities are clipped before any logit so a confident 0 or 1
#: cannot produce an infinity. Tight enough not to distort a real
#: estimate, loose enough to keep the arithmetic finite.
EPSILON = 1e-6

#: A reliability bin below this many observations says nothing, and a
#: ten-bin chart with one point per bin is worse than no chart.
MIN_BIN_SUPPORT = 10


def _pairs(probabilities: Sequence[Optional[float]],
           outcomes: Sequence[Optional[float]],
           threshold: float = 0.0) -> List[Tuple[float, int]]:
    """
    Usable (probability, binary outcome) pairs.

    A `None` probability is an abstention and a `None` outcome is an
    unmatured label; neither is evidence, and neither is silently
    treated as 0.
    """
    out: List[Tuple[float, int]] = []
    for probability, outcome in zip(probabilities, outcomes):
        if probability is None or outcome is None:
            continue
        out.append((float(probability), 1 if outcome > threshold else 0))
    return out


def _clip(probability: float) -> float:
    return min(1.0 - EPSILON, max(EPSILON, probability))


def logit(probability: float) -> float:
    clipped = _clip(probability)
    return math.log(clipped / (1.0 - clipped))


def sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, z))))


# ======================================================================
# Discrimination
# ======================================================================

def roc_auc(probabilities: Sequence[Optional[float]],
            outcomes: Sequence[Optional[float]],
            threshold: float = 0.0) -> Optional[float]:
    """
    Area under the ROC curve, by the rank (Mann-Whitney) identity, with
    ties sharing their average rank.

    Returns None when one class is absent -- AUC is undefined then, and
    a fold with no negatives has not demonstrated anything. 0.5 is the
    no-skill value for any constant or random predictor.
    """
    pairs = _pairs(probabilities, outcomes, threshold)
    positives = [p for p, y in pairs if y == 1]
    negatives = [p for p, y in pairs if y == 0]
    if not positives or not negatives:
        return None

    ordered = sorted(pairs, key=lambda item: item[0])
    ranks: List[float] = [0.0] * len(ordered)
    index = 0
    while index < len(ordered):
        stop = index
        while stop + 1 < len(ordered) and ordered[stop + 1][0] == ordered[index][0]:
            stop += 1
        average = (index + stop) / 2.0 + 1.0
        for position in range(index, stop + 1):
            ranks[position] = average
        index = stop + 1

    positive_rank_sum = sum(rank for rank, (_, y) in zip(ranks, ordered) if y == 1)
    n_pos, n_neg = len(positives), len(negatives)
    statistic = positive_rank_sum - n_pos * (n_pos + 1) / 2.0
    return round(statistic / (n_pos * n_neg), 6)


def confusion(probabilities: Sequence[Optional[float]],
              outcomes: Sequence[Optional[float]],
              threshold: float = 0.0,
              decision_threshold: float = 0.5) -> Dict[str, int]:
    pairs = _pairs(probabilities, outcomes, threshold)
    counts = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
    for probability, outcome in pairs:
        predicted = 1 if probability > decision_threshold else 0
        if predicted == 1 and outcome == 1:
            counts["tp"] += 1
        elif predicted == 1 and outcome == 0:
            counts["fp"] += 1
        elif predicted == 0 and outcome == 0:
            counts["tn"] += 1
        else:
            counts["fn"] += 1
    return counts


def balanced_accuracy(probabilities: Sequence[Optional[float]],
                      outcomes: Sequence[Optional[float]],
                      threshold: float = 0.0,
                      decision_threshold: float = 0.5) -> Optional[float]:
    """
    Mean of sensitivity and specificity.

    Immune to class balance by construction: a predictor that always
    says "up" has sensitivity 1 and specificity 0, so it scores exactly
    0.5 whether the period was 30% up or 70% up. That is why 0.5 is a
    legitimate no-skill floor and raw accuracy is not.

    Returns None when either class is absent from the outcomes.
    """
    counts = confusion(probabilities, outcomes, threshold, decision_threshold)
    positives = counts["tp"] + counts["fn"]
    negatives = counts["tn"] + counts["fp"]
    if not positives or not negatives:
        return None
    sensitivity = counts["tp"] / positives
    specificity = counts["tn"] / negatives
    return round((sensitivity + specificity) / 2.0, 6)


def accuracy(probabilities: Sequence[Optional[float]],
             outcomes: Sequence[Optional[float]],
             threshold: float = 0.0,
             decision_threshold: float = 0.5) -> Optional[float]:
    pairs = _pairs(probabilities, outcomes, threshold)
    if not pairs:
        return None
    correct = sum(1 for probability, outcome in pairs
                  if (1 if probability > decision_threshold else 0) == outcome)
    return round(correct / len(pairs), 6)


def positive_rate(outcomes: Sequence[Optional[float]],
                  threshold: float = 0.0) -> Optional[float]:
    usable = [1 if o > threshold else 0 for o in outcomes if o is not None]
    if not usable:
        return None
    return round(sum(usable) / len(usable), 6)


# ======================================================================
# Calibration
# ======================================================================

def brier_score(probabilities: Sequence[Optional[float]],
                outcomes: Sequence[Optional[float]],
                threshold: float = 0.0) -> Optional[float]:
    """Mean squared error of the probability. Lower is better."""
    pairs = _pairs(probabilities, outcomes, threshold)
    if not pairs:
        return None
    return round(sum((p - y) ** 2 for p, y in pairs) / len(pairs), 6)


def brier_skill_score(model_brier: Optional[float],
                      baseline_brier: Optional[float]) -> Optional[float]:
    """
    `1 - BS_model / BS_baseline`.

    Zero exactly when the model matches its baseline, positive only when
    it beats it. The zero point is a property of the definition, not a
    threshold anyone picked. Returns None when the baseline Brier is
    zero or absent, because the ratio is then undefined rather than
    infinitely good.
    """
    if model_brier is None or baseline_brier is None:
        return None
    if baseline_brier <= 0.0:
        return None
    return round(1.0 - model_brier / baseline_brier, 6)


def log_loss(probabilities: Sequence[Optional[float]],
             outcomes: Sequence[Optional[float]],
             threshold: float = 0.0) -> Optional[float]:
    pairs = _pairs(probabilities, outcomes, threshold)
    if not pairs:
        return None
    total = 0.0
    for probability, outcome in pairs:
        clipped = _clip(probability)
        total -= (math.log(clipped) if outcome == 1
                  else math.log(1.0 - clipped))
    return round(total / len(pairs), 6)


@dataclass(frozen=True)
class ReliabilityBin:
    lower: float
    upper: float
    count: int
    mean_predicted: Optional[float]
    observed_rate: Optional[float]

    @property
    def has_support(self) -> bool:
        return self.count >= MIN_BIN_SUPPORT

    @property
    def gap(self) -> Optional[float]:
        if self.mean_predicted is None or self.observed_rate is None:
            return None
        return round(abs(self.mean_predicted - self.observed_rate), 6)

    def describe(self) -> Dict[str, Any]:
        return {"lower": self.lower, "upper": self.upper, "count": self.count,
                "mean_predicted": self.mean_predicted,
                "observed_rate": self.observed_rate, "gap": self.gap,
                "has_support": self.has_support}


def reliability_bins(probabilities: Sequence[Optional[float]],
                     outcomes: Sequence[Optional[float]],
                     threshold: float = 0.0, bin_count: int = 5,
                     min_support: int = MIN_BIN_SUPPORT) -> List[ReliabilityBin]:
    """
    Equal-width probability bins with their observed frequencies.

    `bin_count` is a request, not a promise: bins are reported with
    their support and a bin below `min_support` is marked rather than
    drawn as if it meant something.
    """
    pairs = _pairs(probabilities, outcomes, threshold)
    bins: List[ReliabilityBin] = []
    width = 1.0 / bin_count
    for index in range(bin_count):
        lower = index * width
        upper = 1.0 if index == bin_count - 1 else (index + 1) * width
        inside = [(p, y) for p, y in pairs
                  if (lower <= p <= upper if index == bin_count - 1
                      else lower <= p < upper)]
        if inside:
            mean_predicted = round(sum(p for p, _ in inside) / len(inside), 6)
            observed = round(sum(y for _, y in inside) / len(inside), 6)
        else:
            mean_predicted = observed = None
        bins.append(ReliabilityBin(round(lower, 6), round(upper, 6),
                                   len(inside), mean_predicted, observed))
    return bins


def expected_calibration_error(probabilities: Sequence[Optional[float]],
                               outcomes: Sequence[Optional[float]],
                               threshold: float = 0.0, bin_count: int = 5,
                               min_support: int = MIN_BIN_SUPPORT
                               ) -> Tuple[Optional[float], str]:
    """
    Count-weighted mean gap between stated and observed frequency.

    Returns `(value, status)` where status is COMPUTED, or NOT_COMPUTED
    with the reason. An ECE averaged over bins holding two observations
    each is noise wearing a decimal point, so it is refused rather than
    reported -- and `NOT_COMPUTED` is deliberately distinct from `0.0`,
    which would mean perfect calibration.
    """
    bins = reliability_bins(probabilities, outcomes, threshold, bin_count,
                            min_support)
    supported = [b for b in bins if b.count and b.has_support]
    total = sum(b.count for b in supported)
    if not supported or total == 0:
        return None, ("NOT_COMPUTED: no probability bin reaches "
                      f"{min_support} observations")
    weighted = sum(b.count * (b.gap or 0.0) for b in supported)
    return round(weighted / total, 6), "COMPUTED"


# ======================================================================
# The shared fitter: Platt scaling IS the calibration curve
# ======================================================================

def _fit_scalar_logistic(x: Sequence[float], y: Sequence[int], *,
                         iterations: int = 100,
                         tolerance: float = 1e-9) -> Optional[Tuple[float, float]]:
    """
    Unregularized 1-D logistic regression by Newton-Raphson (IRLS).

    Unregularized on purpose: the fitted slope has to be interpretable
    against 1.0, and shrinkage would pull every estimate toward zero and
    make a well-calibrated model look under-confident.

    Returns `(slope, intercept)`, or None when the fit is degenerate --
    one class only, no variation in `x`, or a singular step.
    """
    if len(x) != len(y) or len(x) < 4:
        return None
    if len(set(y)) < 2:
        return None
    if max(x) - min(x) < 1e-12:
        return None

    def negative_log_likelihood(slope: float, intercept: float) -> float:
        total = 0.0
        for xi, yi in zip(x, y):
            z = max(-60.0, min(60.0, slope * xi + intercept))
            # log(1 + exp(z)) computed stably, then the y*z term.
            total += (z if z > 0 else 0.0) + math.log1p(math.exp(-abs(z)))
            total -= yi * z
        return total

    # Start at the NULL MODEL: zero slope, intercept at the empirical
    # log-odds. Starting from slope = 1 is what broke this: an input of
    # logit(p) can reach +/-14, so a unit slope saturates every sigmoid
    # on the first step, the IRLS weights p*(1-p) underflow, and the
    # Hessian goes singular -- the fit then reports "degenerate" on data
    # that converges in three iterations from a sane start.
    mean_y = sum(y) / len(y)
    slope = 0.0
    intercept = logit(min(1.0 - EPSILON, max(EPSILON, mean_y)))
    current = negative_log_likelihood(slope, intercept)

    for _ in range(iterations):
        g_slope = g_intercept = 0.0
        h_ss = h_si = h_ii = 0.0
        for xi, yi in zip(x, y):
            p = sigmoid(slope * xi + intercept)
            residual = p - yi
            weight = max(p * (1.0 - p), 1e-12)
            g_slope += residual * xi
            g_intercept += residual
            h_ss += weight * xi * xi
            h_si += weight * xi
            h_ii += weight
        determinant = h_ss * h_ii - h_si * h_si
        if abs(determinant) < 1e-14:
            break
        step_slope = (h_ii * g_slope - h_si * g_intercept) / determinant
        step_intercept = (h_ss * g_intercept - h_si * g_slope) / determinant

        # Damped Newton: halve the step until the likelihood actually
        # improves. Undamped Newton on a logistic likelihood can
        # overshoot into saturation and never come back.
        scale = 1.0
        for _attempt in range(20):
            trial_slope = slope - scale * step_slope
            trial_intercept = intercept - scale * step_intercept
            if not (math.isfinite(trial_slope) and math.isfinite(trial_intercept)):
                scale *= 0.5
                continue
            trial = negative_log_likelihood(trial_slope, trial_intercept)
            if trial <= current:
                slope, intercept, improvement = (trial_slope, trial_intercept,
                                                 current - trial)
                current = trial
                break
            scale *= 0.5
        else:
            break

        if (abs(scale * step_slope) < tolerance
                and abs(scale * step_intercept) < tolerance):
            break

    if not (math.isfinite(slope) and math.isfinite(intercept)):
        return None
    return slope, intercept


def calibration_curve(probabilities: Sequence[Optional[float]],
                      outcomes: Sequence[Optional[float]],
                      threshold: float = 0.0) -> Dict[str, Any]:
    """
    Calibration slope and intercept from `y ~ sigmoid(a*logit(p) + b)`.

    `a = 1, b = 0` means already calibrated. `a < 1` means
    overconfident -- the probabilities are too extreme for what actually
    happens. Reported with the sample size so an unstable estimate from
    a thin fold is visible rather than authoritative.
    """
    pairs = _pairs(probabilities, outcomes, threshold)
    if not pairs:
        return {"status": "NOT_COMPUTED: no usable pairs",
                "slope": None, "intercept": None, "n": 0, "reading": None}
    fit = _fit_scalar_logistic([logit(p) for p, _ in pairs],
                               [y for _, y in pairs])
    if fit is None:
        # Perfect separation has no finite maximum-likelihood estimate,
        # and neither does a single-class window. Refusing is correct;
        # a slope invented for those cases would be the only number in
        # the report that came from nowhere.
        return {"status": "NOT_COMPUTED: degenerate fit "
                          "(single class, no spread, perfect separation, "
                          "or singular)",
                "slope": None, "intercept": None, "n": len(pairs),
                "reading": None}
    slope, intercept = fit
    return {
        "status": "COMPUTED",
        "slope": round(slope, 6),
        "intercept": round(intercept, 6),
        "n": len(pairs),
        "reading": ("overconfident" if slope < 0.9 else
                    "underconfident" if slope > 1.1 else
                    "slope near 1"),
    }


# ======================================================================
# Calibrators
# ======================================================================

@dataclass(frozen=True)
class CalibratorFit:
    """
    A fitted calibration map, with the window it was fitted on recorded.

    `fitted_rows` and `method` travel with it because a calibrator
    fitted on 30 observations and one fitted on 300 are not the same
    kind of claim, and because applying a calibrator without knowing
    where it came from is how future data leaks into past predictions.
    """
    method: str
    fitted_rows: int
    parameters: Dict[str, Any] = field(default_factory=dict)
    status: str = "FITTED"
    note: str = ""

    @property
    def is_usable(self) -> bool:
        return self.status == "FITTED"

    def apply(self, probabilities: Sequence[Optional[float]]
              ) -> List[Optional[float]]:
        """
        Map raw probabilities through this calibrator.

        A `None` in stays a `None` out -- an abstention is not something
        to calibrate. Every returned value is inside [0, 1].
        """
        if self.method == "raw" or not self.is_usable:
            return [None if p is None else _clip(float(p))
                    for p in probabilities]
        if self.method == "platt":
            slope = self.parameters["slope"]
            intercept = self.parameters["intercept"]
            return [None if p is None
                    else _clip(sigmoid(slope * logit(float(p)) + intercept))
                    for p in probabilities]
        if self.method == "isotonic":
            knots_x = self.parameters["x"]
            knots_y = self.parameters["y"]
            return [None if p is None
                    else _clip(_step_interpolate(float(p), knots_x, knots_y))
                    for p in probabilities]
        raise ValueError(f"unknown calibration method {self.method!r}")

    def describe(self) -> Dict[str, Any]:
        return {"method": self.method, "status": self.status,
                "fitted_rows": self.fitted_rows,
                "parameters": ({k: v for k, v in self.parameters.items()
                                if k in ("slope", "intercept")}
                               or {"knots": len(self.parameters.get("x", []))}),
                "note": self.note}


def _step_interpolate(probability: float, knots_x: Sequence[float],
                      knots_y: Sequence[float]) -> float:
    """Right-continuous step lookup, flat outside the fitted range."""
    if not knots_x:
        return probability
    if probability <= knots_x[0]:
        return knots_y[0]
    if probability >= knots_x[-1]:
        return knots_y[-1]
    low, high = 0, len(knots_x) - 1
    while low < high:
        middle = (low + high) // 2
        if knots_x[middle] < probability:
            low = middle + 1
        else:
            high = middle
    return knots_y[low]


def fit_identity() -> CalibratorFit:
    """The control: raw probabilities, untouched."""
    return CalibratorFit(method="raw", fitted_rows=0,
                         note="no calibration applied; the control arm")


def fit_platt(probabilities: Sequence[Optional[float]],
              outcomes: Sequence[Optional[float]],
              threshold: float = 0.0,
              min_rows: int = MIN_BIN_SUPPORT) -> CalibratorFit:
    """
    Platt scaling: `sigmoid(a * logit(p) + b)`, fitted by MLE.

    The same regression as `calibration_curve`, so the correction
    applied and the miscalibration diagnosed cannot disagree.
    """
    pairs = _pairs(probabilities, outcomes, threshold)
    if len(pairs) < min_rows:
        return CalibratorFit(method="platt", fitted_rows=len(pairs),
                             status="NOT_FITTED",
                             note=f"only {len(pairs)} usable rows; "
                                  f"{min_rows} required")
    fit = _fit_scalar_logistic([logit(p) for p, _ in pairs],
                               [y for _, y in pairs])
    if fit is None:
        return CalibratorFit(method="platt", fitted_rows=len(pairs),
                             status="NOT_FITTED",
                             note="degenerate fit (single class, no spread, "
                                  "or singular)")
    slope, intercept = fit
    return CalibratorFit(method="platt", fitted_rows=len(pairs),
                         parameters={"slope": slope, "intercept": intercept})


def fit_isotonic(probabilities: Sequence[Optional[float]],
                 outcomes: Sequence[Optional[float]],
                 threshold: float = 0.0,
                 min_rows: int = MIN_BIN_SUPPORT) -> CalibratorFit:
    """
    Isotonic regression by pool-adjacent-violators.

    Non-parametric, so it can fix a miscalibration no sigmoid can --
    and, being free to fit any monotone step function, it is the easier
    of the two to overfit on a thin calibration window. Both are
    reported; neither is preferred by this module.

    Output is a monotone step function with values in [0, 1] by
    construction, since it averages 0/1 targets.
    """
    pairs = sorted(_pairs(probabilities, outcomes, threshold),
                   key=lambda item: item[0])
    if len(pairs) < min_rows:
        return CalibratorFit(method="isotonic", fitted_rows=len(pairs),
                             status="NOT_FITTED",
                             note=f"only {len(pairs)} usable rows; "
                                  f"{min_rows} required")
    if len({y for _, y in pairs}) < 2:
        return CalibratorFit(method="isotonic", fitted_rows=len(pairs),
                             status="NOT_FITTED",
                             note="single outcome class in the calibration "
                                  "window")

    # Pool adjacent violators: each block holds (sum, count, right edge).
    blocks: List[List[float]] = []
    for probability, outcome in pairs:
        blocks.append([float(outcome), 1.0, probability])
        while len(blocks) > 1 and (blocks[-2][0] / blocks[-2][1]
                                   > blocks[-1][0] / blocks[-1][1]):
            merged_sum = blocks[-2][0] + blocks[-1][0]
            merged_count = blocks[-2][1] + blocks[-1][1]
            right_edge = blocks[-1][2]
            blocks.pop()
            blocks.pop()
            blocks.append([merged_sum, merged_count, right_edge])

    knots_x = [round(block[2], 8) for block in blocks]
    knots_y = [round(block[0] / block[1], 8) for block in blocks]
    return CalibratorFit(method="isotonic", fitted_rows=len(pairs),
                         parameters={"x": knots_x, "y": knots_y})


#: The calibration methods this phase may use. `raw` is the control arm,
#: not a method -- it exists so "calibration helped" is a measured
#: comparison rather than an assertion.
CALIBRATION_METHODS: Tuple[str, ...] = ("raw", "platt", "isotonic")


def fit_calibrator(method: str, probabilities: Sequence[Optional[float]],
                   outcomes: Sequence[Optional[float]],
                   threshold: float = 0.0) -> CalibratorFit:
    if method == "raw":
        return fit_identity()
    if method == "platt":
        return fit_platt(probabilities, outcomes, threshold)
    if method == "isotonic":
        return fit_isotonic(probabilities, outcomes, threshold)
    raise ValueError(f"unknown calibration method {method!r}")


# ======================================================================
# One report, both dimensions, never combined
# ======================================================================

def probability_report(probabilities: Sequence[Optional[float]],
                       outcomes: Sequence[Optional[float]], *,
                       base_rate: Optional[float] = None,
                       threshold: float = 0.0,
                       bin_count: int = 5) -> Dict[str, Any]:
    """
    Discrimination and calibration side by side for one window.

    `base_rate` is the probability a base-rate predictor would have
    emitted, and it must come from data available BEFORE this window --
    computing it from `outcomes` would be scoring the baseline on the
    answers.
    """
    pairs = _pairs(probabilities, outcomes, threshold)
    model_brier = brier_score(probabilities, outcomes, threshold)

    baseline_brier = None
    if base_rate is not None and pairs:
        baseline_brier = brier_score([base_rate] * len(pairs),
                                     [y for _, y in pairs], threshold=0.5)
    ece, ece_status = expected_calibration_error(
        probabilities, outcomes, threshold, bin_count)

    return {
        "n": len(pairs),
        "positive_rate": positive_rate(outcomes, threshold),
        "base_rate_used": base_rate,
        "discrimination": {
            "roc_auc": roc_auc(probabilities, outcomes, threshold),
            "balanced_accuracy": balanced_accuracy(probabilities, outcomes,
                                                   threshold),
            "accuracy": accuracy(probabilities, outcomes, threshold),
            "confusion": confusion(probabilities, outcomes, threshold),
        },
        "calibration": {
            "brier": model_brier,
            "base_rate_brier": baseline_brier,
            "brier_skill_score": brier_skill_score(model_brier,
                                                   baseline_brier),
            "log_loss": log_loss(probabilities, outcomes, threshold),
            "expected_calibration_error": ece,
            "ece_status": ece_status,
            "curve": calibration_curve(probabilities, outcomes, threshold),
            "reliability": [b.describe() for b in reliability_bins(
                probabilities, outcomes, threshold, bin_count)],
        },
    }
