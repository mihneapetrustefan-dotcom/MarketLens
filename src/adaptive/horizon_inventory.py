"""
src/adaptive/horizon_inventory.py
-----------------------------------------------
Which horizons exist, and what each can actually do (Phase 25.9I-F.5).

BUILT FROM EVIDENCE, NEVER FROM A LIST
------------------------------------------
The horizon set is not declared anywhere in this repository, so it is
derived from `research_labels.window_name` and cross-referenced against
`trained_models.label_name`. Writing `("d1", "d5", "d20")` into a
constant would have been a guess that happened to be wrong: the corpus
holds NINE horizons, four of them intraday.

Measured 2026-09-30 over 9,192 label rows:

    intraday_5m    554 abnormal-return labels   (+ volume labels)
    intraday_15m   554
    intraday_30m   554
    intraday_60m   554
    d1             544
    d3             531
    d5             458    <- the ONLY horizon with any model
    d10            286
    d20              7    <- SEALED, and label-immature

THE SEAL, ENFORCED IN SQL
-----------------------------
The protected horizon is allowed to report READINESS METADATA -- that
it exists, how many labels have matured, whether a model exists -- and
is never allowed to report an OUTCOME.

So this module never selects a label's value column. Not for the sealed
horizon, not for any of them: it counts rows and reads names. `COUNT(*)`
cannot leak a return, and a query that never names the value column
cannot be made to leak one by a later caller passing the wrong
argument. `PROTECTED_HORIZONS` is checked a second time before any
descriptor is returned, so even a corpus that renamed the window would
have to defeat two independent guards.

The 7 sealed-horizon labels are ALSO a maturity fact worth seeing: a
20-day horizon needs 20 trading days to resolve, so only the oldest
handful of the 581 observations have one. Even unsealed, it could not
support a model.

WHAT A HORIZON CAN DO IS NOT WHAT IT HAS
--------------------------------------------
`has_model` and `can_produce_signal` are different questions. A
protected horizon with a model still cannot produce a signal, and
`HorizonDescriptor.can_produce_signal` returns False for it by
construction rather than by the caller remembering.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from src.domain.signal_availability_models import HorizonDescriptor

#: Horizons sealed by a protected experiment. The experiment's
#: hypothesis is deliberately NOT restated here: repeating it inside the
#: adaptive layer would put the frozen specification somewhere it could
#: be read, edited or tuned, and the whole point of sealing it is that
#: this layer does not know what it says.
PROTECTED_HORIZONS: Dict[str, str] = {
    "d20": ("this horizon belongs to a sealed one-shot experiment; its "
            "outcome may not be read, inferred or consumed"),
}

#: Label suffixes that describe an OUTCOME. Listed so the guard below
#: can refuse to read them, not so anyone can.
OUTCOME_LABEL_SUFFIXES = ("abnormal_return", "raw_return")

#: Approximate trading-day length, for ordering and reporting only.
#: Intraday horizons carry minutes instead. Nothing decides anything
#: from these numbers.
_TRADING_DAYS = {"d1": 1, "d3": 3, "d5": 5, "d10": 10, "d20": 20}
_MINUTES = {"intraday_5m": 5, "intraday_15m": 15, "intraday_30m": 30,
            "intraday_60m": 60}


class ProtectedHorizonAccess(RuntimeError):
    """Something tried to read a sealed horizon's outcome."""


def is_protected(horizon_id: str) -> bool:
    return str(horizon_id).strip().lower() in PROTECTED_HORIZONS


def guard_protected(horizon_id: str, what: str = "outcome") -> None:
    """
    Refuse, loudly, before a protected horizon's outcome is touched.

    Called at every boundary where a value could escape. Raising is
    deliberate: a silent None would let a caller conclude "no edge"
    from a horizon it was never allowed to look at.
    """
    if is_protected(horizon_id):
        raise ProtectedHorizonAccess(
            f"{horizon_id} is sealed: {PROTECTED_HORIZONS[str(horizon_id).lower()]}"
            f" (refused: {what})")


@dataclass(frozen=True)
class HorizonInventory:
    """
    Every canonical horizon, in a deterministic order.

    Ordered intraday-first by duration, then daily by trading days, so
    two runs over the same corpus produce the same sequence and a
    report diff means something changed in the data.
    """
    horizons: Tuple[HorizonDescriptor, ...] = ()
    built_at: Optional[datetime] = None
    source: str = "research_labels + trained_models"

    def __iter__(self):
        return iter(self.horizons)

    def __len__(self) -> int:
        return len(self.horizons)

    def get(self, horizon_id: str) -> Optional[HorizonDescriptor]:
        wanted = str(horizon_id).strip().lower()
        for descriptor in self.horizons:
            if descriptor.horizon_id.lower() == wanted:
                return descriptor
        return None

    @property
    def ids(self) -> Tuple[str, ...]:
        return tuple(d.horizon_id for d in self.horizons)

    @property
    def protected(self) -> Tuple[HorizonDescriptor, ...]:
        return tuple(d for d in self.horizons if d.is_protected)

    @property
    def signal_capable(self) -> Tuple[HorizonDescriptor, ...]:
        return tuple(d for d in self.horizons if d.can_produce_signal)

    @property
    def modelled(self) -> Tuple[HorizonDescriptor, ...]:
        return tuple(d for d in self.horizons if d.has_model)

    def describe(self) -> Dict[str, object]:
        return {
            "built_at": self.built_at.isoformat() if self.built_at else None,
            "source": self.source,
            "horizon_count": len(self.horizons),
            "protected": [d.horizon_id for d in self.protected],
            "with_model": [d.horizon_id for d in self.modelled],
            "signal_capable": [d.horizon_id for d in self.signal_capable],
            "horizons": [d.describe() for d in self.horizons],
        }


