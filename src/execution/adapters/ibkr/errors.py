"""
src/execution/adapters/ibkr/errors.py
------------------------------------------
IBKR error translation (Phase 15, spec §42, §43, §51, §76).

WHY ERRORS NEED THEIR OWN MODULE
------------------------------------
An IBKR error is a code, a message and a context. The core system can
act on exactly one of those: a canonical category. If raw IBKR codes
reach the orchestrator, every consumer downstream has to learn IBKR's
numbering — which is the boundary violation this whole phase exists to
avoid, arriving through the error path instead of the happy path.

So every failure is mapped to a category and a Phase 14
`ExecutionRejectCode`, and the original is preserved alongside for
debugging rather than discarded.

WHAT IS PRESERVED AND WHAT IS SCRUBBED
------------------------------------------
Preserved: the IBKR code, the IBKR message, the endpoint, the HTTP
status. All of it is useful at 3am and none of it is sensitive.

Scrubbed: anything that looks like a session token, cookie or
authorization header. The Client Portal Gateway holds the credential
and this process never sees a password — but it does hold a session
cookie, and a cookie in a log is a credential in a log.

CATEGORIES ARE COARSE ON PURPOSE
------------------------------------
Twelve categories, not sixty. The category answers one question — what
should the system DO — and the answers are few: retry, wait, stop,
reconcile, tell a human. The precise IBKR code is kept for the human.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from src.domain.broker_models import ExecutionRejectCode


class IBKRErrorCategory(str, Enum):
    """What kind of failure this is, in terms the core can act on."""
    AUTHENTICATION_ERROR = "authentication_error"
    CONNECTION_ERROR = "connection_error"
    RATE_LIMIT_ERROR = "rate_limit_error"
    INVALID_ORDER = "invalid_order"
    INVALID_CONTRACT = "invalid_contract"
    MARKET_CLOSED = "market_closed"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    PERMISSION_ERROR = "permission_error"
    MARKET_DATA_ERROR = "market_data_error"
    BROKER_REJECTION = "broker_rejection"
    TIMEOUT = "timeout"
    UNKNOWN_BROKER_ERROR = "unknown_broker_error"

    @property
    def is_retryable(self) -> bool:
        """
        Whether retrying the same request could succeed.

        `TIMEOUT` is deliberately absent. A timed-out submission may
        have reached the venue, so retrying it is exactly the action
        that turns one intended order into two. It resolves through
        reconciliation, never through a retry.
        """
        return self in (IBKRErrorCategory.CONNECTION_ERROR,
                        IBKRErrorCategory.RATE_LIMIT_ERROR)

    @property
    def stops_new_orders(self) -> bool:
        return self in (IBKRErrorCategory.AUTHENTICATION_ERROR,
                        IBKRErrorCategory.CONNECTION_ERROR,
                        IBKRErrorCategory.PERMISSION_ERROR,
                        IBKRErrorCategory.RATE_LIMIT_ERROR)


#: Category to the Phase 14 code the core already understands. Every
#: category maps, so an error never reaches the orchestrator without a
#: canonical reason attached.
CATEGORY_TO_REJECT: Dict[IBKRErrorCategory, ExecutionRejectCode] = {
    IBKRErrorCategory.AUTHENTICATION_ERROR: ExecutionRejectCode.BROKER_DISCONNECTED,
    IBKRErrorCategory.CONNECTION_ERROR: ExecutionRejectCode.BROKER_DISCONNECTED,
    IBKRErrorCategory.RATE_LIMIT_ERROR: ExecutionRejectCode.RATE_LIMITED,
    IBKRErrorCategory.INVALID_ORDER: ExecutionRejectCode.INVALID_QUANTITY,
    IBKRErrorCategory.INVALID_CONTRACT: ExecutionRejectCode.NO_INSTRUMENT_MAPPING,
    IBKRErrorCategory.MARKET_CLOSED: ExecutionRejectCode.MARKET_CLOSED,
    IBKRErrorCategory.INSUFFICIENT_FUNDS: ExecutionRejectCode.INSUFFICIENT_BUYING_POWER,
    IBKRErrorCategory.PERMISSION_ERROR: ExecutionRejectCode.ACCOUNT_DISABLED,
    IBKRErrorCategory.MARKET_DATA_ERROR: ExecutionRejectCode.NO_PRICE,
    IBKRErrorCategory.BROKER_REJECTION: ExecutionRejectCode.ADAPTER_ERROR,
    IBKRErrorCategory.TIMEOUT: ExecutionRejectCode.ADAPTER_ERROR,
    IBKRErrorCategory.UNKNOWN_BROKER_ERROR: ExecutionRejectCode.ADAPTER_ERROR,
}

#: HTTP status to category, for failures the gateway reports at the
#: transport level rather than in a body.
_HTTP_CATEGORY: Dict[int, IBKRErrorCategory] = {
    400: IBKRErrorCategory.INVALID_ORDER,
    401: IBKRErrorCategory.AUTHENTICATION_ERROR,
    403: IBKRErrorCategory.PERMISSION_ERROR,
    404: IBKRErrorCategory.INVALID_CONTRACT,
    429: IBKRErrorCategory.RATE_LIMIT_ERROR,
    500: IBKRErrorCategory.UNKNOWN_BROKER_ERROR,
    502: IBKRErrorCategory.CONNECTION_ERROR,
    503: IBKRErrorCategory.CONNECTION_ERROR,
    504: IBKRErrorCategory.TIMEOUT,
}

#: Message fragments to category, lowercase. Used only when nothing
#: more structured is available — a code or a status is always
#: preferred, because message text is the part a venue changes without
#: telling anyone.
_MESSAGE_PATTERNS: Tuple[Tuple[str, IBKRErrorCategory], ...] = (
    ("not authenticated", IBKRErrorCategory.AUTHENTICATION_ERROR),
    ("session", IBKRErrorCategory.AUTHENTICATION_ERROR),
    ("competing session", IBKRErrorCategory.AUTHENTICATION_ERROR),
    ("login", IBKRErrorCategory.AUTHENTICATION_ERROR),
    ("no bridge", IBKRErrorCategory.CONNECTION_ERROR),
    ("connection", IBKRErrorCategory.CONNECTION_ERROR),
    ("pacing", IBKRErrorCategory.RATE_LIMIT_ERROR),
    ("too many requests", IBKRErrorCategory.RATE_LIMIT_ERROR),
    ("market is closed", IBKRErrorCategory.MARKET_CLOSED),
    ("outside", IBKRErrorCategory.MARKET_CLOSED),
    ("insufficient", IBKRErrorCategory.INSUFFICIENT_FUNDS),
    ("margin", IBKRErrorCategory.INSUFFICIENT_FUNDS),
    ("no security definition", IBKRErrorCategory.INVALID_CONTRACT),
    ("contract", IBKRErrorCategory.INVALID_CONTRACT),
    ("conid", IBKRErrorCategory.INVALID_CONTRACT),
    ("market data", IBKRErrorCategory.MARKET_DATA_ERROR),
    ("subscription", IBKRErrorCategory.MARKET_DATA_ERROR),
    ("not permitted", IBKRErrorCategory.PERMISSION_ERROR),
    ("permission", IBKRErrorCategory.PERMISSION_ERROR),
    ("timed out", IBKRErrorCategory.TIMEOUT),
    ("timeout", IBKRErrorCategory.TIMEOUT),
    ("order", IBKRErrorCategory.INVALID_ORDER),
)

#: Selected TWS/CPAPI numeric codes with unambiguous meanings. Kept
#: small and specific: guessing at a code's meaning is worse than
#: falling through to the message.
_CODE_CATEGORY: Dict[int, IBKRErrorCategory] = {
    200: IBKRErrorCategory.INVALID_CONTRACT,   # no security definition found
    201: IBKRErrorCategory.BROKER_REJECTION,   # order rejected
    202: IBKRErrorCategory.BROKER_REJECTION,   # order cancelled
    354: IBKRErrorCategory.MARKET_DATA_ERROR,  # not subscribed to market data
    502: IBKRErrorCategory.CONNECTION_ERROR,   # couldn't connect to TWS
    504: IBKRErrorCategory.CONNECTION_ERROR,   # not connected
    1100: IBKRErrorCategory.CONNECTION_ERROR,  # connectivity lost
    2104: IBKRErrorCategory.CONNECTION_ERROR,  # data farm connection ok (info)
    10147: IBKRErrorCategory.BROKER_REJECTION,  # order to cancel not found
    10148: IBKRErrorCategory.BROKER_REJECTION,  # cannot cancel filled order
}

#: Anything matching these is removed before an error is stored or
#: logged. The gateway holds the credential, but the session cookie is
#: a credential too.
_SENSITIVE_KEYS = re.compile(
    r"(cookie|authorization|session|token|password|secret|api[_-]?key|bearer)",
    re.IGNORECASE)
_SENSITIVE_VALUE = re.compile(
    r"(?i)\b(bearer\s+[\w.\-]+|[A-Za-z0-9]{24,})\b")


def scrub(value: Any) -> Any:
    """
    Remove anything credential-shaped from a payload before it is
    stored or logged.

    Keys are matched by name and values by shape, because a session id
    can arrive under a key nobody anticipated. Over-redacting a long
    opaque string is cheap; leaking one is not.
    """
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, item in value.items():
            if _SENSITIVE_KEYS.search(str(key)):
                cleaned[str(key)] = "<redacted>"
            else:
                cleaned[str(key)] = scrub(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        return _SENSITIVE_VALUE.sub("<redacted>", value)
    return value


@dataclass
class IBKRError(Exception):
    """
    A translated IBKR failure.

    Carries both halves: the canonical category the system acts on, and
    the original detail a person reads. `context` is scrubbed on
    construction, so an instance is always safe to log.
    """
    category: IBKRErrorCategory
    message: str
    ibkr_code: Optional[int] = None
    http_status: Optional[int] = None
    endpoint: str = ""
    context: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        Exception.__init__(self, self.message)
        self.message = str(_SENSITIVE_VALUE.sub("<redacted>", self.message))
        self.context = scrub(self.context)

    @property
    def reject_code(self) -> ExecutionRejectCode:
        return CATEGORY_TO_REJECT.get(self.category,
                                      ExecutionRejectCode.ADAPTER_ERROR)

    @property
    def retryable(self) -> bool:
        return self.category.is_retryable

    def as_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category.value,
            "reject_code": self.reject_code.value,
            "message": self.message,
            "ibkr_code": self.ibkr_code,
            "http_status": self.http_status,
            "endpoint": self.endpoint,
            "retryable": self.retryable,
            "context": self.context,
        }


def classify(message: str = "", ibkr_code: Optional[int] = None,
             http_status: Optional[int] = None) -> IBKRErrorCategory:
    """
    Decide the category, most reliable signal first.

    Numeric code, then HTTP status, then message text — in that order,
    because message wording is the part a venue changes without
    announcing it, and a classifier that depends on wording breaks
    silently when it does.
    """
    if ibkr_code is not None and ibkr_code in _CODE_CATEGORY:
        return _CODE_CATEGORY[ibkr_code]
    if http_status is not None and http_status in _HTTP_CATEGORY:
        return _HTTP_CATEGORY[http_status]

    lowered = (message or "").lower()
    for fragment, category in _MESSAGE_PATTERNS:
        if fragment in lowered:
            return category
    return IBKRErrorCategory.UNKNOWN_BROKER_ERROR


def translate(message: str = "", ibkr_code: Optional[int] = None,
              http_status: Optional[int] = None, endpoint: str = "",
              **context: Any) -> IBKRError:
    """Build a translated error from whatever the venue gave us."""
    return IBKRError(
        category=classify(message, ibkr_code, http_status),
        message=message or "IBKR reported an error with no message",
        ibkr_code=ibkr_code, http_status=http_status, endpoint=endpoint,
        context=context)


def explain(error: IBKRError) -> str:
    """
    The sentence a person reads (spec §43).

    Leads with what it means for them, and carries the IBKR detail
    afterwards so it can be quoted into a support ticket.
    """
    lead = {
        IBKRErrorCategory.AUTHENTICATION_ERROR:
            "The IBKR gateway session is not authenticated. Log in to the "
            "Client Portal Gateway and try again.",
        IBKRErrorCategory.CONNECTION_ERROR:
            "The IBKR gateway could not be reached.",
        IBKRErrorCategory.RATE_LIMIT_ERROR:
            "IBKR is pacing our requests. The request was not sent.",
        IBKRErrorCategory.INVALID_ORDER:
            "IBKR refused the order as invalid.",
        IBKRErrorCategory.INVALID_CONTRACT:
            "IBKR could not identify the contract for this instrument.",
        IBKRErrorCategory.MARKET_CLOSED:
            "The market for this instrument is closed at IBKR.",
        IBKRErrorCategory.INSUFFICIENT_FUNDS:
            "The IBKR account has insufficient funds or margin for this order.",
        IBKRErrorCategory.PERMISSION_ERROR:
            "The IBKR account is not permitted to do this.",
        IBKRErrorCategory.MARKET_DATA_ERROR:
            "IBKR market data is unavailable for this instrument on this account.",
        IBKRErrorCategory.BROKER_REJECTION:
            "IBKR rejected the request.",
        IBKRErrorCategory.TIMEOUT:
            "The IBKR request timed out. The outcome is unknown and it will "
            "be resolved by querying IBKR, not by resending.",
        IBKRErrorCategory.UNKNOWN_BROKER_ERROR:
            "IBKR reported an error this system does not recognise.",
    }[error.category]

    detail = error.message
    if error.ibkr_code is not None:
        detail = f"[{error.ibkr_code}] {detail}"
    return f"{lead} IBKR said: {detail}"
