"""
src/modeling/algorithms.py
-------------------------------
Baseline and simple interpretable models (Phase 9, spec §28, §36-§38).

PURE PYTHON, ZERO EXTERNAL DEPENDENCIES. Every other module in src/
has no third-party imports, and CI installs only what
requirements.txt lists. Pulling in numpy/scikit-learn for ridge
regression and logistic regression at this data scale would add
install weight and a failure surface for arithmetic that fits in fifty
lines. Spec §60's "no unnecessary complexity" applies to dependencies
too.

BASELINES ARE FIRST-CLASS, NOT AN AFTERTHOUGHT (spec §28, §29): they
are implemented here, alongside the real models, because a model
result without its baseline is uninterpretable. Making them equally
easy to run is what makes the comparison actually happen.

EVERY MODEL CAN ABSTAIN (spec §27). `predict_one` may return None,
which the engine records as an abstention rather than a guess. A model
forced to answer every question answers badly on the ones it has no
basis for.
"""

import math
import random
import statistics
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.domain.model_models import ModelFamily, PredictionTask


Row = Sequence[Optional[float]]


def _clean_rows(X: Sequence[Row], Y: Sequence[Optional[float]]) -> Tuple[List[List[float]], List[float]]:
    """
    Drop rows containing a missing feature or label.

    Missing values are NOT imputed here. Substituting a mean or zero
    would invent data the model then treats as observed — Phase 8's
    MissingPolicy exists precisely because "missing" and "zero" are
    different facts, and this module does not undo that distinction.
    """
    clean_X, clean_Y = [], []
    for row, label in zip(X, Y):
        if label is None or any(v is None for v in row):
            continue
        clean_X.append([float(v) for v in row])
        clean_Y.append(float(label))
    return clean_X, clean_Y


# ============================================================
# Baselines (spec §28)
# ============================================================

def fit_constant_baseline(X: Sequence[Row], Y: Sequence[Optional[float]],
                           constant: float = 0.0) -> Dict[str, Any]:
    """Always predicts a fixed value. The floor any real model must clear."""
    return {"family": ModelFamily.BASELINE_CONSTANT.value, "constant": constant}


def fit_historical_mean_baseline(X: Sequence[Row], Y: Sequence[Optional[float]]) -> Dict[str, Any]:
    """Always predicts the training mean — surprisingly hard to beat on financial returns."""
    _, clean_Y = _clean_rows(X, Y)
    mean = statistics.fmean(clean_Y) if clean_Y else 0.0
    return {"family": ModelFamily.BASELINE_HISTORICAL_MEAN.value, "constant": round(mean, 8)}


def fit_majority_class_baseline(X: Sequence[Row], Y: Sequence[Optional[float]],
                                 threshold: float = 0.0) -> Dict[str, Any]:
    """
    Always predicts the more common direction.

    The single most important classification baseline: if 53% of
    outcomes are 'up', a model scoring 54% has demonstrated almost
    nothing, and only this comparison makes that visible.
    """
    _, clean_Y = _clean_rows(X, Y)
    ups = sum(1 for y in clean_Y if y > threshold)
    downs = len(clean_Y) - ups
    majority = 1.0 if ups >= downs else 0.0
    base_rate = round(ups / len(clean_Y), 6) if clean_Y else 0.5
    return {"family": ModelFamily.BASELINE_MAJORITY_CLASS.value,
            "majority": majority, "base_rate": base_rate, "threshold": threshold}


def fit_random_baseline(X: Sequence[Row], Y: Sequence[Optional[float]], seed: int = 42) -> Dict[str, Any]:
    """Random guessing at the observed base rate — the true zero-information reference."""
    _, clean_Y = _clean_rows(X, Y)
    base_rate = round(sum(1 for y in clean_Y if y > 0) / len(clean_Y), 6) if clean_Y else 0.5
    return {"family": ModelFamily.BASELINE_RANDOM.value, "base_rate": base_rate, "seed": seed}


# ============================================================
# Ridge regression (spec §37)
# ============================================================

