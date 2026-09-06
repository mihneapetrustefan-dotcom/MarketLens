"""
src/experiments/evaluators.py
--------------------------------------
The registered arms an experiment may use, and the baseline registry.

SECURITY: A REGISTRY, NOT A CODE PATH (§80)
-----------------------------------------------
An arm names an evaluator by STRING and passes a dict of parameters.
There is no callable field, no `eval`, no import-by-name, no path to
arbitrary execution. Asking for an unregistered evaluator raises;
asking for an unknown parameter raises.

That is the structural answer to §80. A configuration format that
cannot express code cannot be made to execute code, however the
configuration arrives.

WHAT AN EVALUATOR DOES
--------------------------
It takes a cohort of Phase 21 experiences and returns `ArmMetrics`. It
does not fetch, train, promote or write anything — the engine owns the
data, the split and the persistence, and an evaluator that could reach
past its cohort would be able to see the test set.

WHAT IS RUNNABLE HERE, AND WHAT IS NOT
------------------------------------------
Measured against the production database:

  SIGNAL     runnable — 1,823 signal experiences with strength spanning
             0.0 to 1.0 and hit rates from 0.43 to 0.60
  REGIME     declared, cannot run — `market_regime` is NULL throughout
  MODEL      declared, cannot run — one model family, none promoted
  STRATEGY   declared, delegates to Phase 12's backtester; no strategy
             configuration exists to backtest yet
  EXECUTION  declared, cannot run — no order has ever been placed
  PORTFOLIO  declared, cannot run — no portfolio or position exists

Every one is registered and every one states what it needs. An
evaluator that silently returned an empty cohort would look like a
clean result; one that says "market_regime is NULL on every row" is a
finding.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.domain.experiment_models import ArmMetrics, ArmSpec


class EvaluatorError(Exception):
    """Raised when an arm cannot be evaluated. Never silently empty."""


class UnknownEvaluator(EvaluatorError):
    """
    An arm named an evaluator that is not registered.

    Deliberately loud. The alternative — falling back to a default —
    would let a typo silently change what an experiment tested.
    """


@dataclass
class EvaluatorSpec:
    """What an evaluator is, what it needs, and whether it can run here."""
    name: str
    description: str
    parameters: Tuple[str, ...]
    requires: Tuple[str, ...] = ()
    runnable: bool = True
    unavailable_reason: str = ""


_REGISTRY: Dict[str, Tuple[EvaluatorSpec, Callable]] = {}


def register(spec: EvaluatorSpec):
    def decorator(function):
        _REGISTRY[spec.name] = (spec, function)
        return function
    return decorator


def get(name: str) -> Tuple[EvaluatorSpec, Callable]:
    if name not in _REGISTRY:
        raise UnknownEvaluator(
            f"No evaluator named {name!r}. Registered: "
            + ", ".join(sorted(_REGISTRY))
            + ". Evaluators are named, never supplied as code.")
    return _REGISTRY[name]


def registered() -> List[EvaluatorSpec]:
    return [spec for spec, _ in _REGISTRY.values()]


def validate_parameters(spec: EvaluatorSpec, parameters: Dict[str, Any]) -> None:
    """
    Refuse unknown parameters.

    A silently ignored parameter is worse than an error: the experiment
    would record that it tested `threshold=0.7` while having tested
    nothing of the kind.
    """
    unknown = set(parameters) - set(spec.parameters)
    if unknown:
        raise EvaluatorError(
            f"{spec.name} does not accept {sorted(unknown)}. Accepted: "
            f"{sorted(spec.parameters)}. An ignored parameter would make the "
            f"experiment record a change it did not make.")


def summarise(rows: Sequence[Dict[str, Any]], label: str = "") -> ArmMetrics:
    """
    Turn a cohort into metrics.

    Neutrals are excluded from directional accuracy for the reason
    Phases 19 and 21 both gave: a market that did not move is not
    evidence for or against a directional claim. They are counted
    separately so the exclusion stays visible.
    """
    metrics = ArmMetrics(label=label, sample_size=len(rows))
    for row in rows:
        result = row.get("direction_result")
        if result == "hit":
            metrics.hits += 1
        elif result == "miss":
            metrics.misses += 1
        elif result == "neutral":
            metrics.neutrals += 1
    if metrics.decided:
        metrics.directional_accuracy = metrics.hits / metrics.decided

    returns = [r["actual_return"] for r in rows if r.get("actual_return") is not None]
    if returns:
        metrics.mean_return = sum(returns) / len(returns)
        metrics.median_return = statistics.median(returns)
        metrics.stdev_return = (statistics.pstdev(returns)
                                if len(returns) > 1 else None)
    mfes = [r["mfe"] for r in rows if r.get("mfe") is not None]
    maes = [r["mae"] for r in rows if r.get("mae") is not None]
    metrics.mean_mfe = (sum(mfes) / len(mfes)) if mfes else None
    metrics.mean_mae = (sum(maes) / len(maes)) if maes else None
    metrics.instrument_count = len({r.get("instrument_id") for r in rows
                                    if r.get("instrument_id")})
    return metrics


def _numeric(value: Any, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise EvaluatorError(f"{name} must be a number, got {value!r}")


# ======================================================================
# Runnable: signal cohort filters
# ======================================================================

@register(EvaluatorSpec(
    name="signal_all",
    description=("Every signal experience in the cohort. The natural "
                 "control for any signal filter — 'what if we kept "
                 "everything'."),
    parameters=("subject_kind",)))
def signal_all(rows: Sequence[Dict[str, Any]],
               parameters: Dict[str, Any]) -> Tuple[ArmMetrics, List[Dict[str, Any]]]:
    kind = parameters.get("subject_kind", "signal")
    selected = [r for r in rows if r.get("subject_kind") == kind]
    return summarise(selected, "all"), selected


@register(EvaluatorSpec(
    name="signal_strength_threshold",
    description=("Signals whose strength is at or above a threshold. "
                 "The candidate for 'does filtering weak signals help'."),
    parameters=("threshold", "subject_kind")))
def signal_strength_threshold(rows, parameters):
    threshold = _numeric(parameters.get("threshold", 0.5), "threshold")
    kind = parameters.get("subject_kind", "signal")
    selected = [r for r in rows
                if r.get("subject_kind") == kind
                and r.get("signal_strength") is not None
                and r["signal_strength"] >= threshold]
    return summarise(selected, f"strength>={threshold:g}"), selected


@register(EvaluatorSpec(
    name="signal_confidence_threshold",
    description="Signals whose confidence score is at or above a threshold.",
    parameters=("threshold", "subject_kind")))
def signal_confidence_threshold(rows, parameters):
    threshold = _numeric(parameters.get("threshold", 0.3), "threshold")
    kind = parameters.get("subject_kind", "signal")
    selected = [r for r in rows
                if r.get("subject_kind") == kind
                and r.get("signal_confidence") is not None
                and r["signal_confidence"] >= threshold]
    return summarise(selected, f"confidence>={threshold:g}"), selected


@register(EvaluatorSpec(
    name="signal_direction",
    description="Signals in one direction only.",
    parameters=("direction", "subject_kind")))
def signal_direction(rows, parameters):
    direction = str(parameters.get("direction", "long")).lower()
    kind = parameters.get("subject_kind", "signal")
    selected = [r for r in rows
                if r.get("subject_kind") == kind
                and str(r.get("expected_direction") or "").lower() == direction]
    return summarise(selected, direction), selected


@register(EvaluatorSpec(
    name="signal_event_filter",
    description=("Signals arising from one event type. The candidate for "
                 "'does this event class behave differently'."),
    parameters=("event_type", "subject_kind")))
def signal_event_filter(rows, parameters):
    event = parameters.get("event_type")
    kind = parameters.get("subject_kind", "signal")
    selected = [r for r in rows
                if r.get("subject_kind") == kind
                and r.get("event_type") == event]
    return summarise(selected, f"event={event}"), selected


@register(EvaluatorSpec(
    name="signal_horizon",
    description="Signals measured at one horizon.",
    parameters=("horizon", "subject_kind")))
def signal_horizon(rows, parameters):
    horizon = parameters.get("horizon")
    kind = parameters.get("subject_kind", "signal")
    selected = [r for r in rows
                if r.get("subject_kind") == kind and r.get("horizon") == horizon]
    return summarise(selected, f"horizon={horizon}"), selected


@register(EvaluatorSpec(
    name="signal_composite",
    description=("Several filters at once. Use only when an experiment "
                 "deliberately changes more than one variable, and say so "
                 "in the description (§8)."),
    parameters=("threshold", "confidence_min", "direction", "event_type",
                "horizon", "subject_kind")))
def signal_composite(rows, parameters):
    kind = parameters.get("subject_kind", "signal")
    selected = list(rows)
    labels = []
    selected = [r for r in selected if r.get("subject_kind") == kind]
    if parameters.get("threshold") is not None:
        threshold = _numeric(parameters["threshold"], "threshold")
        selected = [r for r in selected if (r.get("signal_strength") or -1) >= threshold]
        labels.append(f"strength>={threshold:g}")
    if parameters.get("confidence_min") is not None:
        floor = _numeric(parameters["confidence_min"], "confidence_min")
        selected = [r for r in selected if (r.get("signal_confidence") or -1) >= floor]
        labels.append(f"confidence>={floor:g}")
    if parameters.get("direction"):
        direction = str(parameters["direction"]).lower()
        selected = [r for r in selected
                    if str(r.get("expected_direction") or "").lower() == direction]
        labels.append(direction)
    if parameters.get("event_type"):
        selected = [r for r in selected
                    if r.get("event_type") == parameters["event_type"]]
        labels.append(f"event={parameters['event_type']}")
    if parameters.get("horizon"):
        selected = [r for r in selected if r.get("horizon") == parameters["horizon"]]
        labels.append(f"horizon={parameters['horizon']}")
    return summarise(selected, " & ".join(labels) or "composite"), selected


# ======================================================================
# Declared, and honest about why they cannot run here
# ======================================================================

def _unavailable(spec: EvaluatorSpec):
    """
    An evaluator whose inputs do not exist.

    It raises rather than returning an empty cohort. An empty cohort
    would flow through the engine, produce a sample size of zero, and be
    reported as INCONCLUSIVE — which reads as "we tested and could not
    tell" rather than "we could not test".
    """
    def run(rows, parameters):
        raise EvaluatorError(
            f"{spec.name} cannot run: {spec.unavailable_reason} "
            f"It is registered so the experiment type exists and so this "
            f"gap is visible; it will work when its inputs do.")
    return run


for _spec in (
    EvaluatorSpec(
        name="regime_filter",
        description="Experiences within one market regime (§28).",
        parameters=("regime", "subject_kind"),
        requires=("trading_experiences.market_regime",),
        runnable=False,
        unavailable_reason=("`market_regime` is NULL on every experience in "
                            "the production record, so no regime cohort can "
                            "be formed.")),
    EvaluatorSpec(
        name="model_comparison",
        description="Experiences produced by one trained model (§25).",
        parameters=("trained_model_id", "subject_kind"),
        requires=("trained_models with more than one promoted family",),
        runnable=False,
        unavailable_reason=("only one model family exists and none has been "
                            "promoted, so a model comparison would compare a "
                            "model against itself.")),
    EvaluatorSpec(
        name="strategy_backtest",
        description=("Delegates to Phase 12's `BacktestEngine` (§36). This "
                     "phase does NOT contain a second backtester."),
        parameters=("strategy_id", "configuration"),
        requires=("a strategy configuration and a backtest run",),
        runnable=False,
        unavailable_reason=("no strategy configuration exists to backtest, "
                            "and Phase 12's backtest tables are absent from "
                            "the production database.")),
    EvaluatorSpec(
        name="execution_policy",
        description="Compares execution policies on realised fills (§30).",
        parameters=("policy",),
        requires=("execution_fills", "order_intents"),
        runnable=False,
        unavailable_reason=("no order has ever been placed, so there is no "
                            "fill, slippage or latency to compare.")),
    EvaluatorSpec(
        name="portfolio_allocation",
        description="Compares allocation rules (§29).",
        parameters=("rule", "max_concentration"),
        requires=("portfolios", "positions"),
        runnable=False,
        unavailable_reason=("no portfolio or position exists.")),
    EvaluatorSpec(
        name="risk_policy",
        description=("Evaluates an alternative risk policy. EVALUATION "
                     "ONLY — §31 forbids deploying one."),
        parameters=("policy", "max_position"),
        requires=("risk_decisions",),
        runnable=False,
        unavailable_reason=("no risk decision record exists.")),
):
    _REGISTRY[_spec.name] = (_spec, _unavailable(_spec))


# ======================================================================
# Baseline registry (§54)
# ======================================================================

#: Reusable baseline definitions, so baseline logic is not recreated in
#: every experiment — and so a baseline cannot be quietly chosen to
#: make a candidate look good (§6).
BASELINES: Dict[str, ArmSpec] = {
    "all_signals": ArmSpec(
        name="all signals",
        evaluator="signal_all",
        parameters={"subject_kind": "signal"},
        description=("Every signal, unfiltered. The honest control for any "
                     "filter: it answers 'compared with keeping everything'."),
        complexity=1),
    "all_predictions": ArmSpec(
        name="all predictions",
        evaluator="signal_all",
        parameters={"subject_kind": "prediction"},
        description="Every prediction, unfiltered.",
        complexity=1),
    "long_only": ArmSpec(
        name="long only",
        evaluator="signal_direction",
        parameters={"direction": "long", "subject_kind": "signal"},
        description="Directional control for a long-side hypothesis.",
        complexity=1),
    "short_only": ArmSpec(
        name="short only",
        evaluator="signal_direction",
        parameters={"direction": "short", "subject_kind": "signal"},
        description="Directional control for a short-side hypothesis.",
        complexity=1),
    "coin_flip": ArmSpec(
        name="coin flip",
        evaluator="signal_all",
        parameters={"subject_kind": "signal"},
        description=("The all-signals cohort, read against 50% directional "
                     "accuracy. The weakest defensible baseline and the one "
                     "a signal layer must clear before anything else is "
                     "interesting."),
        complexity=0),
}


def baseline(name: str) -> ArmSpec:
    if name not in BASELINES:
        raise EvaluatorError(
            f"No baseline named {name!r}. Registered: "
            + ", ".join(sorted(BASELINES))
            + ". A baseline invented for one experiment is a baseline chosen "
              "to flatter it.")
    spec = BASELINES[name]
    # Returned as a copy so a caller cannot mutate the registry and
    # silently change the control of every later experiment (§83).
    return ArmSpec(name=spec.name, evaluator=spec.evaluator,
                   parameters=dict(spec.parameters),
                   description=spec.description, complexity=spec.complexity)


def evaluate(arm: ArmSpec, rows: Sequence[Dict[str, Any]]
             ) -> Tuple[ArmMetrics, List[Dict[str, Any]]]:
    """Run one arm over one cohort. The engine owns everything else."""
    spec, function = get(arm.evaluator)
    validate_parameters(spec, arm.parameters)
    return function(rows, arm.parameters)
