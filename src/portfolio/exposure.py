"""
src/portfolio/exposure.py
------------------------------
Where the portfolio's risk actually sits (Phase 11, spec §7, §8).

GROSS SUMS ABSOLUTE VALUE; NET SUMS SIGNED VALUE
----------------------------------------------------
A book that is long $100k of NVDA and short $100k of AMD has net
exposure near zero and gross exposure of $200k. Reporting only the net
figure would describe that book as carrying no risk, which is wrong in
the way that matters: both legs are semiconductor bets that can move
against each other, and both can lose at once.

Every weight in this module is therefore computed on ABSOLUTE
exposure, and net is reported alongside rather than instead.

CLASSIFICATION COMES FROM THE CANONICAL TABLES
--------------------------------------------------
Sector is read through instruments -> securities -> companies ->
sectors, which is populated for every priced instrument in this
database. It is deliberately NOT read from sector_registry's
COMPANY_SECTOR_MAP: that map is keyed by display name, is missing
entries for a documented set of companies, and reaches a sector for
unmapped names only through a keyword fallback. Risk limits must not
depend on a keyword guess.

WHAT CANNOT BE CLASSIFIED IS COUNTED, NOT DROPPED
-----------------------------------------------------
Roughly 150 of this project's 389 instruments have no cached price,
and an instrument can be absent from the canonical tables entirely
(`benchmark-spy` is). Exposure that cannot be attributed to a bucket
lands in `unclassified_exposure`, so a sector breakdown covering 60%
of the book is visibly partial instead of looking complete. A sector
cap evaluated against a partial breakdown is not a real cap, and the
risk engine checks `is_complete` before trusting one.

CURRENCY IS GROUPED, NEVER CONVERTED
----------------------------------------
Positions are grouped by their stated currency and left in it. No FX
rate is applied anywhere (spec §32), because this database has no FX
data — and its `securities.currency` column currently says USD even
for BVB instruments that trade in RON. Inventing a rate to paper over
that would turn a visible data defect into an invisible one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence

from src.domain.portfolio_models import (
    ExposureBreakdown, ExposureBucket, ExposureDimension, PortfolioSnapshot,
    PositionValuation, safe_ratio,
)

#: Bucket key used when a dimension is known to be absent for an
#: instrument, as opposed to the instrument being unknown entirely.
UNKNOWN_KEY = "unknown"


@dataclass(frozen=True)
class InstrumentClassification:
    """How one instrument maps onto each exposure dimension."""
    instrument_id: str
    asset_class: Optional[str] = None
    sector_id: Optional[str] = None
    sector_name: Optional[str] = None
    currency: Optional[str] = None
    ticker: Optional[str] = None

    @property
    def is_known(self) -> bool:
        """False when the instrument is absent from the canonical tables altogether."""
        return any((self.asset_class, self.sector_id, self.currency, self.ticker))


class InstrumentClassifier:
    """
    Batch lookup of instrument attributes from the canonical tables.

    One query for the whole book (spec §53). Instruments missing from
    the tables come back as an unknown classification rather than
    raising — a portfolio holding something the registry has not caught
    up with must still be measurable, with the gap visible.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._cache: Dict[str, InstrumentClassification] = {}

    def _tables_available(self) -> bool:
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('instruments','securities','companies','sectors')"
        ).fetchall()
        return {r[0] for r in rows} >= {"instruments", "securities"}

    def classify(self, instrument_ids: Sequence[str]) -> Dict[str, InstrumentClassification]:
        unique = sorted({i for i in instrument_ids if i})
        if not unique:
            return {}

        missing = [i for i in unique if i not in self._cache]
        if missing and self._tables_available():
            placeholders = ",".join("?" for _ in missing)
            sql = f"""
                SELECT i.instrument_id, i.asset_class, i.ticker,
                       se.currency, c.sector_id, s.name
                FROM instruments i
                LEFT JOIN securities se ON se.security_id = i.security_id
                LEFT JOIN companies  c  ON c.company_id   = se.company_id
                LEFT JOIN sectors    s  ON s.sector_id    = c.sector_id
                WHERE i.instrument_id IN ({placeholders})
            """
            for instrument_id, asset_class, ticker, currency, sector_id, sector_name in \
                    self.conn.execute(sql, missing):
                self._cache[instrument_id] = InstrumentClassification(
                    instrument_id=instrument_id, asset_class=asset_class,
                    sector_id=sector_id, sector_name=sector_name,
                    currency=currency, ticker=ticker)

        for instrument_id in unique:
            self._cache.setdefault(
                instrument_id, InstrumentClassification(instrument_id=instrument_id))

        return {i: self._cache[i] for i in unique}