def fit_ridge(X: Sequence[Row], Y: Sequence[Optional[float]],
              alpha: float = 1.0, standardize: bool = True) -> Dict[str, Any]:
    """
    Ridge regression via the normal equations with L2 regularization.

    REGULARIZED BY DEFAULT (alpha=1.0, spec §46): with many features
    and few genuinely independent observations, unregularized least
    squares fits noise enthusiastically. Alpha is a hyperparameter, but
    zero is not the default.

    Standardization is applied by default and the scaling parameters
    are STORED, so prediction applies exactly the same transformation
    — a classic source of silent breakage when train and predict
    disagree.
    """
    clean_X, clean_Y = _clean_rows(X, Y)
    if len(clean_X) < 2:
        return {"family": ModelFamily.RIDGE_REGRESSION.value, "coefficients": [],
                 "intercept": 0.0, "insufficient_data": True}

    n_features = len(clean_X[0])
    means = [0.0] * n_features
    stds = [1.0] * n_features
    if standardize:
        for j in range(n_features):
            column = [row[j] for row in clean_X]
            means[j] = statistics.fmean(column)
            stds[j] = statistics.stdev(column) if len(column) > 1 and statistics.stdev(column) > 1e-12 else 1.0
        clean_X = [[(row[j] - means[j]) / stds[j] for j in range(n_features)] for row in clean_X]

    y_mean = statistics.fmean(clean_Y)
    centered_Y = [y - y_mean for y in clean_Y]

    # (X'X + alpha*I) beta = X'y  — solved by Gaussian elimination.
    XtX = [[sum(clean_X[k][i] * clean_X[k][j] for k in range(len(clean_X)))
            for j in range(n_features)] for i in range(n_features)]
    for i in range(n_features):
        XtX[i][i] += alpha
    Xty = [sum(clean_X[k][i] * centered_Y[k] for k in range(len(clean_X))) for i in range(n_features)]

    coefficients = _solve_linear_system(XtX, Xty)
    if coefficients is None:
        return {"family": ModelFamily.RIDGE_REGRESSION.value, "coefficients": [],
                 "intercept": round(y_mean, 8), "singular": True}

    return {
        "family": ModelFamily.RIDGE_REGRESSION.value,
        "coefficients": [round(c, 8) for c in coefficients],
        "intercept": round(y_mean, 8),
        "means": means, "stds": stds, "standardized": standardize, "alpha": alpha,
    }


def _solve_linear_system(A: List[List[float]], b: List[float]) -> Optional[List[float]]:
    """Gaussian elimination with partial pivoting. Returns None for a singular system rather than a garbage answer."""
    n = len(A)
    matrix = [row[:] + [b[i]] for i, row in enumerate(A)]
    for column in range(n):
        pivot_row = max(range(column, n), key=lambda r: abs(matrix[r][column]))
        if abs(matrix[pivot_row][column]) < 1e-12:
            return None
        matrix[column], matrix[pivot_row] = matrix[pivot_row], matrix[column]
        for row in range(column + 1, n):
            factor = matrix[row][column] / matrix[column][column]
            for k in range(column, n + 1):
                matrix[row][k] -= factor * matrix[column][k]

    solution = [0.0] * n
    for row in range(n - 1, -1, -1):
        total = matrix[row][n] - sum(matrix[row][k] * solution[k] for k in range(row + 1, n))
        solution[row] = total / matrix[row][row]
    return solution


# ============================================================
# Logistic regression (spec §37)
# ============================================================

