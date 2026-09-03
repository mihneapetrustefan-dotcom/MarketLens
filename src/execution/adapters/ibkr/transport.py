"""
src/execution/adapters/ibkr/transport.py
---------------------------------------------
The IBKR transport boundary (Phase 15, spec §5, §40, §42, §47).

WHY A TRANSPORT LAYER INSIDE AN ADAPTER
-------------------------------------------
Phase 14 draws one boundary: the core does not know which broker it is
talking to. This module draws a second one, inside the IBKR adapter:
the adapter does not know which IBKR *interface* it is talking to.

That matters because IBKR has two, with entirely different shapes —
the Client Portal Web API (REST over a local gateway) and the TWS API
(a socket protocol into a running desktop application). Phase 15 uses
the first. If the second is ever needed, it implements `IBKRTransport`
and nothing in the mapper, the contract resolver or the gateway
changes.

It also makes the adapter testable. `MockIBKRTransport` — in its own
module — is a deterministic double that can time out, disconnect,
duplicate an execution and deliver events out of order on command.
Without this seam, every one of those tests would need a real IBKR
session, and the suite would stop being deterministic.

WHAT THIS LAYER RETURNS
---------------------------
Raw IBKR-shaped dictionaries, unmapped. Translation belongs to
`mapper.py`, and keeping the two apart means a change to IBKR's wire
format touches this file only.

WHAT IT DOES NOT DO
-----------------------
Authenticate. The Client Portal Gateway holds the session — the human
logs into it, and this process talks to an already-authenticated
gateway over localhost. There is no credential parameter anywhere in
this file, and `connect()` takes none, matching the Phase 14 contract.

`is_authenticated()` therefore ASKS the gateway rather than asserting
anything, and a false answer is an operational condition to report,
not an error to retry.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

from src.execution.adapters.ibkr.config import IBKRConfig
from src.execution.adapters.ibkr.errors import (
    IBKRError, IBKRErrorCategory, scrub, translate,
)


@dataclass
class AuthStatus:
    """
    What the gateway says about its own session.

    Four separate booleans because they fail separately, and the
    difference matters: `competing` means another session took over,
    which needs a human, while `connected=False` might resolve on its
    own.
    """
    authenticated: bool = False
    connected: bool = False
    competing: bool = False
    message: str = ""

    @property
    def usable(self) -> bool:
        return self.authenticated and self.connected and not self.competing


@dataclass
class TransportResponse:
    """One raw exchange with IBKR, kept for audit and debugging."""
    endpoint: str
    method: str
    status: int
    body: Any
    latency_ms: float
    at: Optional[datetime] = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class IBKRTransport(ABC):
    """
    The seam between the adapter and an IBKR interface.

    Everything below is IBKR-shaped and everything above it is
    canonical. Implementations return raw dictionaries; they do not
    translate, and they do not decide policy.
    """

    #: Which IBKR interface this is, for reporting.
    name: str = "abstract"

    # ---------------- session ----------------

    @abstractmethod
    def is_authenticated(self) -> AuthStatus:
        """
        Ask the gateway about its session. Never asserts, never logs in.
        """

    @abstractmethod
    def keepalive(self) -> bool:
        """
        Tickle the session so it does not lapse.

        The Client Portal session expires after a few minutes of
        inactivity, so a long-lived process must do this. Returns
        whether the session is still good.
        """

    @abstractmethod
    def close(self) -> None:
        ...

    # ---------------- account ----------------

    @abstractmethod
    def accounts(self) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def account_summary(self, account_id: str) -> Dict[str, Any]:
        ...

    @abstractmethod
    def positions(self, account_id: str) -> List[Dict[str, Any]]:
        ...

    # ---------------- contracts ----------------

    @abstractmethod
    def search_contracts(self, symbol: str,
                         sec_type: str = "STK") -> List[Dict[str, Any]]:
        """
        Symbol to candidate contracts. May legitimately return several
        — resolving that ambiguity is `contracts.py`'s job, not this
        layer's.
        """

    @abstractmethod
    def contract_details(self, conid: str) -> Dict[str, Any]:
        ...

    # ---------------- market data ----------------

    @abstractmethod
    def market_snapshot(self, conids: Sequence[str],
                        fields: Sequence[str] = ()) -> List[Dict[str, Any]]:
        ...

    # ---------------- orders ----------------

    @abstractmethod
    def live_orders(self) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def order_status(self, order_id: str) -> Optional[Dict[str, Any]]:
        """
        Ask about one order. The call that resolves an UNKNOWN state,
        so it must exist even where IBKR makes it awkward.
        """

    @abstractmethod
    def place_order(self, account_id: str,
                    order: Dict[str, Any]) -> Dict[str, Any]:
        ...

    @abstractmethod
    def cancel_order(self, account_id: str, order_id: str) -> Dict[str, Any]:
        ...

    @abstractmethod
    def executions(self, days: int = 1) -> List[Dict[str, Any]]:
        ...

    def reply(self, reply_id: str, confirmed: bool = True) -> Dict[str, Any]:
        """
        Answer an IBKR order-confirmation question.

        IBKR frequently responds to a submission with a question rather
        than an acknowledgement ("this order will be placed outside
        regular trading hours, proceed?"). A transport that cannot
        answer would appear to submit orders that never arrive.

        The default refuses, so an implementation that cannot do this
        says so rather than silently confirming something a human never
        saw.
        """
        raise NotImplementedError(
            f"{self.name} cannot answer IBKR order confirmations")


class ClientPortalTransport(IBKRTransport):
    """
    The Client Portal Web API, over a locally running gateway.

    HOW IT AUTHENTICATES: it does not. The gateway is launched
    separately and a human logs into it in a browser; this class talks
    to `https://localhost:5000/v1/api` and inherits that session
    through the cookie jar the HTTP session holds. No credential
    reaches this process.

    WHY TLS VERIFICATION IS OFF BY DEFAULT: the gateway serves a
    self-signed certificate on localhost. `IBKRConfig` refuses to allow
    that for any non-local host, so the exemption cannot quietly become
    a hole against a remote endpoint.
    """

    name = "client_portal"

    def __init__(self, config: IBKRConfig, session: Optional[Any] = None):
        self.config = config
        self._session = session
        self._history: Deque[TransportResponse] = deque(maxlen=200)
        self._request_times: Deque[datetime] = deque()
        self._last_error: Optional[IBKRError] = None

    # ---------------- plumbing ----------------

    def _ensure_session(self):
        """
        Build the HTTP session lazily.

        `requests` is already present through an existing dependency,
        so the adapter adds nothing to `requirements.txt`. Imported
        here rather than at module scope so that importing this module
        — which the tests and the mock do — never requires it.
        """
        if self._session is not None:
            return self._session
        try:
            import requests
        except ImportError as error:                     # pragma: no cover
            raise IBKRError(
                category=IBKRErrorCategory.CONNECTION_ERROR,
                message="the requests library is required for the Client "
                        "Portal transport",
                endpoint="") from error
        self._session = requests.Session()
        return self._session

    def _throttle(self, now: datetime) -> None:
        """
        A sliding request budget (spec §40).

        IBKR paces per endpoint rather than publishing one global
        number, so a conservative shared budget is applied instead of
        claiming a limit the documentation does not state. Refuses
        rather than sleeping: a blocking wait inside a batch job looks
        like a hang.
        """
        cutoff = now - timedelta(seconds=60)
        while self._request_times and self._request_times[0] < cutoff:
            self._request_times.popleft()
        if len(self._request_times) >= self.config.max_requests_per_minute:
            raise IBKRError(
                category=IBKRErrorCategory.RATE_LIMIT_ERROR,
                message=(f"local request budget of "
                         f"{self.config.max_requests_per_minute}/min is "
                         f"exhausted; the request was not sent"),
                endpoint="")
        self._request_times.append(now)

    def request(self, method: str, endpoint: str,
                payload: Optional[Dict[str, Any]] = None,
                params: Optional[Dict[str, Any]] = None) -> Any:
        """
        One call to the gateway, translated on failure.

        Every IBKR-shaped failure leaves here as an `IBKRError` with a
        category, so no caller upstream ever handles a `requests`
        exception or an HTTP status.
        """
        session = self._ensure_session()
        now = datetime.now(timezone.utc)
        self._throttle(now)

        url = f"{self.config.base_url}{endpoint}"
        started = time.perf_counter()
        try:
            response = session.request(
                method, url, json=payload, params=params,
                timeout=self.config.timeout_seconds,
                verify=self.config.verify_tls)
        except Exception as error:                       # noqa: BLE001
            elapsed = (time.perf_counter() - started) * 1000.0
            name = type(error).__name__
            # A timeout is NOT a connection error. The request may have
            # reached IBKR, and the difference decides whether a retry
            # is allowed at all.
            category = (IBKRErrorCategory.TIMEOUT
                        if "Timeout" in name
                        else IBKRErrorCategory.CONNECTION_ERROR)
            self._last_error = IBKRError(
                category=category,
                message=f"{name}: {error}", endpoint=endpoint,
                context={"latency_ms": round(elapsed, 2)})
            raise self._last_error from error

        elapsed = (time.perf_counter() - started) * 1000.0
        try:
            body = response.json()
        except ValueError:
            body = response.text

        record = TransportResponse(
            endpoint=endpoint, method=method, status=response.status_code,
            body=scrub(body), latency_ms=round(elapsed, 2), at=now)
        self._history.append(record)

        if not record.ok:
            message = ""
            code = None
            if isinstance(body, dict):
                message = str(body.get("error") or body.get("message") or "")
                raw_code = body.get("code")
                code = int(raw_code) if isinstance(raw_code, int) else None
            self._last_error = translate(
                message=message or f"HTTP {response.status_code}",
                ibkr_code=code, http_status=response.status_code,
                endpoint=endpoint, latency_ms=round(elapsed, 2))
            raise self._last_error

        return body

    @property
    def history(self) -> List[TransportResponse]:
        return list(self._history)

    @property
    def last_error(self) -> Optional[IBKRError]:
        return self._last_error

    # ---------------- session ----------------

    def is_authenticated(self) -> AuthStatus:
        try:
            body = self.request("POST", "/iserver/auth/status")
        except IBKRError as error:
            return AuthStatus(message=error.message)
        if not isinstance(body, dict):
            return AuthStatus(message="unexpected auth status payload")
        return AuthStatus(
            authenticated=bool(body.get("authenticated")),
            connected=bool(body.get("connected")),
            competing=bool(body.get("competing")),
            message=str(body.get("message") or ""))

    def keepalive(self) -> bool:
        try:
            body = self.request("POST", "/tickle")
        except IBKRError:
            return False
        if isinstance(body, dict):
            session = body.get("iserver", {}).get("authStatus", {})
            return bool(session.get("authenticated"))
        return True

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:                            # noqa: BLE001
                pass
            self._session = None

    # ---------------- account ----------------

    def accounts(self) -> List[Dict[str, Any]]:
        body = self.request("GET", "/iserver/accounts")
        if isinstance(body, dict):
            ids = body.get("accounts") or []
            return [{"accountId": account_id} for account_id in ids]
        return body if isinstance(body, list) else []

    def account_summary(self, account_id: str) -> Dict[str, Any]:
        body = self.request("GET", f"/portfolio/{account_id}/summary")
        return body if isinstance(body, dict) else {}

    def positions(self, account_id: str) -> List[Dict[str, Any]]:
        body = self.request("GET", f"/portfolio/{account_id}/positions/0")
        return body if isinstance(body, list) else []

    # ---------------- contracts ----------------

    def search_contracts(self, symbol: str,
                         sec_type: str = "STK") -> List[Dict[str, Any]]:
        body = self.request("POST", "/iserver/secdef/search",
                            payload={"symbol": symbol, "secType": sec_type,
                                     "name": False})
        return body if isinstance(body, list) else []

    def contract_details(self, conid: str) -> Dict[str, Any]:
        body = self.request("GET", f"/iserver/contract/{conid}/info")
        return body if isinstance(body, dict) else {}

    # ---------------- market data ----------------

    def market_snapshot(self, conids: Sequence[str],
                        fields: Sequence[str] = ()) -> List[Dict[str, Any]]:
        body = self.request(
            "GET", "/iserver/marketdata/snapshot",
            params={"conids": ",".join(conids),
                    "fields": ",".join(fields or ("31", "84", "86", "88"))})
        return body if isinstance(body, list) else []

    # ---------------- orders ----------------

    def live_orders(self) -> List[Dict[str, Any]]:
        body = self.request("GET", "/iserver/account/orders")
        if isinstance(body, dict):
            return body.get("orders") or []
        return body if isinstance(body, list) else []

    def order_status(self, order_id: str) -> Optional[Dict[str, Any]]:
        try:
            body = self.request("GET", f"/iserver/account/order/status/{order_id}")
        except IBKRError as error:
            if error.category is IBKRErrorCategory.INVALID_CONTRACT:
                # IBKR answers "unknown order" with a 404. That is a
                # real answer — the venue has no record — and must be
                # distinguishable from "we could not ask".
                return None
            raise
        return body if isinstance(body, dict) else None

    def place_order(self, account_id: str,
                    order: Dict[str, Any]) -> Dict[str, Any]:
        body = self.request("POST", f"/iserver/account/{account_id}/orders",
                            payload={"orders": [order]})
        if isinstance(body, list):
            return body[0] if body else {}
        return body if isinstance(body, dict) else {}

    def reply(self, reply_id: str, confirmed: bool = True) -> Dict[str, Any]:
        body = self.request("POST", f"/iserver/reply/{reply_id}",
                            payload={"confirmed": bool(confirmed)})
        if isinstance(body, list):
            return body[0] if body else {}
        return body if isinstance(body, dict) else {}

    def cancel_order(self, account_id: str, order_id: str) -> Dict[str, Any]:
        body = self.request(
            "DELETE", f"/iserver/account/{account_id}/order/{order_id}")
        return body if isinstance(body, dict) else {}

    def executions(self, days: int = 1) -> List[Dict[str, Any]]:
        body = self.request("GET", "/iserver/account/trades",
                            params={"days": max(1, int(days))})
        return body if isinstance(body, list) else []
