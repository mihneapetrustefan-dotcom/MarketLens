"""
src/execution/adapters/ibkr/mock_transport.py
--------------------------------------------------
A deterministic IBKR double (Phase 15, spec §58, §61).

WHY THIS SHIPS IN `src/` RATHER THAN `tests/`
-------------------------------------------------
Two reasons. It is how the CLI can exercise the whole IBKR path with
no gateway and no account — which is what makes the adapter
demonstrable on a machine that has neither. And it is the reference
implementation of `IBKRTransport`: a second transport author reads
this to learn the contract.

It is a TEST DOUBLE, NOT A SIMULATOR
----------------------------------------
It does not model a market. It models IBKR being awkward. There is no
price discovery, no queue position, no liquidity — the fill price is
whatever the test set, because a test asserting on a simulated market
would be asserting about the simulation.

Everything it produces is clearly synthetic: account ids start `DU`
(IBKR's own paper convention), and `is_mock` is True on every payload
that crosses the boundary. Nothing here should ever be mistaken for
data from a real account.

WHAT IT CAN DO WRONG, ON COMMAND
------------------------------------
Time out after accepting. Duplicate an execution. Deliver events out
of order. Disconnect mid-order. Lose authentication. Rate-limit.
Return several contracts for one symbol. Reject. Each is a flag, so a
test sets the one it is about and everything else behaves.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from src.execution.adapters.ibkr.config import IBKRConfig, paper_config
from src.execution.adapters.ibkr.errors import (
    IBKRError, IBKRErrorCategory,
)
from src.execution.adapters.ibkr.transport import AuthStatus, IBKRTransport

#: IBKR paper accounts begin with DU. Using that prefix means a
#: mock account id can never be mistaken for a live one (which
#: begins with U), in a log, a dashboard or a test fixture.
MOCK_ACCOUNT = "DU1234567"


@dataclass
class MockContract:
    """One IBKR contract the mock knows about."""
    conid: str
    symbol: str
    exchange: str = "SMART"
    primary_exchange: str = "NASDAQ"
    currency: str = "USD"
    sec_type: str = "STK"
    trading_class: str = ""
    company_name: str = ""
    multiplier: float = 1.0

    def as_search_result(self) -> Dict[str, Any]:
        return {
            "conid": self.conid, "symbol": self.symbol,
            "companyName": self.company_name or self.symbol,
            "description": self.primary_exchange,
            "secType": self.sec_type, "currency": self.currency,
            "sections": [{"secType": self.sec_type,
                          "exchange": self.exchange}],
            "is_mock": True,
        }

    def as_details(self) -> Dict[str, Any]:
        return {
            "conid": self.conid, "symbol": self.symbol,
            "exchange": self.exchange, "listingExchange": self.primary_exchange,
            "currency": self.currency, "instrument_type": self.sec_type,
            "tradingClass": self.trading_class or self.symbol,
            "multiplier": self.multiplier,
            "rules": {"increment": 0.01, "sizeIncrement": 1.0,
                      "minSize": 1.0},
            "is_mock": True,
        }


@dataclass
class MockOrder:
    """An order as the mock venue holds it."""
    order_id: str
    account_id: str
    conid: str
    side: str
    quantity: float
    order_type: str
    status: str = "Submitted"
    filled: float = 0.0
    remaining: float = 0.0
    avg_price: Optional[float] = None
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    time_in_force: str = "DAY"
    client_order_id: str = ""
    at: Optional[datetime] = None

    def as_payload(self) -> Dict[str, Any]:
        return {
            "orderId": self.order_id, "acct": self.account_id,
            "conid": self.conid, "side": self.side,
            "totalSize": self.quantity, "filledQuantity": self.filled,
            "remainingQuantity": self.remaining or (self.quantity - self.filled),
            "avgPrice": self.avg_price, "orderType": self.order_type,
            "status": self.status, "price": self.limit_price,
            "auxPrice": self.stop_price, "tif": self.time_in_force,
            "cOID": self.client_order_id,
            "lastExecutionTime_r": int(self.at.timestamp() * 1000) if self.at else None,
            "is_mock": True,
        }


class MockIBKRTransport(IBKRTransport):
    """
    IBKR, failing on command.

    Defaults are entirely cooperative, so a test that sets no flag gets
    a well-behaved venue and only its own subject is unusual.
    """

    name = "mock"

    def __init__(self, config: Optional[IBKRConfig] = None,
                 account_id: str = MOCK_ACCOUNT):
        self.config = config or paper_config(account_id=account_id)
        self.account_id = account_id

        # --- failure switches -----------------------------------
        self.authenticated = True
        self.connected = True
        self.competing = False
        self.timeout_on_place = False
        self.raise_on_place: Optional[IBKRErrorCategory] = None
        self.raise_on_status = False
        self.reject_on_place = False
        self.rate_limited = False
        self.ambiguous_symbols: set = set()
        self.unknown_orders: set = set()
        self.requires_confirmation = False
        self.duplicate_executions = False
        self.market_data_available = True

        # --- venue state ----------------------------------------
        self.contracts: Dict[str, MockContract] = {}
        self.orders: Dict[str, MockOrder] = {}
        self.executions_log: List[Dict[str, Any]] = []
        self.positions_book: Dict[str, Dict[str, Any]] = {}
        self.quotes: Dict[str, Dict[str, Any]] = {}
        self.cash: float = 1_000_000.0
        self.equity: float = 1_000_000.0

        self.place_calls = 0
        self.cancel_calls = 0
        self.request_count = 0
        self._ids = itertools.count(1)
        self._exec_ids = itertools.count(1)
        self._pending_reply: Optional[str] = None

        self._seed()

    # ---------------- fixtures ----------------

    def _seed(self) -> None:
        for conid, symbol, primary, name in (
            ("265598", "AAPL", "NASDAQ", "APPLE INC"),
            ("272093", "MSFT", "NASDAQ", "MICROSOFT CORP"),
            ("8314", "IBM", "NYSE", "INTL BUSINESS MACHINES"),
        ):
            self.contracts[conid] = MockContract(
                conid=conid, symbol=symbol, primary_exchange=primary,
                company_name=name)
            self.quotes[conid] = {"conid": conid, "31": "100.00",
                                  "84": "99.98", "86": "100.02",
                                  "88": "1000", "is_mock": True}

    def add_contract(self, contract: MockContract,
                     quote: Optional[Dict[str, Any]] = None) -> MockContract:
        self.contracts[contract.conid] = contract
        self.quotes[contract.conid] = quote or {
            "conid": contract.conid, "31": "100.00", "84": "99.98",
            "86": "100.02", "88": "1000", "is_mock": True}
        return contract

    def set_quote(self, conid: str, last: float, bid: float, ask: float,
                  volume: float = 1000.0) -> None:
        self.quotes[conid] = {
            "conid": conid, "31": f"{last:.2f}", "84": f"{bid:.2f}",
            "86": f"{ask:.2f}", "88": f"{volume:.0f}", "is_mock": True}

    # ---------------- plumbing ----------------

    def _guard(self, endpoint: str) -> None:
        """Every call passes here, so a switch cannot be bypassed."""
        self.request_count += 1
        if self.rate_limited:
            raise IBKRError(
                category=IBKRErrorCategory.RATE_LIMIT_ERROR,
                message="pacing violation", endpoint=endpoint)
        if not self.connected:
            raise IBKRError(
                category=IBKRErrorCategory.CONNECTION_ERROR,
                message="no bridge to the gateway", endpoint=endpoint)
        if not self.authenticated:
            raise IBKRError(
                category=IBKRErrorCategory.AUTHENTICATION_ERROR,
                message="not authenticated", endpoint=endpoint)

    # ---------------- session ----------------

    def is_authenticated(self) -> AuthStatus:
        return AuthStatus(
            authenticated=self.authenticated, connected=self.connected,
            competing=self.competing,
            message="mock transport" if self.authenticated
                    else "not authenticated")

    def keepalive(self) -> bool:
        return self.authenticated and self.connected

    def close(self) -> None:
        self.connected = False

    # ---------------- account ----------------

    def accounts(self) -> List[Dict[str, Any]]:
        self._guard("/iserver/accounts")
        return [{"accountId": self.account_id, "is_mock": True}]

    def account_summary(self, account_id: str) -> Dict[str, Any]:
        self._guard("/portfolio/summary")
        return {
            "accountcode": {"amount": 0, "value": account_id},
            "totalcashvalue": {"amount": self.cash, "currency": "USD"},
            "netliquidation": {"amount": self.equity, "currency": "USD"},
            "availablefunds": {"amount": self.cash, "currency": "USD"},
            "buyingpower": {"amount": self.cash * 4, "currency": "USD"},
            "initmarginreq": {"amount": 0.0, "currency": "USD"},
            "maintmarginreq": {"amount": 0.0, "currency": "USD"},
            "realizedpnl": {"amount": 0.0, "currency": "USD"},
            "unrealizedpnl": {"amount": 0.0, "currency": "USD"},
            "is_mock": True,
        }

    def positions(self, account_id: str) -> List[Dict[str, Any]]:
        self._guard("/portfolio/positions")
        return list(self.positions_book.values())

    def set_position(self, conid: str, quantity: float, avg_cost: float,
                     market_price: Optional[float] = None) -> None:
        contract = self.contracts.get(conid)
        self.positions_book[conid] = {
            "conid": conid, "position": quantity, "avgCost": avg_cost,
            "avgPrice": avg_cost,
            "mktPrice": market_price if market_price is not None else avg_cost,
            "mktValue": quantity * (market_price if market_price is not None
                                    else avg_cost),
            "currency": "USD", "realizedPnl": 0.0, "unrealizedPnl": 0.0,
            "contractDesc": contract.symbol if contract else conid,
            "is_mock": True,
        }

    # ---------------- contracts ----------------

    def search_contracts(self, symbol: str,
                         sec_type: str = "STK") -> List[Dict[str, Any]]:
        self._guard("/iserver/secdef/search")
        matches = [c for c in self.contracts.values()
                   if c.symbol == symbol and c.sec_type == sec_type]
        if symbol in self.ambiguous_symbols:
            # Two listings the discriminators CANNOT separate: same
            # security type, same currency, different venue. This is
            # the real ambiguity — a currency or type difference is
            # resolvable, so a mock that only produced those would
            # never exercise the path that refuses to choose.
            matches = matches + [
                MockContract(
                    conid=f"{symbol}-ARCA", symbol=symbol, exchange="ARCA",
                    primary_exchange="ARCA", currency="USD",
                    sec_type=sec_type,
                    company_name=f"{symbol} (secondary US listing)"),
                MockContract(
                    conid=f"{symbol}-LSE", symbol=symbol, exchange="LSE",
                    primary_exchange="LSE", currency="GBP",
                    sec_type=sec_type,
                    company_name=f"{symbol} (London listing)"),
            ]
        return [c.as_search_result() for c in matches]

    def contract_details(self, conid: str) -> Dict[str, Any]:
        self._guard("/iserver/contract/info")
        contract = self.contracts.get(conid)
        if contract is None:
            raise IBKRError(
                category=IBKRErrorCategory.INVALID_CONTRACT,
                message=f"no security definition found for conid {conid}",
                ibkr_code=200, endpoint="/iserver/contract/info")
        return contract.as_details()

    # ---------------- market data ----------------

    def market_snapshot(self, conids: Sequence[str],
                        fields: Sequence[str] = ()) -> List[Dict[str, Any]]:
        self._guard("/iserver/marketdata/snapshot")
        if not self.market_data_available:
            raise IBKRError(
                category=IBKRErrorCategory.MARKET_DATA_ERROR,
                message="not subscribed to market data for this instrument",
                ibkr_code=354, endpoint="/iserver/marketdata/snapshot")
        return [self.quotes[c] for c in conids if c in self.quotes]

    # ---------------- orders ----------------

    def live_orders(self) -> List[Dict[str, Any]]:
        self._guard("/iserver/account/orders")
        return [o.as_payload() for o in self.orders.values()]

    def order_status(self, order_id: str) -> Optional[Dict[str, Any]]:
        self._guard("/iserver/account/order/status")
        if self.raise_on_status:
            raise IBKRError(
                category=IBKRErrorCategory.CONNECTION_ERROR,
                message="could not query order status",
                endpoint="/iserver/account/order/status")
        if order_id in self.unknown_orders:
            return None
        order = self.orders.get(order_id)
        return order.as_payload() if order else None

    def place_order(self, account_id: str,
                    order: Dict[str, Any]) -> Dict[str, Any]:
        self._guard("/iserver/account/orders")
        self.place_calls += 1

        if self.raise_on_place is not None:
            raise IBKRError(category=self.raise_on_place,
                            message="mock failure on place",
                            endpoint="/iserver/account/orders")

        order_id = f"ib-{next(self._ids):06d}"
        record = MockOrder(
            order_id=order_id, account_id=account_id,
            conid=str(order.get("conid", "")), side=str(order.get("side", "BUY")),
            quantity=float(order.get("quantity", 0.0)),
            order_type=str(order.get("orderType", "MKT")),
            limit_price=order.get("price"), stop_price=order.get("auxPrice"),
            time_in_force=str(order.get("tif", "DAY")),
            client_order_id=str(order.get("cOID", "")),
            at=datetime.now(timezone.utc))

        if self.timeout_on_place:
            # The dangerous case: the venue accepted and we never heard.
            record.status = "Submitted"
            record.remaining = record.quantity
            self.orders[order_id] = record
            raise IBKRError(
                category=IBKRErrorCategory.TIMEOUT,
                message="request timed out waiting for IBKR",
                endpoint="/iserver/account/orders")

        if self.reject_on_place:
            record.status = "Rejected"
            self.orders[order_id] = record
            return {"order_id": order_id, "order_status": "Rejected",
                    "text": "order rejected by mock venue", "is_mock": True}

        if self.requires_confirmation:
            # IBKR answers with a question rather than an ack. Nothing
            # is placed until it is answered.
            self._pending_reply = order_id
            self._pending_order = record
            return {"id": f"reply-{order_id}",
                    "message": ["This order will be placed outside regular "
                                "trading hours. Proceed?"],
                    "is_mock": True}

        record.remaining = record.quantity
        self.orders[order_id] = record
        return {"order_id": order_id, "order_status": "Submitted",
                "is_mock": True}

    def reply(self, reply_id: str, confirmed: bool = True) -> Dict[str, Any]:
        self._guard("/iserver/reply")
        if not confirmed or self._pending_reply is None:
            return {"order_status": "Cancelled", "is_mock": True}
        record = self._pending_order
        record.remaining = record.quantity
        self.orders[record.order_id] = record
        self._pending_reply = None
        return {"order_id": record.order_id, "order_status": "Submitted",
                "is_mock": True}

    def cancel_order(self, account_id: str, order_id: str) -> Dict[str, Any]:
        self._guard("/iserver/account/order")
        self.cancel_calls += 1
        order = self.orders.get(order_id)
        if order is None:
            raise IBKRError(
                category=IBKRErrorCategory.BROKER_REJECTION,
                message="order to cancel not found", ibkr_code=10147,
                endpoint="/iserver/account/order")
        if order.status == "Filled":
            raise IBKRError(
                category=IBKRErrorCategory.BROKER_REJECTION,
                message="cannot cancel an order that is already filled",
                ibkr_code=10148, endpoint="/iserver/account/order")
        order.status = "Cancelled"
        return {"order_id": order_id, "msg": "Request was submitted",
                "is_mock": True}

    def executions(self, days: int = 1) -> List[Dict[str, Any]]:
        self._guard("/iserver/account/trades")
        if self.duplicate_executions:
            # A venue replaying its recent executions after a reconnect
            # — the ordinary case, not a fault.
            return self.executions_log + self.executions_log
        return list(self.executions_log)

    # ---------------- driving the mock ----------------

    def fill(self, order_id: str, quantity: float, price: float,
             commission: float = 1.0, at: Optional[datetime] = None,
             execution_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Execute part or all of an order, as the venue would.

        Updates the order, the position book and the cash, then appends
        an execution — so a test that fills twice sees the same
        arithmetic a real venue would produce.
        """
        order = self.orders[order_id]
        order.filled += quantity
        order.remaining = max(0.0, order.quantity - order.filled)
        previous = (order.avg_price or 0.0) * (order.filled - quantity)
        order.avg_price = (previous + quantity * price) / order.filled
        order.status = "Filled" if order.remaining <= 1e-9 else "Submitted"
        moment = at or datetime.now(timezone.utc)
        order.at = moment

        signed = quantity if order.side.upper() == "BUY" else -quantity
        existing = self.positions_book.get(order.conid)
        held = float(existing["position"]) if existing else 0.0
        self.set_position(order.conid, held + signed, price, price)
        self.cash -= signed * price * 1.0 + commission

        execution = {
            "execution_id": execution_id or f"ex-{next(self._exec_ids):06d}",
            "order_ref": order.client_order_id,
            "orderId": order_id,
            "conid": order.conid,
            "symbol": (self.contracts[order.conid].symbol
                       if order.conid in self.contracts else order.conid),
            "side": "B" if order.side.upper() == "BUY" else "S",
            "size": quantity, "price": price,
            "trade_time_r": int(moment.timestamp() * 1000),
            "commission": commission, "net_amount": quantity * price,
            "exchange": "SMART", "currency": "USD",
            "is_mock": True,
        }
        self.executions_log.append(execution)
        return execution

    def set_status(self, order_id: str, status: str) -> None:
        self.orders[order_id].status = status
