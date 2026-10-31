"""
src/capture/stack.py
----------------------------
Assemble the capture gateway from the canonical parts, and nothing else.

    IBKRConfig          environment, with ordering FORCED off
    transport           ClientPortalTransport (real) or MockIBKRTransport
    InstrumentRegistry  loaded from the capture store
    IBKRGateway         the one IBKR adapter, with the exchange calendar
    capture_only(...)   gateway AND transport refuse every venue write

There is no second adapter, no second calendar and no order path: the
trading stack (`src/trading/stack.py`) is not imported. `enabled=True`
is set because capture exists to talk to IBKR; it grants nothing,
since `ordering_enabled=False` is set in the same breath and the
guard would refuse a write even if it were not.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from src.execution.adapters.ibkr.config import IBKRConfig
from src.execution.adapters.ibkr.gateway import IBKRGateway
from src.execution.adapters.submission_guard import capture_only
from src.execution.instruments import InstrumentRegistry
from src.marketdata.calendar import USEquityCalendar


def build_capture_gateway(conn: sqlite3.Connection, *, mock: bool = False,
                          transport: Optional[Any] = None) -> Any:
    config = IBKRConfig.from_environment(enabled=True, ordering_enabled=False)
    if transport is None:
        if mock:
            from src.execution.adapters.ibkr.mock_transport import MockIBKRTransport
            transport = MockIBKRTransport(config)
        else:
            from src.execution.adapters.ibkr.transport import ClientPortalTransport
            transport = ClientPortalTransport(config)
    registry = InstrumentRegistry(conn)
    registry.load()
    gateway = IBKRGateway(config, transport, registry,
                          exchange_calendar=USEquityCalendar())
    return capture_only(gateway)
