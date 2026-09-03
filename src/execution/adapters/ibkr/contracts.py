"""
src/execution/adapters/ibkr/contracts.py
---------------------------------------------
IBKR contract resolution (Phase 15, spec §14, §15, §16, §18).

THE ASSUMPTION THIS MODULE EXISTS TO REFUSE
-----------------------------------------------
`ticker == instrument`. It is false at IBKR more often than anywhere
else, because IBKR lists the same symbol on several venues in several
currencies and several security types. `AAPL` alone identifies at
least a US stock and a European listing; `IBM` alone can be a stock or
an option chain root.

So resolution needs symbol PLUS security type PLUS currency PLUS
exchange, and when those still leave more than one candidate the
answer is a structured ambiguity — never a pick.

WHY AMBIGUITY IS RETURNED RATHER THAN RESOLVED
--------------------------------------------------
Choosing the first result would work almost always, and the times it
did not would be a trade in the wrong security on the wrong exchange
in the wrong currency. That failure is silent, expensive and hard to
detect after the fact. An unresolved instrument that refuses to trade
is a phone call; a resolved-to-the-wrong-contract instrument is a
position nobody meant to hold.

`conid` IS THE REAL IDENTIFIER
----------------------------------
Once resolved, the conid is what everything downstream uses. It is
stable, unambiguous, and IBKR's own key. The symbol is kept for humans
and never used for routing.

CACHING IS PERSISTENT AND DELIBERATE
----------------------------------------
A resolved contract is written into the Phase 14
`broker_instrument_mapping` table plus an IBKR-specific side table, so
the next run does not re-resolve and cannot reach a different answer.
Re-resolving on every run would mean the mapping could silently change
under a listing change — which is exactly the moment a human should be
told rather than a machine deciding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.domain.broker_models import (
    BrokerInstrumentMapping, ExecutionRejectCode,
)
from src.execution.adapters.ibkr.errors import IBKRError, IBKRErrorCategory
from src.execution.adapters.ibkr.mapper import _number
from src.execution.adapters.ibkr.transport import IBKRTransport


@dataclass
class IBKRContract:
    """
    A fully identified IBKR contract.

    `conid` is the identity. Everything else is description, kept
    because a human reading a reconciliation report needs to recognise
    what they are looking at.
    """
    conid: str
    symbol: str
    sec_type: str = "STK"
    currency: str = "USD"
    exchange: str = "SMART"
    primary_exchange: str = ""
    trading_class: str = ""
    company_name: str = ""
    multiplier: float = 1.0
    expiry: Optional[str] = None
    strike: Optional[float] = None
    right: Optional[str] = None
    tick_size: Optional[float] = None
    size_increment: Optional[float] = None
    minimum_size: Optional[float] = None

    def describe(self) -> str:
        parts = [self.symbol, self.sec_type, self.currency]
        if self.primary_exchange:
            parts.append(self.primary_exchange)
        if self.expiry:
            parts.append(self.expiry)
        return " ".join(parts) + f" (conid {self.conid})"

    def as_mapping(self, instrument_id: str,
                   broker_id: str = "ibkr") -> BrokerInstrumentMapping:
        """
        Fold into the Phase 14 mapping the rest of the system uses.

        The conid goes into `broker_payload`, which the core never
        interprets — exactly the extension point Phase 14 left for a
        venue with its own identifier scheme.
        """
        return BrokerInstrumentMapping(
            canonical_instrument_id=instrument_id,
            broker_id=broker_id,
            broker_symbol=self.symbol,
            venue=self.primary_exchange or self.exchange,
            asset_class=_ASSET_CLASS.get(self.sec_type, "stock"),
            currency=self.currency,
            tick_size=self.tick_size,
            minimum_quantity=self.minimum_size,
            quantity_increment=self.size_increment,
            price_precision=None,
            contract_multiplier=self.multiplier,
            timezone_name="UTC",
            trading_hours="",
            tradable=True,
            broker_payload={
                "conid": self.conid,
                "sec_type": self.sec_type,
                "exchange": self.exchange,
                "primary_exchange": self.primary_exchange,
                "trading_class": self.trading_class,
                "company_name": self.company_name,
                "expiry": self.expiry,
                "strike": self.strike,
                "right": self.right,
            })


#: IBKR security type to this project's asset-class vocabulary.
_ASSET_CLASS: Dict[str, str] = {
    "STK": "stock",
    "ETF": "etf",
    "CASH": "forex",
    "FUT": "futures",
    "OPT": "options",
    "CRYPTO": "crypto",
    "IND": "index",
}


@dataclass
class ContractResolution:
    """
    A resolution outcome that carries its own failure.

    `candidates` is populated on ambiguity so an operator can see what
    the choice actually was, and add the discriminator that settles
    it, without going to IBKR themselves.
    """
    contract: Optional[IBKRContract] = None
    ambiguous: bool = False
    candidates: List[IBKRContract] = field(default_factory=list)
    code: Optional[ExecutionRejectCode] = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.contract is not None and not self.ambiguous

    def explain(self) -> str:
        if self.ok:
            return f"Resolved to {self.contract.describe()}."
        if self.ambiguous:
            listing = "; ".join(c.describe() for c in self.candidates[:6])
            return (f"IBKR returned {len(self.candidates)} contracts and none "
                    f"was uniquely identified: {listing}. Narrow the request "
                    f"with a currency, exchange or security type.")
        return self.detail or "The contract could not be resolved."


@dataclass
class ContractQuery:
    """
    What we know about the instrument we want.

    `symbol` alone is never enough, which is why `sec_type` and
    `currency` have defaults that are honest for this project's
    universe rather than being Optional and quietly skipped.
    """
    symbol: str
    sec_type: str = "STK"
    currency: str = "USD"
    exchange: Optional[str] = None
    primary_exchange: Optional[str] = None
    expiry: Optional[str] = None

    def describe(self) -> str:
        parts = [self.symbol, self.sec_type, self.currency]
        if self.primary_exchange:
            parts.append(self.primary_exchange)
        return " ".join(parts)


def _contract_from_search(payload: Dict[str, Any],
                          query: ContractQuery) -> IBKRContract:
    """One search result to a contract, with what the search reveals."""
    sections = payload.get("sections") or []
    exchange = query.exchange or "SMART"
    sec_type = query.sec_type
    for section in sections:
        if str(section.get("secType", "")).upper() == query.sec_type.upper():
            sec_type = str(section.get("secType"))
            if section.get("exchange"):
                exchange = str(section["exchange"]).split(",")[0]
            break

    return IBKRContract(
        conid=str(payload.get("conid") or ""),
        symbol=str(payload.get("symbol") or query.symbol),
        sec_type=sec_type,
        currency=str(payload.get("currency") or query.currency),
        exchange=exchange,
        primary_exchange=str(payload.get("description") or ""),
        company_name=str(payload.get("companyName") or ""))


def _enrich(contract: IBKRContract, details: Dict[str, Any]) -> IBKRContract:
    """Fold contract details — the trading rules — into a contract."""
    rules = details.get("rules") or {}
    contract.trading_class = str(details.get("tradingClass")
                                 or contract.trading_class or "")
    contract.primary_exchange = str(details.get("listingExchange")
                                    or contract.primary_exchange or "")
    if details.get("exchange"):
        contract.exchange = str(details["exchange"]).split(",")[0]
    multiplier = _number(details.get("multiplier"))
    if multiplier:
        contract.multiplier = multiplier
    contract.tick_size = _number(rules.get("increment"))
    contract.size_increment = _number(rules.get("sizeIncrement"))
    contract.minimum_size = _number(rules.get("minSize"))
    return contract


class ContractResolver:
    """
    Symbol plus discriminators to one IBKR contract, or an ambiguity.

    Caches in memory for the process and persists through the caller,
    so a resolved contract is resolved once.
    """

    def __init__(self, transport: IBKRTransport, broker_id: str = "ibkr"):
        self.transport = transport
        self.broker_id = broker_id
        self._by_query: Dict[Tuple[str, str, str], ContractResolution] = {}
        self._by_conid: Dict[str, IBKRContract] = {}

    def resolve(self, query: ContractQuery,
                use_cache: bool = True) -> ContractResolution:
        """
        Find the one contract this query identifies.

        Filters candidates by every discriminator the caller supplied,
        then requires exactly one survivor. Two survivors is an
        ambiguity, not a choice.
        """
        key = (query.symbol.upper(), query.sec_type.upper(),
               query.currency.upper())
        if use_cache and key in self._by_query:
            return self._by_query[key]

        try:
            raw = self.transport.search_contracts(query.symbol, query.sec_type)
        except IBKRError as error:
            resolution = ContractResolution(
                code=(ExecutionRejectCode.NO_INSTRUMENT_MAPPING
                      if error.category is IBKRErrorCategory.INVALID_CONTRACT
                      else ExecutionRejectCode.ADAPTER_ERROR),
                detail=error.message)
            return resolution

        candidates = [_contract_from_search(item, query) for item in raw
                      if item.get("conid")]
        if not candidates:
            resolution = ContractResolution(
                code=ExecutionRejectCode.NO_INSTRUMENT_MAPPING,
                detail=f"IBKR returned no contract for {query.describe()}")
            self._by_query[key] = resolution
            return resolution

        narrowed = self._narrow(candidates, query)

        if len(narrowed) > 1:
            # Deliberately not a choice. See the module docstring.
            resolution = ContractResolution(
                ambiguous=True, candidates=narrowed,
                code=ExecutionRejectCode.NO_INSTRUMENT_MAPPING,
                detail=f"{len(narrowed)} contracts match {query.describe()}")
            self._by_query[key] = resolution
            return resolution

        if not narrowed:
            resolution = ContractResolution(
                code=ExecutionRejectCode.NO_INSTRUMENT_MAPPING,
                detail=(f"IBKR returned {len(candidates)} contract(s) for "
                        f"{query.symbol} but none matched "
                        f"{query.describe()}"),
                candidates=candidates)
            self._by_query[key] = resolution
            return resolution

        contract = narrowed[0]
        try:
            contract = _enrich(contract,
                               self.transport.contract_details(contract.conid))
        except IBKRError as error:
            # The trading rules are enrichment. Losing them costs
            # precision on tick and lot size, not identity, so the
            # resolution stands and the reason is recorded.
            resolution = ContractResolution(
                contract=contract,
                detail=f"contract details unavailable: {error.message}")
            self._by_query[key] = resolution
            self._by_conid[contract.conid] = contract
            return resolution

        resolution = ContractResolution(contract=contract)
        self._by_query[key] = resolution
        self._by_conid[contract.conid] = contract
        return resolution

    @staticmethod
    def _narrow(candidates: Sequence[IBKRContract],
                query: ContractQuery) -> List[IBKRContract]:
        """
        Apply every discriminator the caller gave.

        Each filter is applied only when the caller supplied that
        discriminator — a filter on an unspecified field would silently
        exclude the right answer.
        """
        narrowed = list(candidates)
        narrowed = [c for c in narrowed
                    if c.sec_type.upper() == query.sec_type.upper()]
        narrowed = [c for c in narrowed
                    if c.currency.upper() == query.currency.upper()]
        if query.primary_exchange:
            wanted = query.primary_exchange.upper()
            narrowed = [c for c in narrowed
                        if wanted in (c.primary_exchange or "").upper()]
        if query.exchange:
            wanted = query.exchange.upper()
            narrowed = [c for c in narrowed
                        if wanted in (c.exchange or "").upper()]
        return narrowed

    def by_conid(self, conid: str) -> Optional[IBKRContract]:
        if conid in self._by_conid:
            return self._by_conid[conid]
        try:
            details = self.transport.contract_details(conid)
        except IBKRError:
            return None
        contract = IBKRContract(
            conid=conid,
            symbol=str(details.get("symbol") or ""),
            sec_type=str(details.get("instrument_type") or "STK"),
            currency=str(details.get("currency") or "USD"))
        contract = _enrich(contract, details)
        self._by_conid[conid] = contract
        return contract

    @property
    def cached(self) -> Dict[str, IBKRContract]:
        return dict(self._by_conid)


def conid_of(mapping: Optional[BrokerInstrumentMapping]) -> Optional[str]:
    """
    The IBKR conid carried on a Phase 14 mapping.

    A helper rather than an inline lookup, so that every read of the
    conid goes through one place — and so nothing outside this package
    has to know that `broker_payload["conid"]` is where it lives.
    """
    if mapping is None:
        return None
    value = (mapping.broker_payload or {}).get("conid")
    return str(value) if value else None