class ExposureEngine:
    """Aggregates priced positions into exposure along each available dimension."""

    def __init__(self, classifier: InstrumentClassifier):
        self.classifier = classifier

    # ---------------- dimension keys ----------------

    def _key_for(self, dimension: ExposureDimension,
                 classification: InstrumentClassification,
                 valuation: PositionValuation) -> Optional[tuple]:
        """
        (key, label) for a valuation on one dimension, or None when the
        instrument cannot be attributed at all.

        Returning None is what routes exposure into
        `unclassified_exposure`; it must never fall back to a made-up
        bucket, which would make an unattributable position look
        attributed.
        """
        if dimension == ExposureDimension.INSTRUMENT:
            label = classification.ticker or valuation.position.instrument_id
            return (valuation.position.instrument_id, label)

        if dimension == ExposureDimension.SECTOR:
            if not classification.sector_id:
                return None
            return (classification.sector_id,
                    classification.sector_name or classification.sector_id)

        if dimension == ExposureDimension.ASSET_CLASS:
            if not classification.asset_class:
                return None
            return (classification.asset_class, classification.asset_class)

        if dimension == ExposureDimension.CURRENCY:
            # The position's own stated currency wins over the
            # registry's: it is what the holding was actually recorded
            # in, and the registry is known to be wrong for BVB names.
            currency = valuation.position.currency or classification.currency
            if not currency:
                return None
            return (currency, currency)

        return None

    # ---------------- breakdown ----------------

    def breakdown(self, snapshot: PortfolioSnapshot,
                  dimension: ExposureDimension) -> ExposureBreakdown:
        """
        Exposure along one dimension.

        Only priced valuations contribute. Unpriced positions are
        already recorded on the snapshot as `unvalued_positions`; adding
        them here at zero would understate every bucket while making the
        totals look whole.
        """
        result = ExposureBreakdown(dimension=dimension)
        if not snapshot.valuations:
            return result

        classifications = self.classifier.classify(
            [v.position.instrument_id for v in snapshot.valuations])
        equity = snapshot.equity
        buckets: Dict[str, ExposureBucket] = {}

        for valuation in snapshot.valuations:
            exposure = valuation.exposure
            if exposure is None:
                continue
            classification = classifications.get(
                valuation.position.instrument_id,
                InstrumentClassification(valuation.position.instrument_id))

            resolved = self._key_for(dimension, classification, valuation)
            if resolved is None:
                result.unclassified_exposure += exposure
                result.unclassified_count += 1
                continue

            key, label = resolved
            bucket = buckets.get(key)
            if bucket is None:
                bucket = ExposureBucket(key=key, label=label)
                buckets[key] = bucket

            bucket.exposure += exposure
            bucket.position_count += 1
            if valuation.position.is_short:
                bucket.short_exposure += exposure
            else:
                bucket.long_exposure += exposure

        for bucket in buckets.values():
            # None rather than 0.0 at non-positive equity: a weight
            # against zero equity is undefined, not zero (spec §8).
            bucket.weight = (safe_ratio(bucket.exposure, equity)
                             if equity > 0 else None)

        result.buckets = sorted(
            buckets.values(), key=lambda b: b.exposure, reverse=True)
        return result

    def all_breakdowns(self, snapshot: PortfolioSnapshot
                       ) -> Dict[ExposureDimension, ExposureBreakdown]:
        """Every dimension at once; the classifier's cache makes this one query total."""
        return {dimension: self.breakdown(snapshot, dimension)
                for dimension in ExposureDimension}
