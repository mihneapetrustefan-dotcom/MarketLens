"""
src/attribution/detectors.py
--------------------------------------
One deterministic rule per error type. Each cites numbers or stays
silent.

THE CONTRACT EVERY DETECTOR HONOURS
---------------------------------------
    inputs present, rule matched      -> fired=True, with evidence
    inputs present, rule not matched  -> fired=False, judgeable
    inputs absent                     -> INSUFFICIENT_EVIDENCE + `missing`

The third case is the one that matters. "I looked and found nothing"
and "I could not look" are different facts, and merging them makes an
unmeasurable layer indistinguishable from a clean one — the most
flattering possible lie for a diagnostic system to tell.

NO DETECTOR INFERS FROM THE RESULT
--------------------------------------
None of these rules take "the outcome was bad" as evidence for
anything. A loss is an input to the question, never an answer to it.
Concretely, and each is asserted by an adversarial test:

  * a losing trade does not imply PREDICTION_ERROR — it implies a
    prediction whose direction can be checked, and the check is what
    fires;
  * a missing price does not imply DATA_ERROR — it implies the outcome
    could not be measured, which Phase 19 already recorded;
  * a risk rejection does not imply RISK_ERROR — a block that prevented
    a loss did its job;
  * execution is never blamed for a wrong direction.

DETERMINISM (§42, §65)
--------------------------
Pure arithmetic over stored numbers. No clock, no randomness, no
network, no LLM. Same inputs, same methodology version, byte-identical
output.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from src.domain.attribution_models import (
    HORIZON_RESCUE_RETURN, MAGNITUDE_OVERSHOOT_RATIO,
    MAGNITUDE_SHORTFALL_RATIO, MIN_COHORT_SAMPLE, NEUTRAL_BAND,
    TIMING_CAPTURE_RATIO, TIMING_MFE_FLOOR, AttributionConfidence,
    DetectorResult, ErrorType, Evidence, Severity,
)


def _severity_for(magnitude: Optional[float]) -> Severity:
    """
    How much a deviation mattered, by size alone.

    Deliberately crude and deliberately separate from confidence: this
    says "how big", not "how sure". A 12% adverse move matters whether
    or not we know why.
    """
    if magnitude is None:
        return Severity.INFO
    size = abs(magnitude)
    if size >= 0.10:
        return Severity.CRITICAL
    if size >= 0.05:
        return Severity.HIGH
    if size >= 0.02:
        return Severity.MEDIUM
    if size >= NEUTRAL_BAND:
        return Severity.LOW
    return Severity.INFO


def _missing(error_type: ErrorType, what: str, table: str) -> DetectorResult:
    """The honest empty answer, naming exactly what was absent."""
    return DetectorResult(
        error_type=error_type,
        confidence=AttributionConfidence.INSUFFICIENT_EVIDENCE,
        missing=table,
        summary=f"cannot be judged: {what}",
        evidence=[Evidence(
            kind="missing_input",
            statement=f"{what}. No attribution is made rather than a guess.",
            source=table)])


# ======================================================================
# Layers with evidence
# ======================================================================

def detect_prediction_error(outcome: Dict[str, Any]) -> DetectorResult:
    """
    Was the direction wrong at the stated horizon (§6, §7)?

    Fires only on an explicit `miss` from Phase 19, which already
    applied the neutral band — so a move too small to mean anything
    cannot produce a prediction error here (§7).

    It says the direction was wrong. It does NOT say the model is
    faulty: one wrong call is not a verdict on a model, and the
    model-level view is what the profiles in `analytics.py` are for.
    """
    if outcome.get("status") != "available":
        return _missing(ErrorType.PREDICTION_ERROR,
                        "the outcome was never measured",
                        "outcome_measurements.status")
    result = outcome.get("direction_result")
    if result is None:
        return _missing(ErrorType.PREDICTION_ERROR,
                        "no directional verdict was recorded",
                        "outcome_measurements.direction_result")

    realized = outcome.get("simple_return")
    expected_direction = outcome.get("expected_direction") or ""

    if result == "miss":
        return DetectorResult(
            error_type=ErrorType.PREDICTION_ERROR, fired=True,
            confidence=AttributionConfidence.HIGH,
            severity=_severity_for(realized),
            summary=(f"claimed {expected_direction}, the market moved "
                     f"{realized:+.2%} over {outcome.get('horizon')}"),
            evidence=[
                Evidence(kind="direction", source="outcome_measurements",
                         statement=(f"expected {expected_direction}, realised "
                                    f"{outcome.get('realized_direction')}"),
                         value=realized),
                Evidence(kind="neutral_band", source="domain.outcome_models",
                         statement=(f"the move cleared the {NEUTRAL_BAND:.1%} "
                                    f"neutral band, so it is a real "
                                    f"disagreement rather than noise"),
                         value=abs(realized) if realized is not None else None,
                         comparison=NEUTRAL_BAND),
            ])

    # Judged, and no directional error. That is a finding, not a gap.
    return DetectorResult(
        error_type=ErrorType.PREDICTION_ERROR, fired=False,
        confidence=AttributionConfidence.HIGH,
        summary=f"direction was {result}")


def detect_magnitude_error(outcome: Dict[str, Any]) -> DetectorResult:
    """
    Direction right, size badly estimated (§8).

    Explicitly NOT the same finding as a wrong direction. A model that
    calls +5% and delivers +0.2% understood the sign and not the scale,
    and the remedy differs entirely from the remedy for a wrong sign.

    Fires in both directions — a wild overshoot is a calibration
    failure too, and only counting shortfalls would bias the profile.
    """
    if outcome.get("status") != "available":
        return _missing(ErrorType.MAGNITUDE_ERROR,
                        "the outcome was never measured",
                        "outcome_measurements.status")
    expected = outcome.get("expected_return")
    realized = outcome.get("simple_return")
    if expected is None or realized is None:
        return _missing(ErrorType.MAGNITUDE_ERROR,
                        "no expected return was recorded for this subject",
                        "outcome_measurements.expected_return")
    if abs(expected) < NEUTRAL_BAND:
        return _missing(
            ErrorType.MAGNITUDE_ERROR,
            f"the expected move ({expected:+.4f}) is inside the neutral band, "
            f"so there is no magnitude claim to be wrong about",
            "outcome_measurements.expected_return")

    if outcome.get("direction_result") != "hit":
        # Magnitude is only meaningful once the sign was right.
        # Otherwise every wrong call would also be a magnitude error and
        # the two would stop distinguishing anything.
        return DetectorResult(
            error_type=ErrorType.MAGNITUDE_ERROR, fired=False,
            confidence=AttributionConfidence.HIGH,
            summary="direction was not correct, so magnitude is not the story")

    ratio = abs(realized) / abs(expected)
    if ratio < MAGNITUDE_SHORTFALL_RATIO:
        kind, detail = "shortfall", (
            f"realised {ratio:.0%} of the expected move")
    elif ratio > MAGNITUDE_OVERSHOOT_RATIO:
        kind, detail = "overshoot", (
            f"realised {ratio:.1f}x the expected move")
    else:
        return DetectorResult(
            error_type=ErrorType.MAGNITUDE_ERROR, fired=False,
            confidence=AttributionConfidence.HIGH,
            summary=f"magnitude within tolerance ({ratio:.0%} of expected)")

    return DetectorResult(
        error_type=ErrorType.MAGNITUDE_ERROR, fired=True,
        confidence=AttributionConfidence.HIGH,
        severity=_severity_for(realized - expected),
        summary=f"direction correct, magnitude {kind}: {detail}",
        evidence=[
            Evidence(kind="magnitude", source="outcome_measurements",
                     statement=(f"expected {expected:+.2%}, realised "
                                f"{realized:+.2%} — {detail}"),
                     value=realized, comparison=expected,
                     detail={"ratio": round(ratio, 4), "kind": kind}),
            Evidence(kind="direction", source="outcome_measurements",
                     statement="the direction was correct; only the size was "
                               "misjudged, which is a calibration finding "
                               "rather than a directional one"),
        ])


def detect_horizon_mismatch(outcome: Dict[str, Any],
                            siblings: Sequence[Dict[str, Any]]) -> DetectorResult:
    """
    Wrong at the stated horizon, right later (§9).

    Needs the same subject measured at other horizons — which Phase 19
    provides, seven per subject. Without siblings there is nothing to
    compare and the answer is INSUFFICIENT_EVIDENCE, not "no mismatch".

    §9 is explicit that this must not be misclassified as pure
    prediction failure. It is a statement about the *window*, and the
    remedy is a different horizon rather than a different model.
    """
    if outcome.get("direction_result") != "miss":
        return DetectorResult(
            error_type=ErrorType.HORIZON_MISMATCH, fired=False,
            confidence=AttributionConfidence.HIGH,
            summary="the stated horizon was not a miss")

    later = [row for row in siblings
             if row.get("status") == "available"
             and row.get("direction_result") == "hit"
             and (row.get("horizon_sort") or 0) > (outcome.get("horizon_sort") or 0)
             and row.get("simple_return") is not None]
    if not siblings:
        return _missing(ErrorType.HORIZON_MISMATCH,
                        "this subject was measured at only one horizon",
                        "outcome_measurements.horizon")
    if not later:
        return DetectorResult(
            error_type=ErrorType.HORIZON_MISMATCH, fired=False,
            confidence=AttributionConfidence.HIGH,
            summary="no longer horizon vindicated the call")

    # The rescue must be worth having. Without a floor, a two-basis-point
    # drift at 10d would "rescue" every 1d miss and the finding would
    # mean nothing.
    strong = [row for row in later
              if abs(row["simple_return"]) >= HORIZON_RESCUE_RETURN]
    if not strong:
        best = max(later, key=lambda r: abs(r["simple_return"]))
        return DetectorResult(
            error_type=ErrorType.HORIZON_MISMATCH, fired=False,
            confidence=AttributionConfidence.MEDIUM,
            summary=(f"a later horizon was directionally right but only by "
                     f"{best['simple_return']:+.2%}, under the "
                     f"{HORIZON_RESCUE_RETURN:.0%} floor"))

    rescue = max(strong, key=lambda r: abs(r["simple_return"]))
    return DetectorResult(
        error_type=ErrorType.HORIZON_MISMATCH, fired=True,
        confidence=AttributionConfidence.HIGH,
        severity=_severity_for(rescue["simple_return"]),
        summary=(f"wrong at {outcome.get('horizon')} "
                 f"({outcome.get('simple_return'):+.2%}) but correct at "
                 f"{rescue['horizon']} ({rescue['simple_return']:+.2%})"),
        evidence=[
            Evidence(kind="horizon", source="outcome_measurements",
                     statement=(f"at {outcome.get('horizon')} the move was "
                                f"{outcome.get('simple_return'):+.2%}, against "
                                f"the claim"),
                     value=outcome.get("simple_return")),
            Evidence(kind="horizon", source="outcome_measurements",
                     statement=(f"at {rescue['horizon']} the move was "
                                f"{rescue['simple_return']:+.2%}, with the claim"),
                     value=rescue["simple_return"],
                     detail={"horizon": rescue["horizon"]}),
            Evidence(kind="interpretation", source="attribution.detectors",
                     statement="the direction was eventually right; the stated "
                               "window was wrong. This is a horizon finding, "
                               "not a directional one."),
        ])


def detect_timing_error(outcome: Dict[str, Any]) -> DetectorResult:
    """
    The favourable move happened and was not captured (§10).

    Both conditions are required, and both matter:

      * the excursion was worth having (MFE above a floor) — a 0.3%
        blip is not a missed opportunity;
      * the close kept little of it (capture ratio) — an MFE barely
        above the close is not bad timing, it is a normal path.

    `time_to_mfe_seconds` sharpens it: an MFE on the reference bar
    itself means the move was already over when the signal spoke, which
    is a different problem from a move that came and went mid-window.
    """
    if outcome.get("status") != "available":
        return _missing(ErrorType.TIMING_ERROR,
                        "the outcome was never measured",
                        "outcome_measurements.status")
    mfe = outcome.get("mfe")
    realized = outcome.get("simple_return")
    if mfe is None or realized is None:
        return _missing(
            ErrorType.TIMING_ERROR,
            "no favourable excursion was computed (the bars carried no "
            "high/low), so timing cannot be assessed",
            "outcome_measurements.mfe")

    direction = (outcome.get("expected_direction") or "").lower()
    # Signed the way the position would have experienced it, so a short
    # that fell is a gain. Without this the capture ratio is meaningless
    # for half the book.
    captured = realized if direction == "long" else -realized

    if mfe < TIMING_MFE_FLOOR:
        return DetectorResult(
            error_type=ErrorType.TIMING_ERROR, fired=False,
            confidence=AttributionConfidence.HIGH,
            summary=(f"the favourable excursion never exceeded "
                     f"{TIMING_MFE_FLOOR:.0%}; there was nothing to capture"))

    ratio = captured / mfe if mfe else 0.0
    if ratio >= TIMING_CAPTURE_RATIO:
        return DetectorResult(
            error_type=ErrorType.TIMING_ERROR, fired=False,
            confidence=AttributionConfidence.HIGH,
            summary=f"kept {ratio:.0%} of the favourable excursion")

    seconds = outcome.get("time_to_mfe_seconds")
    at_entry = seconds is not None and seconds <= 0
    note = ("the favourable extreme occurred on the reference bar itself — "
            "the move was already over when the signal spoke"
            if at_entry else
            "the favourable move occurred inside the window and was given back")

    return DetectorResult(
        error_type=ErrorType.TIMING_ERROR, fired=True,
        # MEDIUM, not HIGH. This layer has no orders and no exit rule, so
        # "not captured" is inferred from the path rather than observed
        # from a fill. The evidence is real; the interpretation is one of
        # several.
        confidence=AttributionConfidence.MEDIUM,
        severity=_severity_for(mfe - captured),
        summary=(f"a {mfe:+.2%} favourable excursion was available; the close "
                 f"kept {captured:+.2%} ({ratio:.0%})"),
        evidence=[
            Evidence(kind="excursion", source="outcome_measurements.mfe",
                     statement=f"maximum favourable excursion {mfe:+.2%}",
                     value=mfe, comparison=TIMING_MFE_FLOOR),
            Evidence(kind="capture", source="outcome_measurements.simple_return",
                     statement=(f"the close realised {captured:+.2%}, "
                                f"{ratio:.0%} of what was offered"),
                     value=captured, comparison=mfe,
                     detail={"capture_ratio": round(ratio, 4)}),
            Evidence(kind="timing", source="outcome_measurements.time_to_mfe_seconds",
                     statement=note, value=seconds),
            Evidence(kind="caveat", source="attribution.detectors",
                     statement="no order or exit rule exists in this database, "
                               "so this describes the price path, not a "
                               "decision that was actually taken"),
        ])


def detect_signal_error(outcome: Dict[str, Any],
                        signal: Optional[Dict[str, Any]]) -> DetectorResult:
    """
    Did the prediction-to-signal translation lose something (§11)?

    The case §11 describes: the model predicted a direction, the signal
    layer emitted something else or suppressed it, and the market then
    moved the way the model said.

    Suppression is the version this database can actually see. It is
    reported as a signal-layer finding ONLY when the suppressed claim
    turned out to be right and worth having — a suppression that
    avoided a loss is the rule working, and labelling it an error would
    punish the system for being careful (§13's logic, applied here).
    """
    if signal is None:
        return _missing(ErrorType.SIGNAL_ERROR,
                        "no signal record accompanies this outcome",
                        "signals")
    if outcome.get("status") != "available":
        return _missing(ErrorType.SIGNAL_ERROR,
                        "the outcome was never measured",
                        "outcome_measurements.status")

    status = (signal.get("status") or "").lower()
    realized = outcome.get("simple_return")
    result = outcome.get("direction_result")

    if status not in ("suppressed", "rejected", "expired"):
        return DetectorResult(
            error_type=ErrorType.SIGNAL_ERROR, fired=False,
            confidence=AttributionConfidence.HIGH,
            summary=f"the signal was {status or 'active'}; nothing was withheld")

    if result != "hit" or realized is None:
        return DetectorResult(
            error_type=ErrorType.SIGNAL_ERROR, fired=False,
            confidence=AttributionConfidence.HIGH,
            summary=(f"the signal was {status} and the claim was not "
                     f"vindicated — withholding it cost nothing"))

    if abs(realized) < HORIZON_RESCUE_RETURN:
        return DetectorResult(
            error_type=ErrorType.SIGNAL_ERROR, fired=False,
            confidence=AttributionConfidence.MEDIUM,
            summary=(f"the {status} signal was directionally right but only by "
                     f"{realized:+.2%}; too small to call the suppression a loss"))

    reason = signal.get("suppression_note") or "no reason recorded"
    return DetectorResult(
        error_type=ErrorType.SIGNAL_ERROR, fired=True,
        # MEDIUM: the suppression may still have been correct policy on
        # the information available at the time. Hindsight is not a
        # verdict on a rule.
        confidence=AttributionConfidence.MEDIUM,
        severity=_severity_for(realized),
        summary=(f"a {status} signal was directionally correct and moved "
                 f"{realized:+.2%}"),
        evidence=[
            Evidence(kind="signal_state", source="signals.status",
                     statement=f"the signal was {status}: {reason}"),
            Evidence(kind="outcome", source="outcome_measurements",
                     statement=(f"the withheld claim was correct and worth "
                                f"{realized:+.2%} over {outcome.get('horizon')}"),
                     value=realized, comparison=HORIZON_RESCUE_RETURN),
            Evidence(kind="caveat", source="attribution.detectors",
                     statement="the suppression may still have been correct on "
                               "the information available at the time; this "
                               "records the cost, not a verdict on the rule"),
        ])


def detect_data_error(outcome: Dict[str, Any],
                      observation: Optional[Dict[str, Any]]) -> DetectorResult:
    """
    Was the decision made on bad data (§16, §38)?

    Judged on the data's state AT DECISION TIME, from the observation's
    recorded quality level — never inferred from the outcome. §16 is
    explicit: *do not infer data error merely because outcome was bad*,
    and this detector never looks at the return at all.
    """
    if observation is None:
        return _missing(ErrorType.DATA_ERROR,
                        "no research observation accompanies this subject",
                        "research_observations")
    quality = (observation.get("quality_level") or "").lower()
    if not quality:
        return _missing(ErrorType.DATA_ERROR,
                        "the observation records no quality level",
                        "research_observations.quality_level")

    if quality in ("high", "medium"):
        return DetectorResult(
            error_type=ErrorType.DATA_ERROR, fired=False,
            confidence=AttributionConfidence.HIGH,
            summary=f"decision-time data quality was {quality}")

    severity = Severity.HIGH if quality == "invalid" else Severity.MEDIUM
    return DetectorResult(
        error_type=ErrorType.DATA_ERROR, fired=True,
        confidence=AttributionConfidence.HIGH,
        severity=severity,
        summary=f"decision-time data quality was {quality}",
        evidence=[
            Evidence(kind="data_quality",
                     source="research_observations.quality_level",
                     statement=(f"the observation was marked {quality} at the "
                                f"time the claim was made — this is the state "
                                f"at decision time, not a judgement made "
                                f"afterwards")),
            Evidence(kind="method", source="attribution.detectors",
                     statement="this detector never reads the realised return; "
                               "a bad outcome cannot produce a data finding"),
        ])


# ======================================================================
# Layers with no evidence source in this database
# ======================================================================
#
# Each is implemented and each refuses to guess. They begin producing
# findings the moment their inputs exist. Naming the missing table in
# `missing` is what makes the gap visible in the dashboard instead of
# looking like a clean bill of health.

def detect_regime_error(outcome: Dict[str, Any],
                        cohort: Optional[Dict[str, Any]] = None) -> DetectorResult:
    """
    Does this model behave differently in this regime (§15)?

    Requires a regime label AND a regime cohort large enough to compare
    against. §15 forbids labelling every high-volatility loss a regime
    error, so the rule needs a population, not an anecdote.

    On the production database `market_regime` is NULL on all 6,510
    measurements, so this returns INSUFFICIENT_EVIDENCE everywhere.
    `signals.volatility_percentile` is populated and could support a
    future regime definition — but inventing one here is exactly what
    §20 of Phase 19 and §15 of this phase forbid.
    """
    regime = outcome.get("market_regime")
    if not regime:
        return _missing(
            ErrorType.REGIME_ERROR,
            "no market regime is recorded for this subject "
            "(the column exists and is unpopulated)",
            "outcome_measurements.market_regime")
    if cohort is None or (cohort.get("sample_size") or 0) < MIN_COHORT_SAMPLE:
        return _missing(
            ErrorType.REGIME_ERROR,
            f"the {regime} cohort has fewer than {MIN_COHORT_SAMPLE} "
            f"measurements, too few to say the regime is the difference",
            "outcome_aggregates")

    accuracy = cohort.get("directional_accuracy")
    if accuracy is None:
        return _missing(ErrorType.REGIME_ERROR,
                        "the regime cohort has no directional accuracy",
                        "outcome_aggregates.directional_accuracy")
    if accuracy >= 0.5 or outcome.get("direction_result") != "miss":
        return DetectorResult(
            error_type=ErrorType.REGIME_ERROR, fired=False,
            confidence=AttributionConfidence.MEDIUM,
            summary=(f"the {regime} cohort is not systematically worse "
                     f"({accuracy:.0%} over {cohort.get('sample_size')})"))

    return DetectorResult(
        error_type=ErrorType.REGIME_ERROR, fired=True,
        confidence=AttributionConfidence.MEDIUM,
        severity=Severity.MEDIUM,
        summary=(f"this miss sits in the {regime} regime, where the cohort "
                 f"is right {accuracy:.0%} of the time"),
        evidence=[
            Evidence(kind="regime", source="outcome_measurements.market_regime",
                     statement=f"regime at decision time: {regime}"),
            Evidence(kind="cohort", source="outcome_aggregates",
                     statement=(f"the {regime} cohort is directionally right "
                                f"{accuracy:.0%} of the time over "
                                f"{cohort.get('sample_size')} measurements"),
                     value=accuracy,
                     detail={"sample_size": cohort.get("sample_size")}),
        ])


def detect_sizing_error(outcome: Dict[str, Any],
                        position: Optional[Dict[str, Any]] = None) -> DetectorResult:
    """
    Was the position the wrong size for the signal (§12)?

    Needs a position, a risk budget and an exposure — none of which
    exist: `portfolios`, `positions` and `portfolio_state_snapshots`
    are absent from the production database.
    """
    if position is None:
        return _missing(
            ErrorType.SIZING_ERROR,
            "no position record exists — sizing cannot be assessed without "
            "a size, a risk budget and an exposure",
            "positions / portfolios")
    quantity = position.get("quantity")
    budget = position.get("risk_budget")
    if quantity is None or budget in (None, 0):
        return _missing(ErrorType.SIZING_ERROR,
                        "the position carries no quantity or no risk budget",
                        "positions.quantity / portfolios.risk_budget")

    share = abs(quantity * (outcome.get("reference_price") or 0.0)) / budget
    if share <= 1.0:
        return DetectorResult(
            error_type=ErrorType.SIZING_ERROR, fired=False,
            confidence=AttributionConfidence.HIGH,
            summary=f"the position used {share:.0%} of its risk budget")
    return DetectorResult(
        error_type=ErrorType.SIZING_ERROR, fired=True,
        confidence=AttributionConfidence.HIGH,
        severity=_severity_for(outcome.get("mae")),
        summary=f"the position used {share:.0%} of its risk budget",
        evidence=[
            Evidence(kind="sizing", source="positions",
                     statement=(f"notional exposure was {share:.0%} of the "
                                f"risk budget"),
                     value=share, comparison=1.0),
            Evidence(kind="adverse", source="outcome_measurements.mae",
                     statement="the adverse excursion is what that size was "
                               "exposed to",
                     value=outcome.get("mae")),
        ])


def detect_risk_error(outcome: Dict[str, Any],
                      decision: Optional[Dict[str, Any]] = None) -> DetectorResult:
    """
    Did risk behave differently from policy (§13)?

    §13 draws a distinction this detector is built around: a risk block
    followed by a favourable move is **EXPECTED_RISK_BLOCK**, not a
    failure. Risk exists to decline exposure, and grading it on
    hindsight would train it to decline less.

    A finding requires a POLICY VIOLATION — a decision that contradicts
    the limits recorded with it — not an unlucky outcome.

    `risk_decisions` does not exist in the production database.
    """
    if decision is None:
        return _missing(
            ErrorType.RISK_ERROR,
            "no risk decision record exists — a risk finding requires the "
            "policy that was applied, not the outcome that followed",
            "risk_decisions")

    violated = decision.get("violated_limits") or []
    approved = decision.get("is_approved")

    if not approved and not violated:
        return DetectorResult(
            error_type=ErrorType.RISK_ERROR, fired=False,
            confidence=AttributionConfidence.HIGH,
            summary=("expected risk block: exposure was declined under policy. "
                     "A block followed by a favourable move is risk working, "
                     "not risk failing"))
    if approved and violated:
        return DetectorResult(
            error_type=ErrorType.RISK_ERROR, fired=True,
            confidence=AttributionConfidence.HIGH,
            severity=Severity.CRITICAL,
            summary=f"approved despite {len(violated)} violated limit(s)",
            evidence=[
                Evidence(kind="risk_policy", source="risk_decisions",
                         statement=(f"the decision approved exposure while "
                                    f"recording violated limits: "
                                    f"{', '.join(map(str, violated))}"),
                         detail={"violated": list(violated)}),
            ])
    return DetectorResult(
        error_type=ErrorType.RISK_ERROR, fired=False,
        confidence=AttributionConfidence.HIGH,
        summary="the risk decision matched its recorded policy")


def detect_execution_error(outcome: Dict[str, Any],
                           fill: Optional[Dict[str, Any]] = None) -> DetectorResult:
    """
    Did execution materially cost something (§14)?

    §14 is explicit: *do not blame execution for an objectively bad
    prediction*. So this compares the fill against the decision price
    and nothing else — it never reads the realised return, and a wrong
    direction cannot produce an execution finding.

    `order_intents`, `execution_orders` and `execution_fills` do not
    exist in the production database, and no order has ever been
    created.
    """
    if fill is None:
        return _missing(
            ErrorType.EXECUTION_ERROR,
            "no fill exists — no order has ever been placed, so there is no "
            "execution to assess",
            "execution_fills / order_intents")
    decision_price = fill.get("decision_price")
    fill_price = fill.get("fill_price")
    if not decision_price or not fill_price:
        return _missing(ErrorType.EXECUTION_ERROR,
                        "the fill carries no decision price or no fill price",
                        "execution_fills")

    side = (outcome.get("expected_direction") or "long").lower()
    slippage = ((fill_price - decision_price) / decision_price
                if side == "long" else
                (decision_price - fill_price) / decision_price)
    if slippage <= NEUTRAL_BAND:
        return DetectorResult(
            error_type=ErrorType.EXECUTION_ERROR, fired=False,
            confidence=AttributionConfidence.HIGH,
            summary=f"slippage {slippage:+.2%}, within tolerance")
    return DetectorResult(
        error_type=ErrorType.EXECUTION_ERROR, fired=True,
        confidence=AttributionConfidence.HIGH,
        severity=_severity_for(slippage),
        summary=f"adverse slippage of {slippage:+.2%} against the decision price",
        evidence=[
            Evidence(kind="slippage", source="execution_fills",
                     statement=(f"decision price {decision_price}, fill "
                                f"{fill_price} — {slippage:+.2%} adverse"),
                     value=fill_price, comparison=decision_price),
            Evidence(kind="method", source="attribution.detectors",
                     statement="computed against the decision price only; the "
                               "realised return is not consulted, so a wrong "
                               "direction cannot become an execution finding"),
        ])


def detect_portfolio_error(outcome: Dict[str, Any],
                           portfolio: Optional[Dict[str, Any]] = None) -> DetectorResult:
    """
    Good signal, bad portfolio construction (§17).

    Needs concentration or correlation across held positions.
    `portfolios` and `positions` do not exist in the production
    database.
    """
    if portfolio is None:
        return _missing(
            ErrorType.PORTFOLIO_ERROR,
            "no portfolio record exists — concentration and correlation "
            "cannot be assessed from a single signal",
            "portfolios / positions")
    concentration = portfolio.get("max_concentration")
    limit = portfolio.get("concentration_limit")
    if concentration is None or limit in (None, 0):
        return _missing(ErrorType.PORTFOLIO_ERROR,
                        "the portfolio records no concentration measure",
                        "portfolios.max_concentration")
    if concentration <= limit:
        return DetectorResult(
            error_type=ErrorType.PORTFOLIO_ERROR, fired=False,
            confidence=AttributionConfidence.HIGH,
            summary=f"concentration {concentration:.0%} within its limit")
    return DetectorResult(
        error_type=ErrorType.PORTFOLIO_ERROR, fired=True,
        confidence=AttributionConfidence.MEDIUM,
        severity=Severity.HIGH,
        summary=(f"concentration {concentration:.0%} exceeded the "
                 f"{limit:.0%} limit"),
        evidence=[
            Evidence(kind="concentration", source="portfolios",
                     statement=(f"maximum concentration {concentration:.0%} "
                                f"against a {limit:.0%} limit"),
                     value=concentration, comparison=limit),
        ])


#: Registered in the order the engine evaluates them. Order does not
#: decide the primary error — evidence strength does, in `engine.py` —
#: but a stable order keeps output byte-identical between runs (§65).
DETECTORS = (
    ErrorType.PREDICTION_ERROR,
    ErrorType.MAGNITUDE_ERROR,
    ErrorType.HORIZON_MISMATCH,
    ErrorType.TIMING_ERROR,
    ErrorType.SIGNAL_ERROR,
    ErrorType.DATA_ERROR,
    ErrorType.REGIME_ERROR,
    ErrorType.SIZING_ERROR,
    ErrorType.RISK_ERROR,
    ErrorType.EXECUTION_ERROR,
    ErrorType.PORTFOLIO_ERROR,
)