def _sort_key(horizon_id: str) -> Tuple[int, int, str]:
    minutes = _MINUTES.get(horizon_id)
    if minutes is not None:
        return (0, minutes, horizon_id)
    days = _TRADING_DAYS.get(horizon_id)
    if days is not None:
        return (1, days, horizon_id)
    return (2, 0, horizon_id)


def build_horizon_inventory(conn: sqlite3.Connection, *,
                            as_of: Optional[datetime] = None,
                            qualified_model_ids: Optional[Sequence[str]] = None
                            ) -> HorizonInventory:
    """
    Read the horizon set from the corpus. Never reads a label VALUE.

    `qualified_model_ids` is supplied by `model_inventory`, which owns
    the qualification question. This module deliberately does not decide
    whether a model is good enough -- it only counts.
    """
    qualified = {str(i) for i in (qualified_model_ids or ())}

    label_counts: Dict[str, Dict[str, int]] = {}
    try:
        # COUNT(*) and names only. The label value column is never
        # named in this query, which is what makes the seal structural.
        rows = conn.execute(
            "SELECT window_name, name, COUNT(*) FROM research_labels "
            "GROUP BY window_name, name").fetchall()
    except sqlite3.DatabaseError:
        rows = []
    for window_name, name, count in rows:
        window = str(window_name or "").strip()
        if not window:
            continue
        label_counts.setdefault(window, {})[str(name)] = int(count)

    models_by_horizon: Dict[str, List[Tuple[str, str]]] = {}
    try:
        model_rows = conn.execute(
            "SELECT label_name, model_qualified_id, status "
            "FROM trained_models").fetchall()
    except sqlite3.DatabaseError:
        model_rows = []
    for label_name, model_qualified_id, status in model_rows:
        window = str(label_name or "").split(".")[0].strip()
        if not window:
            continue
        models_by_horizon.setdefault(window, []).append(
            (str(model_qualified_id or ""), str(status or "")))

    descriptors: List[HorizonDescriptor] = []
    for window in sorted(set(label_counts) | set(models_by_horizon),
                         key=_sort_key):
        names = label_counts.get(window, {})
        abnormal = names.get(f"{window}.abnormal_return", 0)
        raw = names.get(f"{window}.raw_return", 0)
        models = models_by_horizon.get(window, [])
        distinct_models = {model_id for model_id, _ in models if model_id}
        qualified_here = {model_id for model_id in distinct_models
                          if model_id in qualified}

        protected = is_protected(window)
        limitations: List[str] = []
        if abnormal and abnormal < 30:
            limitations.append(
                f"only {abnormal} matured labels; a horizon needs its full "
                "window to elapse before a label exists")
        if not distinct_models:
            limitations.append("no model has ever been trained for this horizon")
        elif not qualified_here:
            limitations.append(
                "a model exists but none passed the qualification gates")
        if protected:
            limitations.append(
                "sealed: no signal, target or preferred horizon may derive "
                "from this horizon")

        descriptors.append(HorizonDescriptor(
            horizon_id=window,
            label_name=f"{window}.abnormal_return" if abnormal else "",
            label_count=abnormal, raw_label_count=raw,
            is_intraday=window.startswith("intraday"),
            is_protected=protected,
            protected_note=PROTECTED_HORIZONS.get(window, ""),
            model_count=len(distinct_models),
            qualified_model_count=len(qualified_here),
            trading_days=_TRADING_DAYS.get(window),
            minutes=_MINUTES.get(window),
            limitations=tuple(limitations)))

    return HorizonInventory(horizons=tuple(descriptors), built_at=as_of)


def label_maturity(conn: sqlite3.Connection, horizon_id: str,
                   observation_total: Optional[int] = None) -> Dict[str, object]:
    """
    How many labels have matured for a horizon. READINESS ONLY.

    Returns counts and a ratio, never a value or a statistic derived
    from one. Safe to call for a protected horizon precisely because
    there is no path from here to an outcome -- but the returned dict
    says so, so a reader cannot mistake maturity for performance.
    """
    window = str(horizon_id).strip()
    try:
        matured = conn.execute(
            "SELECT COUNT(*) FROM research_labels "
            "WHERE window_name = ? AND name = ?",
            (window, f"{window}.abnormal_return")).fetchone()[0]
    except sqlite3.DatabaseError:
        matured = 0
    total = observation_total
    if total is None:
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM research_observations").fetchone()[0]
        except sqlite3.DatabaseError:
            total = 0
    return {
        "horizon_id": window,
        "matured_labels": int(matured),
        "observations": int(total or 0),
        "maturity_ratio": (round(matured / total, 6) if total else None),
        "is_protected": is_protected(window),
        "note": ("readiness metadata only; no outcome statistic is exposed "
                 "for any horizon, protected or not"),
    }
