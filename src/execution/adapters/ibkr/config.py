"""
src/execution/adapters/ibkr/config.py
------------------------------------------
Interactive Brokers configuration (Phase 15, spec §6, §7, §8, §46).

EVERYTHING COMES FROM THE ENVIRONMENT
-----------------------------------------
Same mechanism the existing collectors use — `os.environ.get` plus
GitHub Actions secrets. No new configuration system, because the
project already has one and a second would be the parallel architecture
Phase 14's §0 forbids.

THE THING THIS CONFIG DELIBERATELY CANNOT HOLD
--------------------------------------------------
An IBKR username, password, token or API key. Not "should not" — there
is no field for one, and that is a property of the transport choice
rather than of discipline.

The Client Portal Gateway authenticates the human, holds the session,
and this application talks to the gateway over localhost. So the
credential never enters this process at all. A TWS-API integration
would have had the same property; an OAuth integration would not, and
choosing CPAPI meant choosing not to need the field.

What IS configured: where the gateway is listening, which account,
timeouts, retry bounds, and two independent safety gates.

PAPER-FIRST, ENFORCED IN THE TYPE
-------------------------------------
`environment` may not be LIVE. Constructing a config that says
otherwise raises, before any connection is attempted — so a mistyped
environment variable fails at startup rather than at the venue.

And connecting is not permission to trade. `ordering_enabled` is a
SECOND flag, off by default, because an IBKR session existing is not a
reason for an order to exist (spec §46).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from src.domain.broker_models import ExecutionEnvironment

#: Environment variable names. Prefixed, so they cannot collide with
#: the market-data keys the collectors already read.
ENV_ENABLED = "IBKR_ENABLED"
ENV_ENVIRONMENT = "IBKR_ENVIRONMENT"
ENV_ACCOUNT_ID = "IBKR_ACCOUNT_ID"
ENV_HOST = "IBKR_HOST"
ENV_PORT = "IBKR_PORT"
ENV_BASE_PATH = "IBKR_BASE_PATH"
ENV_TIMEOUT = "IBKR_TIMEOUT_SECONDS"
ENV_RECONNECT = "IBKR_RECONNECT_ENABLED"
ENV_MAX_RETRIES = "IBKR_MAX_RETRIES"
ENV_ORDERING = "IBKR_PAPER_ORDERING_ENABLED"
ENV_VERIFY_TLS = "IBKR_VERIFY_TLS"
ENV_RATE_LIMIT = "IBKR_MAX_REQUESTS_PER_MINUTE"

#: The Client Portal Gateway's own default. It serves HTTPS on
#: localhost with a self-signed certificate, which is why
#: `verify_tls` defaults to False for a localhost host and why that
#: default is narrowed the moment the host is not local.
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 5000
DEFAULT_BASE_PATH = "/v1/api"

#: IBKR documents pacing rather than one hard number, and it differs
#: per endpoint. A conservative shared budget is used instead of
#: claiming a limit the documentation does not state.
DEFAULT_RATE_LIMIT_PER_MINUTE = 50


def _flag(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _number(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class IBKRConfigurationError(Exception):
    """Raised when configuration is unsafe or incoherent."""


@dataclass
class IBKRConfig:
    """
    Where the gateway is, which account, and what is permitted.

    Note the absence of every credential field. See the module
    docstring — that absence is the transport choice paying off, not an
    omission to be filled in later.
    """
    enabled: bool = False
    environment: ExecutionEnvironment = ExecutionEnvironment.PAPER
    account_id: Optional[str] = None

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    base_path: str = DEFAULT_BASE_PATH
    timeout_seconds: float = 15.0
    verify_tls: bool = False

    reconnect_enabled: bool = True
    max_retries: int = 5
    backoff_seconds: float = 1.0
    max_backoff_seconds: float = 60.0
    max_requests_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE

    #: The second gate. Connecting is not permission to trade.
    ordering_enabled: bool = False

    broker_id: str = "ibkr"

    def __post_init__(self):
        if self.environment.is_real_money:
            raise IBKRConfigurationError(
                "IBKR_ENVIRONMENT=live is refused. Phase 15 is paper-first "
                "and no real-money execution path exists in this repository.")
        if self.environment is not ExecutionEnvironment.PAPER:
            raise IBKRConfigurationError(
                f"IBKR_ENVIRONMENT must be 'paper' in this phase "
                f"(got {self.environment.value!r})")
        if self.port <= 0:
            raise IBKRConfigurationError(f"IBKR_PORT must be positive")
        if self.max_retries < 1:
            raise IBKRConfigurationError("IBKR_MAX_RETRIES must be at least 1")
        if self.verify_tls is False and not self._is_local():
            # Skipping certificate verification is defensible against a
            # gateway on this machine with a self-signed certificate.
            # Against anything else it is a hole, so it is refused
            # rather than warned about.
            raise IBKRConfigurationError(
                f"TLS verification may only be disabled for a gateway on "
                f"this machine (host is {self.host!r}). Set "
                f"{ENV_VERIFY_TLS}=true, or point at localhost.")

    def _is_local(self) -> bool:
        return self.host in ("localhost", "127.0.0.1", "::1", "[::1]")

    @property
    def base_url(self) -> str:
        return f"https://{self.host}:{self.port}{self.base_path}"

    @property
    def is_paper(self) -> bool:
        return self.environment is ExecutionEnvironment.PAPER

    @property
    def can_submit_orders(self) -> bool:
        """
        Both gates, and the environment. All three, every time.

        Written as one property so no caller can check two of the three
        and believe it has checked.
        """
        return (self.enabled and self.ordering_enabled and self.is_paper
                and not self.environment.is_real_money)

    def describe(self) -> Dict[str, Any]:
        """
        A reportable summary for the CLI and the dashboard.

        Contains no secret because the config holds none. This can be
        printed, logged and serialised into a dashboard payload
        without redaction.
        """
        return {
            "broker_id": self.broker_id,
            "enabled": self.enabled,
            "environment": self.environment.value,
            "account_id": self.account_id,
            "gateway": f"{self.host}:{self.port}{self.base_path}",
            "verify_tls": self.verify_tls,
            "timeout_seconds": self.timeout_seconds,
            "reconnect_enabled": self.reconnect_enabled,
            "max_retries": self.max_retries,
            "max_requests_per_minute": self.max_requests_per_minute,
            "ordering_enabled": self.ordering_enabled,
            "can_submit_orders": self.can_submit_orders,
            "live_execution": False,
            "holds_credentials": False,
        }

    @classmethod
    def from_environment(cls, **overrides: Any) -> "IBKRConfig":
        """
        Read the environment, then apply explicit overrides.

        Overrides exist for tests and for the CLI. They cannot make the
        environment LIVE — `__post_init__` refuses that regardless of
        where the value came from.
        """
        raw_environment = (os.environ.get(ENV_ENVIRONMENT) or "paper").strip().lower()
        try:
            environment = ExecutionEnvironment(raw_environment)
        except ValueError:
            raise IBKRConfigurationError(
                f"{ENV_ENVIRONMENT}={raw_environment!r} is not a known "
                f"environment. Expected 'paper'.") from None

        host = (os.environ.get(ENV_HOST) or DEFAULT_HOST).strip()
        values: Dict[str, Any] = dict(
            enabled=_flag(ENV_ENABLED, False),
            environment=environment,
            account_id=(os.environ.get(ENV_ACCOUNT_ID) or "").strip() or None,
            host=host,
            port=int(_number(ENV_PORT, DEFAULT_PORT)),
            base_path=(os.environ.get(ENV_BASE_PATH) or DEFAULT_BASE_PATH).strip(),
            timeout_seconds=_number(ENV_TIMEOUT, 15.0),
            verify_tls=_flag(ENV_VERIFY_TLS, False),
            reconnect_enabled=_flag(ENV_RECONNECT, True),
            max_retries=int(_number(ENV_MAX_RETRIES, 5)),
            max_requests_per_minute=int(
                _number(ENV_RATE_LIMIT, DEFAULT_RATE_LIMIT_PER_MINUTE)),
            ordering_enabled=_flag(ENV_ORDERING, False),
        )
        values.update(overrides)
        return cls(**values)


def paper_config(**overrides: Any) -> IBKRConfig:
    """A ready-to-use paper configuration, for tests and the mock."""
    defaults: Dict[str, Any] = dict(
        enabled=True, environment=ExecutionEnvironment.PAPER,
        account_id="DU0000000", ordering_enabled=False)
    defaults.update(overrides)
    return IBKRConfig(**defaults)
