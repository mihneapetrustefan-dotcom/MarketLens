"""
src/execution/adapters/ibkr/mapper.py
------------------------------------------
IBKR to canonical translation (Phase 15, spec §20, §24, §26, §28, §11,
§12, §13, §17).

THE ONLY PLACE IBKR VOCABULARY IS UNDERSTOOD
------------------------------------------------
IBKR says `PreSubmitted`, `B`, `MKT`, `avgCost`, `netliquidation`. The
core says `ACKNOWLEDGED`, `BUY`, `MARKET`, `average_price`, `equity`.
This module is the dictionary, and it is the only file above the
transport that knows either dialect.

EVERY MAP IS WRITTEN OUT
----------------------------
No status is translated by lowercasing it and hoping. IBKR's
`Submitted` and the canonical `SUBMITTED` mean *different things* —
IBKR's means the venue is working it, which is canonical
`ACKNOWLEDGED`; canonical `SUBMITTED` means we have sent it and heard
nothing. Mapping those by spelling would be wrong in the most
expensive possible way, on the happy path, silently.

WHAT IS PRESERVED
---------------------
Every normalized object carries the raw IBKR payload alongside.
Normalization is lossy and the lost part is usually what explains a
discrepancy at 3am. It is scrubbed of anything credential-shaped
first.

NUMBERS ARE PARSED DEFENSIVELY
----------------------------------
IBKR returns numbers as strings, as numbers, and occasionally as
strings with commas. `_number` handles all three and returns None
rather than raising — a quote field that failed to parse must read as
"unknown", never as zero, because zero is a price and unknown is not.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.domain.broker_models import (
    AccountSnapshot, CanonicalOrderSide, CanonicalOrderType,
    CanonicalTimeInForce, CashBalance, ExecutionEnvironment, ExecutionFill,
    ExecutionOrder, ExecutionOrderState, MarginSnapshot, PositionSnapshot,
    finite_or_none,
)
from src.execution.adapters.ibkr.errors import scrub
from src.execution.gateway import BrokerOrderView

# ============================================================
# Order status
# ============================================================

#: IBKR order status to canonical state.
#:
#: The entry that matters most is `Submitted`. IBKR uses it to mean
#: "the venue is working this order", which is canonical
#: ACKNOWLEDGED — canonical SUBMITTED means "we sent it and have not
#: heard back". Mapping the two by their spelling would be wrong on
#: every successful order.
IBKR_STATUS_TO_STATE: Dict[str, ExecutionOrderState] = {
    "PendingSubmit": ExecutionOrderState.SUBMITTED,
    "PreSubmitted": ExecutionOrderState.ACKNOWLEDGED,
    "Submitted": ExecutionOrderState.ACKNOWLEDGED,
    "Presubmitted": ExecutionOrderState.ACKNOWLEDGED,
    "Working": ExecutionOrderState.WORKING,
    "Filled": ExecutionOrderState.FILLED,
    "PendingCancel": ExecutionOrderState.CANCEL_REQUESTED,
    "Cancelled": ExecutionOrderState.CANCELLED,
    "Canceled": ExecutionOrderState.CANCELLED,
    "ApiCancelled": ExecutionOrderState.CANCELLED,
    "Rejected": ExecutionOrderState.REJECTED,
    "Expired": ExecutionOrderState.EXPIRED,
    # `Inactive` is IBKR's catch-all for an order the venue is holding
    # but not working — a bad price, a closed market, a rejected
    # attribute. It is NOT terminal and must not be read as cancelled,
    # so it becomes a question rather than a conclusion.
    "Inactive": ExecutionOrderState.RECONCILIATION_REQUIRED,
    "WarnState": ExecutionOrderState.RECONCILIATION_REQUIRED,
}

#: Canonical order type to IBKR's.
TYPE_TO_IBKR: Dict[CanonicalOrderType, str] = {
    CanonicalOrderType.MARKET: "MKT",
    CanonicalOrderType.LIMIT: "LMT",
    CanonicalOrderType.STOP: "STP",
    CanonicalOrderType.STOP_LIMIT: "STOP_LIMIT",
}
IBKR_TO_TYPE: Dict[str, CanonicalOrderType] = {
    "MKT": CanonicalOrderType.MARKET,
    "MARKET": CanonicalOrderType.MARKET,
    "LMT": CanonicalOrderType.LIMIT,
    "LIMIT": CanonicalOrderType.LIMIT,
    "STP": CanonicalOrderType.STOP,
    "STOP": CanonicalOrderType.STOP,
    "STOP_LIMIT": CanonicalOrderType.STOP_LIMIT,
    "STP_LMT": CanonicalOrderType.STOP_LIMIT,
}

#: Time in force. FOK is deliberately absent: IBKR's support for it is
#: conditional on the contract and the venue, and the capability
#: declaration omits it rather than promising something that would be
#: refused at submission.
TIF_TO_IBKR: Dict[CanonicalTimeInForce, str] = {
    CanonicalTimeInForce.DAY: "DAY",
    CanonicalTimeInForce.GTC: "GTC",
    CanonicalTimeInForce.IOC: "IOC",
}
IBKR_TO_TIF: Dict[str, CanonicalTimeInForce] = {
    "DAY": CanonicalTimeInForce.DAY,
    "GTC": CanonicalTimeInForce.GTC,
    "IOC": CanonicalTimeInForce.IOC,
    "OPG": CanonicalTimeInForce.DAY,
}


def _number(value: Any) -> Optional[float]:
    """
    Parse an IBKR number, whatever shape it arrived in.

    Returns None rather than raising or defaulting to zero. A quote
    field that failed to parse means "unknown", and unknown is not a
    price — treating it as 0.0 would let a broken field become a
    tradeable one.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return finite_or_none(float(value))
    text = str(value).strip().replace(",", "")
    if not text or text in ("--", "n/a", "N/A"):
        return None
    # IBKR decorates some quote fields, e.g. "C100.25" for a close.
    while text and text[0].isalpha():
        text = text[1:]
    try:
        return finite_or_none(float(text))
    except ValueError:
        return None


