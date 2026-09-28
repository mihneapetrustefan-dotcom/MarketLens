"""
src/adaptive/flags.py
----------------------------------
Feature flags for the adaptive decision layer (Phase 25.9I-A).

EVERY FLAG IS OFF, AND OFF IS THE ONLY STATE THIS PHASE HAS
---------------------------------------------------------------
Nothing in 25.9I-A reads these flags to change behaviour, because none
of the behaviour exists yet. They are defined now so that 25.9I-B..H
each ship behind a switch that was already in the diagnostics, rather
than inventing one at the moment it is first needed.

WHAT A FLAG CAN AND CANNOT DO
---------------------------------
A flag here can enable an ANALYSIS path in a later phase. No flag can
enable a broker write, place an order, or change an environment: those
are governed by `ExecutionSafety` (Phase 14) and `TradingModeStore`
(Phase 25.9E), and this module deliberately imports neither. A flag
that could reach them would be a second source of truth for the one
question this project refuses to answer twice.

FAIL CLOSED, AND SAY WHY
----------------------------
An unreadable value does not raise and does not pass: it resolves to
OFF and records a reason. Raising would let a typo in an unrelated
variable stop a capture session that was running fine; passing would
let one enable a subsystem nobody chose. `AdaptiveFlags.problems`
carries every rejected value so the diagnostics can show it.

DEPENDENCIES ARE ENFORCED, NOT DOCUMENTED
---------------------------------------------
Dynamic exits without a position thesis would be exits with no stated
reason to exist. Where one flag needs another, `resolve()` turns the
dependent flag off and records why, so an operator sees the refusal
instead of a subsystem that half-runs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Tuple

#: One prefix, so `env | grep MARKETLENS_ADAPTIVE` shows the whole layer.
ENV_PREFIX = "MARKETLENS_ADAPTIVE_"

#: The master switch. Off, every individual flag is still independently
#: readable -- it gates nothing on its own and exists so that a future
#: operator can turn the layer off in one place.
ENV_LAYER = ENV_PREFIX + "LAYER_ENABLED"

TRUE_VALUES = frozenset({"1", "true", "yes", "on", "enabled"})
FALSE_VALUES = frozenset({"", "0", "false", "no", "off", "disabled"})

#: flag name -> environment variable
FLAG_ENV: Dict[str, str] = {
    "adaptive_universe_enabled": ENV_PREFIX + "UNIVERSE_ENABLED",
    "adaptive_market_data_enabled": ENV_PREFIX + "MARKET_DATA_ENABLED",
    "market_data_cost_governor_enabled": ENV_PREFIX + "COST_GOVERNOR_ENABLED",
    "smart_capital_allocation_enabled": ENV_PREFIX + "CAPITAL_ALLOCATION_ENABLED",
    "position_thesis_enabled": ENV_PREFIX + "POSITION_THESIS_ENABLED",
    "dynamic_exit_enabled": ENV_PREFIX + "DYNAMIC_EXIT_ENABLED",
    "event_driven_position_reassessment_enabled":
        ENV_PREFIX + "EVENT_REASSESSMENT_ENABLED",
    "adaptive_execution_freshness_gate_enabled":
        ENV_PREFIX + "EXECUTION_FRESHNESS_GATE_ENABLED",
}

#: flag name -> the flag it cannot run without.
FLAG_DEPENDENCIES: Dict[str, str] = {
    # An exit recommendation with no thesis has nothing to revise.
    "dynamic_exit_enabled": "position_thesis_enabled",
    # Reassessment revises a thesis; without one there is nothing to revise.
    "event_driven_position_reassessment_enabled": "position_thesis_enabled",
    # Escalating to realtime data without the governor is spending with no
    # ceiling, which is the failure this layer exists to avoid.
    "adaptive_market_data_enabled": "market_data_cost_governor_enabled",
}


def _parse(raw: Optional[str], name: str,
           problems: Dict[str, str]) -> bool:
    """A value that is not plainly true or false is a problem, and is OFF."""
    if raw is None:
        return False
    text = raw.strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    problems[name] = (f"{name}={raw!r} is not a boolean; the flag stays off")
    return False


@dataclass(frozen=True)
class AdaptiveFlags:
    """
    The resolved state of the adaptive layer. Immutable once resolved.

    `layer_enabled` is informational: it does not force the individual
    flags on, and turning it on enables nothing by itself.
    """
    layer_enabled: bool = False
    adaptive_universe_enabled: bool = False
    adaptive_market_data_enabled: bool = False
    market_data_cost_governor_enabled: bool = False
    smart_capital_allocation_enabled: bool = False
    position_thesis_enabled: bool = False
    dynamic_exit_enabled: bool = False
    event_driven_position_reassessment_enabled: bool = False
    adaptive_execution_freshness_gate_enabled: bool = False
    problems: Tuple[str, ...] = ()
    suppressed: Tuple[str, ...] = ()

    @property
    def any_enabled(self) -> bool:
        return any(getattr(self, name) for name in FLAG_ENV)

    @property
    def enabled_flags(self) -> Tuple[str, ...]:
        return tuple(sorted(name for name in FLAG_ENV if getattr(self, name)))

    def is_enabled(self, name: str) -> bool:
        if name not in FLAG_ENV:
            raise KeyError(f"unknown adaptive flag: {name}")
        return bool(getattr(self, name))

    def describe(self) -> Dict[str, object]:
        """A diagnostics view. Contains no account, token or credential."""
        return {
            "layer_enabled": self.layer_enabled,
            "flags": {name: getattr(self, name) for name in sorted(FLAG_ENV)},
            "any_enabled": self.any_enabled,
            "problems": list(self.problems),
            "suppressed": list(self.suppressed),
            # Stated as data so a diagnostic cannot drift from the truth.
            "enables_broker_writes": False,
            "enables_live_trading": False,
        }


def resolve(environ: Optional[Mapping[str, str]] = None) -> AdaptiveFlags:
    """
    Read the flags from the environment. Absent, unreadable -> OFF.

    Pure with respect to its input: pass a mapping to resolve without
    touching the process environment, which is what the tests do.
    """
    env = os.environ if environ is None else environ
    problems: Dict[str, str] = {}

    values = {name: _parse(env.get(var), var, problems)
              for name, var in FLAG_ENV.items()}
    layer = _parse(env.get(ENV_LAYER), ENV_LAYER, problems)

    # Dependencies, after parsing: a flag whose prerequisite is off is
    # turned off here and the reason is kept.
    suppressed = []
    for name, prerequisite in FLAG_DEPENDENCIES.items():
        if values.get(name) and not values.get(prerequisite):
            values[name] = False
            suppressed.append(
                f"{name} requires {prerequisite}, which is off")

    return AdaptiveFlags(layer_enabled=layer,
                         problems=tuple(sorted(problems.values())),
                         suppressed=tuple(sorted(suppressed)),
                         **values)