def fit_logistic(X: Sequence[Row], Y: Sequence[Optional[float]],
                 threshold: float = 0.0, learning_rate: float = 0.1,
                 iterations: int = 300, l2: float = 0.01,
                 standardize: bool = True) -> Dict[str, Any]:
    """
    Binary logistic regression by gradient descent, L2-regularized.

    Labels are binarized at `threshold` (default 0.0 — i.e. "did the
    return exceed zero"). The threshold is stored, because a model
    trained on "beat zero" answers a different question from one
    trained on "beat 2%", and confusing the two silently changes what
    a prediction means.
    """
    clean_X, clean_Y = _clean_rows(X, Y)
    if len(clean_X) < 2:
        return {"family": ModelFamily.LOGISTIC_REGRESSION.value, "coefficients": [],
                 "intercept": 0.0, "insufficient_data": True, "threshold": threshold}

    n_features = len(clean_X[0])
    means = [0.0] * n_features
    stds = [1.0] * n_features
    if standardize:
        for j in range(n_features):
            column = [row[j] for row in clean_X]
            means[j] = statistics.fmean(column)
            deviation = statistics.stdev(column) if len(column) > 1 else 0.0
            stds[j] = deviation if deviation > 1e-12 else 1.0
        clean_X = [[(row[j] - means[j]) / stds[j] for j in range(n_features)] for row in clean_X]

    binary_Y = [1.0 if y > threshold else 0.0 for y in clean_Y]
    weights = [0.0] * n_features
    intercept = 0.0

    for _ in range(iterations):
        gradient_w = [0.0] * n_features
        gradient_b = 0.0
        for row, target in zip(clean_X, binary_Y):
            z = intercept + sum(w * x for w, x in zip(weights, row))
            prediction = 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, z))))
            error = prediction - target
            for j in range(n_features):
                gradient_w[j] += error * row[j]
            gradient_b += error
        size = len(clean_X)
        for j in range(n_features):
            weights[j] -= learning_rate * (gradient_w[j] / size + l2 * weights[j])
        intercept -= learning_rate * (gradient_b / size)

    return {
        "family": ModelFamily.LOGISTIC_REGRESSION.value,
        "coefficients": [round(w, 8) for w in weights],
        "intercept": round(intercept, 8),
        "means": means, "stds": stds, "standardized": standardize,
        "threshold": threshold, "l2": l2,
    }


# ============================================================
# Prediction
# ============================================================

def predict_one(parameters: Dict[str, Any], row: Row) -> Optional[float]:
    """
    Apply a fitted model to one row.

    Returns None — an ABSTENTION (spec §27) — when the model cannot
    honestly answer: a missing feature, an untrained model, or a
    singular fit. Guessing instead would produce a number the caller
    could not distinguish from a real prediction.
    """
    family = parameters.get("family")

    if family in (ModelFamily.BASELINE_CONSTANT.value, ModelFamily.BASELINE_HISTORICAL_MEAN.value):
        return parameters.get("constant", 0.0)

    if family == ModelFamily.BASELINE_MAJORITY_CLASS.value:
        return parameters.get("majority", 0.0)

    if family == ModelFamily.BASELINE_RANDOM.value:
        generator = random.Random(parameters.get("seed", 42))
        return 1.0 if generator.random() < parameters.get("base_rate", 0.5) else 0.0

    if parameters.get("insufficient_data") or parameters.get("singular"):
        return None
    if any(v is None for v in row):
        return None

    coefficients = parameters.get("coefficients") or []
    if len(coefficients) != len(row):
        return None

    values = [float(v) for v in row]
    if parameters.get("standardized"):
        means, stds = parameters.get("means", []), parameters.get("stds", [])
        if len(means) == len(values) and len(stds) == len(values):
            values = [(values[j] - means[j]) / stds[j] for j in range(len(values))]

    z = parameters.get("intercept", 0.0) + sum(c * v for c, v in zip(coefficients, values))

    if family == ModelFamily.LOGISTIC_REGRESSION.value:
        return 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, z))))
    return z


def predict_batch(parameters: Dict[str, Any], X: Sequence[Row]) -> List[Optional[float]]:
    """Apply a model across rows. Abstentions come back as None, never as a filler value."""
    return [predict_one(parameters, row) for row in X]


#: Registry of fitters, so the engine never branches on family.
FITTERS: Dict[ModelFamily, Callable[..., Dict[str, Any]]] = {
    ModelFamily.BASELINE_CONSTANT: fit_constant_baseline,
    ModelFamily.BASELINE_HISTORICAL_MEAN: fit_historical_mean_baseline,
    ModelFamily.BASELINE_MAJORITY_CLASS: fit_majority_class_baseline,
    ModelFamily.BASELINE_RANDOM: fit_random_baseline,
    ModelFamily.RIDGE_REGRESSION: fit_ridge,
    ModelFamily.LINEAR_REGRESSION: lambda X, Y, **kw: fit_ridge(X, Y, alpha=kw.pop("alpha", 0.0), **kw),
    ModelFamily.LOGISTIC_REGRESSION: fit_logistic,
}


def fit(family: ModelFamily, X: Sequence[Row], Y: Sequence[Optional[float]],
        **hyperparameters) -> Dict[str, Any]:
    """Fit any supported family. Raises for an unsupported one rather than silently substituting a baseline."""
    fitter = FITTERS.get(family)
    if fitter is None:
        raise ValueError(f"unsupported model family: {family}")
    return fitter(X, Y, **hyperparameters)