def _moment(value: Any) -> Optional[datetime]:
    """Epoch milliseconds or seconds to an aware UTC datetime."""
    raw = _number(value)
    if raw is None or raw <= 0:
        return None
    seconds = raw / 1000.0 if raw > 1e11 else raw
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def side_from_ibkr(value: Any) -> CanonicalOrderSide:
    """
    IBKR spells the side `B`/`S` on executions and `BUY`/`SELL` on
    orders. Both are handled; anything else raises rather than
    defaulting, because guessing a side is unrecoverable.
    """
    text = str(value or "").strip().upper()
    if text in ("B", "BUY", "BOT"):
        return CanonicalOrderSide.BUY
    if text in ("S", "SELL", "SLD"):
        return CanonicalOrderSide.SELL
    raise ValueError(f"unrecognised IBKR side {value!r}")


def side_to_ibkr(side: CanonicalOrderSide) -> str:
    return "BUY" if side is CanonicalOrderSide.BUY else "SELL"


def state_from_ibkr(status: Any) -> ExecutionOrderState:
    """
    IBKR status to canonical state.

    An unrecognised status becomes RECONCILIATION_REQUIRED rather than
    UNKNOWN or a guess. IBKR adding a status we have never seen is a
    real possibility, and the safe response is to stop and ask rather
    than to assume it resembles something familiar.
    """
    text = str(status or "").strip()
    if text in IBKR_STATUS_TO_STATE:
        return IBKR_STATUS_TO_STATE[text]
    for known, state in IBKR_STATUS_TO_STATE.items():
        if known.lower() == text.lower():
            return state
    return ExecutionOrderState.RECONCILIATION_REQUIRED


# ============================================================
# Orders
# ============================================================

def order_to_ibkr(order: ExecutionOrder, conid: str,
                  account_id: str) -> Dict[str, Any]:
    """
    Canonical order to an IBKR order request (spec §20).

    `cOID` carries our client order id, which is derived from the
    idempotency key — so a retry sends the same one, and IBKR's own
    duplicate detection sees it as the same order. That is the second
    line of idempotency defence, at the venue rather than in our book.
    """
    payload: Dict[str, Any] = {
        "conid": int(conid) if str(conid).isdigit() else conid,
        "side": side_to_ibkr(order.side),
        "quantity": order.quantity,
        "orderType": TYPE_TO_IBKR[order.order_type],
        "tif": TIF_TO_IBKR[order.time_in_force],
        "acctId": account_id,
        "cOID": order.client_order_id,
        # Never true in this phase. IBKR treats an order outside
        # regular hours as a separate permission, and asking for it
        # would widen what a paper test can touch.
        "outsideRTH": False,
    }
    if order.limit_price is not None:
        payload["price"] = order.limit_price
    if order.stop_price is not None:
        payload["auxPrice"] = order.stop_price
    return payload


