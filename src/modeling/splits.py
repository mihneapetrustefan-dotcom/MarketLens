"""
src/modeling/splits.py
---------------------------
Temporal splitting with purging and embargo (Phase 9, spec §13-§16).

THE PROBLEM PURGING SOLVES, CONCRETELY: an observation dated 1 March
with a 20-day forward label is not fully known until 21 March. If the
test period starts 10 March, that training observation's LABEL
overlaps the test period — the model was trained on the answer to a
question it is about to be tested on. The split looks clean by date
and leaks anyway.

PURGING removes training observations whose label window reaches into
the test period. EMBARGO additionally drops observations immediately
before the test period, because serial correlation in financial data
means near-boundary rows carry information about the test period even
when their labels technically close in time.

WHY BOTH, AND WHY ON BY DEFAULT: either alone leaves a real leak.
Purging without embargo still lets a 1-day-before observation encode
the test period's opening conditions; embargo without purging still
lets a long-horizon label bleed across. They are defaults, not
options, and turning them off requires passing explicit zeros.

THERE IS NO SHUFFLE. Random splitting of a time series puts the future
in the training set, and offering the option — even off by default —
is offering the mistake (same reasoning as Phase 7's builder).
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.domain.model_models import TrainingWindow

logger = logging.getLogger("marketlens.modeling.splits")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


def _require_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (got a naive datetime)")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be in UTC (got offset {value.utcoffset()})")
    return value


def add_months(moment: datetime, months: int) -> datetime:
    """Month arithmetic that never overflows a short month (day clamped to 28)."""
    year = moment.year + (moment.month - 1 + months) // 12
    month = (moment.month - 1 + months) % 12 + 1
    return moment.replace(year=year, month=month, day=min(moment.day, 28))


def purge(
    observations: Sequence[Any],
    test_start: datetime,
    timestamp_getter: Callable[[Any], Optional[datetime]],
    label_horizon_days: float,
) -> Tuple[List[Any], List[Any]]:
    """
    Remove training observations whose LABEL WINDOW overlaps the test
    period (spec §15).

    An observation at time T with a horizon of H days is not fully
    resolved until T+H. If T+H > test_start, that observation's label
    was determined partly inside the test period, so training on it
    leaks.

    Returns:
        (kept, purged) — the purged rows are RETURNED, not silently
        dropped, so the count can be reported and audited.
    """
    _require_utc(test_start, "test_start")
    kept, purged = [], []
    for observation in observations:
        moment = timestamp_getter(observation)
        if moment is None:
            purged.append(observation)   # undated: cannot prove it is safe
            continue
        label_resolves_at = moment + timedelta(days=label_horizon_days)
        if label_resolves_at > test_start:
            purged.append(observation)
        else:
            kept.append(observation)
    return kept, purged


def embargo(
    observations: Sequence[Any],
    test_start: datetime,
    timestamp_getter: Callable[[Any], Optional[datetime]],
    embargo_days: float,
) -> Tuple[List[Any], List[Any]]:
    """
    Remove training observations falling within `embargo_days` before
    the test period (spec §16).

    Purging handles label overlap; this handles the subtler problem —
    financial series are serially correlated, so an observation from
    the day before the test period carries information about it even
    when its own label closed cleanly beforehand.

    Returns:
        (kept, embargoed)
    """
    _require_utc(test_start, "test_start")
    if embargo_days <= 0:
        return list(observations), []
    boundary = test_start - timedelta(days=embargo_days)
    kept, embargoed = [], []
    for observation in observations:
        moment = timestamp_getter(observation)
        if moment is None:
            embargoed.append(observation)
            continue
        (embargoed if moment > boundary else kept).append(observation)
    return kept, embargoed


class WalkForwardSplitter:
    """
    Generates expanding or rolling walk-forward splits with purging and
    embargo applied (spec §13, §14).

    EXPANDING keeps all history in each training set; ROLLING keeps a
    fixed-length recent window. Both are offered because they answer
    different questions — expanding assumes the past stays relevant,
    rolling assumes regimes change — and neither is universally right.
    """

    def __init__(
        self,
        label_horizon_days: float,
        embargo_days: float = 1.0,
        train_months: int = 36,
        test_months: int = 6,
        step_months: int = 6,
        expanding: bool = True,
    ):
        """
        Args:
            label_horizon_days: how long after an observation its label
                resolves. Drives purging; MUST match the label actually
                being modelled, or purging silently under-protects.
            embargo_days: gap enforced before each test period.
                Default 1.0 — small, but the difference between "no
                embargo" and "some embargo" matters more than its exact
                size.
        """
        if label_horizon_days < 0:
            raise ValueError("label_horizon_days must not be negative")
        self.label_horizon_days = label_horizon_days
        self.embargo_days = embargo_days
        self.train_months = train_months
        self.test_months = test_months
        self.step_months = step_months
        self.expanding = expanding

    def generate_windows(self, start: datetime, end: datetime) -> List[Dict[str, datetime]]:
        """Produce the raw train/test boundary pairs. Test always strictly follows train."""
        _require_utc(start, "start")
        _require_utc(end, "end")
        windows = []
        train_start = start
        while True:
            train_end = add_months(train_start if not self.expanding else start,
                                    self.train_months + (0 if not self.expanding else
                                                          self._elapsed_months(start, train_start)))
            test_end = add_months(train_end, self.test_months)
            if train_end >= end:
                break
            windows.append({
                "train_start": start if self.expanding else train_start,
                "train_end": train_end,
                "test_start": train_end,
                "test_end": min(test_end, end),
            })
            train_start = add_months(train_start, self.step_months)
        return windows

    @staticmethod
    def _elapsed_months(start: datetime, current: datetime) -> int:
        return (current.year - start.year) * 12 + (current.month - start.month)

    def split(
        self,
        observations: Sequence[Any],
        start: datetime,
        end: datetime,
        timestamp_getter: Callable[[Any], Optional[datetime]],
    ) -> List[Dict[str, Any]]:
        """
        Produce fully-prepared splits.

        Returns:
            A list of {"window": TrainingWindow, "train": [...],
            "test": [...]}. Purge and embargo counts are recorded ON
            the window, so how much data protection removed is always
            visible rather than hidden.
        """
        results = []
        for index, bounds in enumerate(self.generate_windows(start, end)):
            train_pool, test_set = [], []
            for observation in observations:
                moment = timestamp_getter(observation)
                if moment is None:
                    continue   # undated observations enter no split
                if bounds["train_start"] <= moment < bounds["train_end"]:
                    train_pool.append(observation)
                elif bounds["test_start"] <= moment < bounds["test_end"]:
                    test_set.append(observation)

            after_purge, purged = purge(train_pool, bounds["test_start"],
                                         timestamp_getter, self.label_horizon_days)
            after_embargo, embargoed = embargo(after_purge, bounds["test_start"],
                                                timestamp_getter, self.embargo_days)

            window = TrainingWindow(
                label=f"w{index + 1}",
                train_start=bounds["train_start"], train_end=bounds["train_end"],
                test_start=bounds["test_start"], test_end=bounds["test_end"],
                train_size=len(after_embargo), test_size=len(test_set),
                purged_count=len(purged), embargoed_count=len(embargoed),
            )
            results.append({"window": window, "train": after_embargo, "test": test_set})

        logger.info("Walk-forward: %d window(s) generated", len(results))
        return results


def verify_no_temporal_overlap(
    train: Sequence[Any],
    test: Sequence[Any],
    timestamp_getter: Callable[[Any], Optional[datetime]],
    label_horizon_days: float,
) -> List[str]:
    """
    Independent verification that a split is actually clean (spec §21).

    Deliberately re-derives the check rather than trusting the splitter
    that produced the data: a leakage guard that shares code with the
    thing it guards can be fooled by a bug in the shared part. Returns
    a list of violations; empty means clean.
    """
    violations = []
    test_times = [timestamp_getter(o) for o in test if timestamp_getter(o) is not None]
    if not test_times or not train:
        return violations

    earliest_test = min(test_times)
    for observation in train:
        moment = timestamp_getter(observation)
        if moment is None:
            violations.append("training observation has no timestamp")
            continue
        if moment >= earliest_test:
            violations.append(
                f"training observation at {moment.isoformat()} is at or after the earliest test "
                f"observation at {earliest_test.isoformat()}")
        elif moment + timedelta(days=label_horizon_days) > earliest_test:
            violations.append(
                f"training observation at {moment.isoformat()} has a label resolving at "
                f"{(moment + timedelta(days=label_horizon_days)).isoformat()}, inside the test period")
    return violations
