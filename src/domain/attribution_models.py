"""
src/domain/attribution_models.py
--------------------------------------------
Where a deviation occurred, and the evidence that says so.

WHAT THIS LAYER IS NOT
--------------------------
It is not a scoreboard. A loss is not a mistake and a win is not a
correct decision — a good decision can lose and a bad one can win, and
a diagnostic layer that forgets this becomes a machine for
rationalising noise.

So three of the outcomes below are *not* errors, and they are
first-class:

    NO_ERROR              every layer behaved as designed
    EXPECTED_LOSS         a loss inside the normal distribution
    UNKNOWN               the evidence does not support a conclusion

`INSUFFICIENT_EVIDENCE` is a status, not a failure. Phase 20's
instruction is explicit: *never invent an explanation simply because
the outcome was bad*.

THE EVIDENCE RULE
---------------------
Every attribution carries the specific numbers that produced it. A row
saying "timing error" and nothing else is an opinion; a row saying
"the favourable excursion reached +6.5% on day 5 while the close
finished at +2.4%, so the move was available and not captured" is a
finding somebody can check and disagree with.

A detector that cannot cite numbers returns nothing. That is why
`ErrorAttribution` cannot be constructed without at least one
`Evidence`, and a test asserts it.

WHAT CAN AND CANNOT BE ATTRIBUTED HERE
------------------------------------------
Measured against the production database on 2026-09-05, six of the
nine layers §2 lists have **no evidence source at all**:

    prediction   YES   549 predictions
    signal       YES   408 signals with model linkage
    timing       YES   MFE/MAE and their timestamps
    horizon      YES   seven horizons per subject
    data         YES   observation quality levels
    regime       NO    `market_regime` is NULL on all 6,510 rows
    sizing       NO    `portfolios`, `positions` do not exist
    risk         NO    `risk_decisions` does not exist
    execution    NO    `order_intents` does not exist
    portfolio    NO    `positions` does not exist

Those six detectors are still implemented, and they return
`INSUFFICIENT_EVIDENCE` naming the table that is missing. They start
producing findings the moment their inputs exist, and until then the
absence is visible rather than silently absent from the taxonomy.

DETERMINISTIC (§42, §65)
----------------------------
Every rule here is arithmetic over stored numbers. No LLM is called,
no randomness is used, and the same inputs under the same methodology
version produce byte-identical output. A diagnosis that changes when
you look at it twice is not a diagnosis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

# ======================================================================
# Versioning (§44)
# ======================================================================

#: Bump when a RULE changes — a different threshold, a different
#: ranking, a new detector that changes an existing verdict. It is part
#: of the primary key, so a bump writes NEW attributions beside the old
#: ones and never rewrites a conclusion somebody has already read.
ATTRIBUTION_METHOD_VERSION = "v1"


class ErrorType(str, Enum):
    """
    The taxonomy (§5).

    Three members are deliberately not errors. Without them every
    outcome is forced into a fault, and the layer stops being a
    diagnosis and becomes a confession.
    """
    PREDICTION_ERROR = "prediction_error"
    MAGNITUDE_ERROR = "magnitude_error"
    HORIZON_MISMATCH = "horizon_mismatch"
    SIGNAL_ERROR = "signal_error"
    TIMING_ERROR = "timing_error"
    SIZING_ERROR = "sizing_error"
    RISK_ERROR = "risk_error"
    EXECUTION_ERROR = "execution_error"
    REGIME_ERROR = "regime_error"
    DATA_ERROR = "data_error"
    PORTFOLIO_ERROR = "portfolio_error"

    #: Not errors.
    NO_ERROR = "no_error"
    EXPECTED_LOSS = "expected_loss"
    UNKNOWN = "unknown"

    @property
    def is_error(self) -> bool:
        return self not in (ErrorType.NO_ERROR, ErrorType.EXPECTED_LOSS,
                            ErrorType.UNKNOWN)


class AttributionConfidence(str, Enum):
    """
    How much the evidence supports the conclusion (§21).

    Ordinal labels, NOT a probability. Nothing here is calibrated
    against how often an attribution turns out to be right, because
    nothing has ever checked. Emitting 0.87 would imply a calibration
    that does not exist, and every downstream consumer would inherit
    the implication.

    HIGH     an unambiguous arithmetic fact
    MEDIUM   the numbers point one way, another reading is possible
    LOW      consistent with the evidence, and with other stories too
    INSUFFICIENT_EVIDENCE  the inputs required are absent
    """
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"

    @property
    def rank(self) -> int:
        return {"high": 3, "medium": 2, "low": 1,
                "insufficient_evidence": 0}[self.value]


class Severity(str, Enum):
    """
    How much this mattered (§25).

    Explicitly NOT a substitute for confidence. A CRITICAL severity
    with LOW confidence means "if this is what happened it matters a
    great deal, and we are not sure it is" — which is a useful thing to
    be able to say, and impossible if the two collapse into one number.
    """
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AttributionStatus(str, Enum):
    """Where this case stands (§46)."""
    PENDING = "pending"
    ATTRIBUTED = "attributed"
    PARTIALLY_ATTRIBUTED = "partially_attributed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    REQUIRES_REVIEW = "requires_review"
    SUPERSEDED = "superseded"


class AttributionRole(str, Enum):
    """Primary versus contributing (§20)."""
    PRIMARY = "primary"
    CONTRIBUTING = "contributing"


class Observability(str, Enum):
    """
    Observed fact versus hypothetical (§24).

    Every counterfactual row carries HYPOTHETICAL, and the loader
    refuses to mix the two in one result set without saying which is
    which. Presenting a counterfactual as history is the single most
    damaging thing this layer could do: it would manufacture a track
    record that never happened.
    """
    OBSERVED = "observed"
    HYPOTHETICAL = "hypothetical"


# ======================================================================
# Evidence (§22)
# ======================================================================

@dataclass
class Evidence:
    """
    One checkable fact behind an attribution.

    `source` names the table and column the number came from, so a
    reader can go and look. `statement` is the human sentence. `value`
    and `comparison` are the numbers themselves, kept structured rather
    than only formatted into prose, because an aggregate over evidence
    is useful and an aggregate over sentences is not.
    """
    kind: str
    statement: str
    source: str = ""
    value: Optional[float] = None
    comparison: Optional[float] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "statement": self.statement,
                "source": self.source, "value": self.value,
                "comparison": self.comparison, "detail": self.detail}


@dataclass
class ErrorAttribution:
    """
    One conclusion about one outcome, with the evidence for it.

    Identity is
    `(subject_kind, subject_id, horizon, method_version, error_type)`,
    so a subject may carry several attributions — §19 requires that,
    because a single bad result frequently has more than one cause and
    collapsing them into one label destroys the information.
    """
    subject_kind: str
    subject_id: str
    horizon: str
    error_type: ErrorType
    method_version: str = ATTRIBUTION_METHOD_VERSION

    role: AttributionRole = AttributionRole.CONTRIBUTING
    confidence: AttributionConfidence = AttributionConfidence.LOW
    severity: Severity = Severity.INFO
    status: AttributionStatus = AttributionStatus.ATTRIBUTED
    observability: Observability = Observability.OBSERVED

    summary: str = ""
    evidence: List[Evidence] = field(default_factory=list)

    #: What was expected and what happened, carried for reading without
    #: a join back to the outcome row.
    expected_direction: str = ""
    expected_return: Optional[float] = None
    realized_return: Optional[float] = None
    deviation: Optional[float] = None

    # Context, copied at attribution time.
    instrument_id: str = ""
    trained_model_id: Optional[str] = None
    model_status: Optional[str] = None
    strategy_id: Optional[str] = None
    market_regime: Optional[str] = None
    event_type: Optional[str] = None
    confidence_score: Optional[float] = None
    strength: Optional[float] = None
    signal_status: Optional[str] = None

    outcome_method_version: str = ""
    attributed_at: Optional[datetime] = None

    def __post_init__(self):
        if self.attributed_at is None:
            self.attributed_at = datetime.now(timezone.utc)

    @property
    def identity(self) -> tuple:
        return (self.subject_kind, self.subject_id, self.horizon,
                self.method_version, self.error_type.value)

    @property
    def is_error(self) -> bool:
        return self.error_type.is_error

    def require_evidence(self) -> None:
        """
        Refuse to exist without evidence.

        Called by the repository before writing. An attribution with no
        evidence is an assertion, and this phase's whole premise is
        that assertions are not diagnoses.

        The one exception is `INSUFFICIENT_EVIDENCE`, whose evidence is
        precisely the absence — and even then the detector must say
        WHICH input was missing.
        """
        if not self.evidence:
            raise ValueError(
                f"{self.error_type.value} for {self.subject_id} carries no "
                f"evidence. Every attribution must cite the numbers that "
                f"produced it; an explanation without evidence is an opinion "
                f"stored as a fact.")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "subject_kind": self.subject_kind, "subject_id": self.subject_id,
            "horizon": self.horizon, "error_type": self.error_type.value,
            "role": self.role.value, "confidence": self.confidence.value,
            "severity": self.severity.value, "status": self.status.value,
            "observability": self.observability.value,
            "summary": self.summary,
            "evidence": [item.as_dict() for item in self.evidence],
            "expected_return": self.expected_return,
            "realized_return": self.realized_return,
            "deviation": self.deviation,
        }


@dataclass
class DetectorResult:
    """
    What one detector concluded, including "I cannot tell".

    A detector returns this rather than `Optional[ErrorAttribution]` so
    that "no evidence available" and "evidence available, no error
    found" stay distinguishable. Collapsing them would make an
    unmeasurable layer look like a clean one, which is the most
    flattering possible lie.
    """
    error_type: ErrorType
    fired: bool = False
    confidence: AttributionConfidence = AttributionConfidence.INSUFFICIENT_EVIDENCE
    severity: Severity = Severity.INFO
    summary: str = ""
    evidence: List[Evidence] = field(default_factory=list)
    #: Which input was missing, when nothing could be judged.
    missing: str = ""

    @property
    def judgeable(self) -> bool:
        return self.confidence != AttributionConfidence.INSUFFICIENT_EVIDENCE


# ======================================================================
# Thresholds — named, documented, and reused from Phase 19
# ======================================================================

#: A realised move smaller than the Phase 19 neutral band is not
#: evidence of anything. Imported rather than redefined so the two
#: phases cannot drift apart (§7).
from src.domain.outcome_models import NEUTRAL_BAND  # noqa: E402

#: Direction right, magnitude badly wrong (§8). A signal that predicted
#: +5% and delivered +0.2% got the sign right and the size wrong, and
#: calling that identical to a wrong direction destroys the distinction
#: §8 exists to draw.
MAGNITUDE_SHORTFALL_RATIO = 0.25   # realised under a quarter of expected
MAGNITUDE_OVERSHOOT_RATIO = 4.0    # realised over four times expected

#: Horizon mismatch (§9): wrong at the stated horizon, right later. The
#: later move must clear the neutral band by a real margin, or a drift
#: of a few basis points would "rescue" every miss.
HORIZON_RESCUE_RETURN = 0.02

#: Timing (§10). The favourable excursion was worth having and was not
#: captured. Both conditions are required: a 0.3% MFE is not a missed
#: opportunity, and an MFE barely above the close is not bad timing.
TIMING_MFE_FLOOR = 0.02
TIMING_CAPTURE_RATIO = 0.4         # kept less than 40% of what was offered

#: Expected versus unexpected (§26, §27). A result inside the cohort's
#: own 10th-90th percentile band is ordinary. Outside it is worth a
#: look. The percentiles come from Phase 19's aggregates, so no new
#: distributional assumption is introduced.
UNEXPECTED_PERCENTILE_LOW = "p10_return"
UNEXPECTED_PERCENTILE_HIGH = "p90_return"

#: Below this, a cohort cannot say whether anything is unusual. Reused
#: from the Phase 9 evaluator via Phase 19, not redefined (§29, §62).
from src.domain.model_models import ModelEvaluation  # noqa: E402
MIN_COHORT_SAMPLE = ModelEvaluation.MIN_EFFECTIVE_SAMPLE