def order_view_from_ibkr(payload: Dict[str, Any],
                         instrument_id: Optional[str] = None,
                         symbol: str = "") -> BrokerOrderView:
    """One IBKR order as the venue currently describes it."""
    quantity = _number(payload.get("totalSize")) or _number(
        payload.get("quantity")) or 0.0
    filled = _number(payload.get("filledQuantity")) or 0.0
    return BrokerOrderView(
        broker_order_id=str(payload.get("orderId") or payload.get("order_id") or ""),
        instrument_id=instrument_id,
        broker_symbol=symbol or str(payload.get("ticker")
                                    or payload.get("symbol") or ""),
        side=side_from_ibkr(payload.get("side")),
        quantity=quantity,
        filled_quantity=filled,
        average_fill_price=_number(payload.get("avgPrice")),
        state=state_from_ibkr(payload.get("status")),
        order_type=IBKR_TO_TYPE.get(
            str(payload.get("orderType") or "MKT").upper(),
            CanonicalOrderType.MARKET),
        time_in_force=IBKR_TO_TIF.get(
            str(payload.get("tif") or "DAY").upper(),
            CanonicalTimeInForce.DAY),
        limit_price=_number(payload.get("price")),
        stop_price=_number(payload.get("auxPrice")),
        client_order_id=str(payload.get("cOID") or payload.get("order_ref") or "")
                        or None,
        at=_moment(payload.get("lastExecutionTime_r")),
        raw_broker_payload=scrub(payload))


# ============================================================
# Executions and fills
# ============================================================

def fill_from_execution(payload: Dict[str, Any], order: ExecutionOrder,
                        instrument_id: Optional[str] = None) -> ExecutionFill:
    """
    An IBKR execution to a canonical fill (spec §26, §28).

    The idempotency key is IBKR's own execution id. That is the right
    choice and the only safe one: two genuinely different executions
    can agree on instrument, side, size, price and second — a venue
    filling 100 as two 50s at one price produces exactly that — so
    deduplicating on the visible fields would silently discard a real
    fill.

    Commission frequently arrives LATER than the execution, in a
    separate report, so a zero here means "not yet reported" rather
    than "free". It is recorded as it arrives and the raw payload
    keeps whatever IBKR actually said.
    """
    quantity = abs(_number(payload.get("size")) or 0.0)
    price = _number(payload.get("price")) or 0.0
    execution_id = str(payload.get("execution_id")
                       or payload.get("execid") or "")
    return ExecutionFill(
        fill_id=execution_id or f"ibkr-fill-{payload.get('orderId', '')}",
        order_id=order.order_id,
        broker_id=order.broker_id,
        account_id=order.account_id,
        instrument_id=instrument_id or order.instrument_id,
        side=side_from_ibkr(payload.get("side")),
        quantity=quantity,
        price=price,
        filled_at=_moment(payload.get("trade_time_r")),
        execution_id=execution_id or None,
        broker_order_id=str(payload.get("orderId") or "") or None,
        commission=_number(payload.get("commission")) or 0.0,
        fees=_number(payload.get("fees")) or 0.0,
        exchange_fees=_number(payload.get("exchange_fees")) or 0.0,
        currency=str(payload.get("currency") or "USD"),
        reference_price=order.reference_price,
        liquidity=str(payload.get("liquidation") or ""),
        idempotency_key=execution_id,
        correlation_id=order.correlation_id,
        raw_broker_payload=scrub(payload))


# ============================================================
# Account
# ============================================================

def _summary_amount(summary: Dict[str, Any], *keys: str) -> Optional[float]:
    """
    Pull one figure out of an IBKR account summary.

    IBKR returns each field as `{"amount": ..., "currency": ...}` in
    some responses and as a bare value in others, and the key casing
    varies. Several candidate keys are tried because a missing figure
    must read as None, not as zero.
    """
    for key in keys:
        for candidate in (key, key.lower(), key.upper()):
            if candidate not in summary:
                continue
            value = summary[candidate]
            if isinstance(value, dict):
                parsed = _number(value.get("amount"))
                if parsed is not None:
                    return parsed
            else:
                parsed = _number(value)
                if parsed is not None:
                    return parsed
    return None


def account_from_ibkr(summary: Dict[str, Any], account_id: str,
                      broker_id: str, at: datetime) -> AccountSnapshot:
    """
    IBKR account summary to a canonical snapshot (spec §11, §12).

    Every field is Optional where IBKR may not report it. A cash
    account has no margin, and defaulting margin to zero would make
    "no margin concept" indistinguishable from "no margin used".
    """
    cash = _summary_amount(summary, "totalcashvalue", "totalCashValue")
    equity = _summary_amount(summary, "netliquidation", "netLiquidation")
    currency = "USD"
    for key in ("totalcashvalue", "netliquidation"):
        block = summary.get(key)
        if isinstance(block, dict) and block.get("currency"):
            currency = str(block["currency"])
            break

    return AccountSnapshot(
        account_id=account_id, broker_id=broker_id, at=at,
        base_currency=currency,
        cash=cash if cash is not None else 0.0,
        equity=equity if equity is not None else 0.0,
        available_funds=_summary_amount(summary, "availablefunds",
                                        "availableFunds"),
        buying_power=_summary_amount(summary, "buyingpower", "buyingPower"),
        portfolio_value=equity,
        realized_pnl=_summary_amount(summary, "realizedpnl", "realizedPnl") or 0.0,
        unrealized_pnl=_summary_amount(summary, "unrealizedpnl", "unrealizedPnl"),
        balances=[CashBalance(currency=currency,
                              cash=cash if cash is not None else 0.0)],
        margin=MarginSnapshot(
            initial_margin=_summary_amount(summary, "initmarginreq",
                                           "initMarginReq"),
            maintenance_margin=_summary_amount(summary, "maintmarginreq",
                                               "maintMarginReq"),
            margin_used=_summary_amount(summary, "initmarginreq",
                                        "initMarginReq"),
            margin_available=_summary_amount(summary, "excessliquidity",
                                             "excessLiquidity"),
            leverage=_summary_amount(summary, "leverage")),
        environment=ExecutionEnvironment.PAPER,
        raw_broker_payload=scrub(summary))


def position_from_ibkr(payload: Dict[str, Any], account_id: str,
                       broker_id: str, at: datetime,
                       instrument_id: Optional[str] = None) -> PositionSnapshot:
    """
    An IBKR position to canonical (spec §13).

    `avgCost` at IBKR is the cost per unit INCLUDING the contract
    multiplier, which for a stock equals the price and for a future
    does not. The multiplier is divided out where it is reported, so
    `average_price` is comparable across instruments — and the raw
    payload keeps IBKR's own figure either way.
    """
    quantity = _number(payload.get("position")) or 0.0
    multiplier = _number(payload.get("multiplier")) or 1.0
    average = (_number(payload.get("avgPrice"))
               or _number(payload.get("avgCost")))
    if average is not None and multiplier and multiplier != 1.0:
        average = average / multiplier

    return PositionSnapshot(
        account_id=account_id, broker_id=broker_id,
        instrument_id=instrument_id or str(payload.get("conid") or ""),
        quantity=quantity,
        average_price=average if average is not None else 0.0,
        at=at,
        market_price=_number(payload.get("mktPrice")),
        realized_pnl=_number(payload.get("realizedPnl")) or 0.0,
        unrealized_pnl=_number(payload.get("unrealizedPnl")),
        currency=str(payload.get("currency") or "USD"),
        broker_symbol=str(payload.get("contractDesc")
                          or payload.get("ticker") or ""),
        raw_broker_payload=scrub(payload))


# ============================================================
# Market data
# ============================================================

#: IBKR snapshot field ids. Numeric keys, documented by IBKR.
FIELD_LAST = "31"
FIELD_BID = "84"
FIELD_ASK = "86"
FIELD_VOLUME = "88"
SNAPSHOT_FIELDS: Tuple[str, ...] = (FIELD_LAST, FIELD_BID, FIELD_ASK,
                                    FIELD_VOLUME)


def quote_from_ibkr(payload: Dict[str, Any],
                    received_at: datetime) -> Dict[str, Any]:
    """
    An IBKR snapshot to a canonical quote (spec §17, §39).

    Two timestamps are kept apart deliberately: `broker_at` is when
    IBKR says the quote happened, `received_at` is when we saw it.
    They are frequently different, and the gap is what freshness is
    measured against — collapsing them would make stale data look
    current.
    """
    bid = _number(payload.get(FIELD_BID))
    ask = _number(payload.get(FIELD_ASK))
    mid = ((bid + ask) / 2.0) if (bid is not None and ask is not None) else None
    return {
        "conid": str(payload.get("conid") or ""),
        "last": _number(payload.get(FIELD_LAST)),
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "volume": _number(payload.get(FIELD_VOLUME)),
        "broker_at": _moment(payload.get("_updated")),
        "received_at": received_at,
        "raw": scrub(payload),
    }
