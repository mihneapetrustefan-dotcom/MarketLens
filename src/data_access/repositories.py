"""
src/data_access/repositories.py
------------------------------------
Internal Data Access Layer for the Phase 1 canonical tables.

RESPONSIBILITY:
The ONLY code allowed to write raw SQL against the canonical tables
(exchanges, sectors, companies, securities, instruments, news_sources)
introduced by schema.py. Business logic (existing or future) should
depend on these repository interfaces, never on SQLite directly — this
is the "Internal Data Access Layer" from the phase's own target
architecture diagram.

Every repository method is idempotent on primary key (INSERT OR
REPLACE) — re-running the migration script is always safe.
"""

import json
import sqlite3
from typing import List, Optional

from src.domain.models import Company, Security, Instrument, Exchange, Sector, NewsSource
from src.domain.enums import AssetClass, InstrumentType, SourceType


class ExchangeRepository:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def save(self, exchange: Exchange) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO exchanges (exchange_id, name, country, timezone) VALUES (?, ?, ?, ?)",
            (exchange.exchange_id, exchange.name, exchange.country, exchange.timezone),
        )
        self._conn.commit()

    def get(self, exchange_id: str) -> Optional[Exchange]:
        row = self._conn.execute("SELECT * FROM exchanges WHERE exchange_id = ?", (exchange_id,)).fetchone()
        if not row:
            return None
        return Exchange(exchange_id=row[0], name=row[1], country=row[2], timezone=row[3])

    def list_all(self) -> List[Exchange]:
        rows = self._conn.execute("SELECT * FROM exchanges ORDER BY exchange_id").fetchall()
        return [Exchange(exchange_id=r[0], name=r[1], country=r[2], timezone=r[3]) for r in rows]


class SectorRepository:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def save(self, sector: Sector) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO sectors (sector_id, name) VALUES (?, ?)",
            (sector.sector_id, sector.name),
        )
        self._conn.commit()

    def get(self, sector_id: str) -> Optional[Sector]:
        row = self._conn.execute("SELECT * FROM sectors WHERE sector_id = ?", (sector_id,)).fetchone()
        if not row:
            return None
        return Sector(sector_id=row[0], name=row[1])

    def list_all(self) -> List[Sector]:
        rows = self._conn.execute("SELECT * FROM sectors ORDER BY name").fetchall()
        return [Sector(sector_id=r[0], name=r[1]) for r in rows]


class CompanyRepository:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def save(self, company: Company) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO companies (company_id, canonical_name, aliases_json, sector_id) VALUES (?, ?, ?, ?)",
            (company.company_id, company.canonical_name, json.dumps(company.aliases), company.sector_id),
        )
        self._conn.commit()

    def get(self, company_id: str) -> Optional[Company]:
        row = self._conn.execute("SELECT * FROM companies WHERE company_id = ?", (company_id,)).fetchone()
        if not row:
            return None
        return Company(company_id=row[0], canonical_name=row[1], aliases=json.loads(row[2]), sector_id=row[3])

    def get_by_canonical_name(self, canonical_name: str) -> Optional[Company]:
        row = self._conn.execute("SELECT * FROM companies WHERE canonical_name = ?", (canonical_name,)).fetchone()
        if not row:
            return None
        return Company(company_id=row[0], canonical_name=row[1], aliases=json.loads(row[2]), sector_id=row[3])

    def list_all(self) -> List[Company]:
        rows = self._conn.execute("SELECT * FROM companies ORDER BY canonical_name").fetchall()
        return [Company(company_id=r[0], canonical_name=r[1], aliases=json.loads(r[2]), sector_id=r[3]) for r in rows]

    def list_by_sector(self, sector_id: str) -> List[Company]:
        rows = self._conn.execute("SELECT * FROM companies WHERE sector_id = ? ORDER BY canonical_name", (sector_id,)).fetchall()
        return [Company(company_id=r[0], canonical_name=r[1], aliases=json.loads(r[2]), sector_id=r[3]) for r in rows]

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]


class SecurityRepository:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def save(self, security: Security) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO securities (security_id, company_id, instrument_type, currency) VALUES (?, ?, ?, ?)",
            (security.security_id, security.company_id, security.instrument_type.value, security.currency),
        )
        self._conn.commit()

    def get(self, security_id: str) -> Optional[Security]:
        row = self._conn.execute("SELECT * FROM securities WHERE security_id = ?", (security_id,)).fetchone()
        if not row:
            return None
        return Security(security_id=row[0], company_id=row[1], instrument_type=InstrumentType(row[2]), currency=row[3])

    def list_by_company(self, company_id: str) -> List[Security]:
        rows = self._conn.execute("SELECT * FROM securities WHERE company_id = ?", (company_id,)).fetchall()
        return [Security(security_id=r[0], company_id=r[1], instrument_type=InstrumentType(r[2]), currency=r[3]) for r in rows]


class InstrumentRepository:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def save(self, instrument: Instrument) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO instruments (instrument_id, security_id, exchange_id, ticker, asset_class) VALUES (?, ?, ?, ?, ?)",
            (instrument.instrument_id, instrument.security_id, instrument.exchange_id, instrument.ticker, instrument.asset_class.value),
        )
        self._conn.commit()

    def get(self, instrument_id: str) -> Optional[Instrument]:
        row = self._conn.execute("SELECT * FROM instruments WHERE instrument_id = ?", (instrument_id,)).fetchone()
        if not row:
            return None
        return Instrument(instrument_id=row[0], security_id=row[1], exchange_id=row[2], ticker=row[3], asset_class=AssetClass(row[4]))

    def get_by_exchange_and_ticker(self, exchange_id: str, ticker: str) -> Optional[Instrument]:
        """
        The CORRECT way to look up an instrument — by (exchange, ticker),
        never by ticker alone. See domain/models.py's Instrument.identity_key().
        """
        row = self._conn.execute(
            "SELECT * FROM instruments WHERE exchange_id = ? AND ticker = ?", (exchange_id, ticker)
        ).fetchone()
        if not row:
            return None
        return Instrument(instrument_id=row[0], security_id=row[1], exchange_id=row[2], ticker=row[3], asset_class=AssetClass(row[4]))

    def list_by_ticker(self, ticker: str) -> List[Instrument]:
        """
        Returns EVERY instrument sharing this bare ticker string,
        possibly across multiple exchanges (e.g. "EL" -> both
        Electrica/BVB and Estee Lauder/NYSE) — deliberately named
        differently from get_by_exchange_and_ticker() so a caller can
        never mistake "the instruments sharing this ticker" for "the
        one true instrument for this ticker", since no such single
        thing exists.
        """
        rows = self._conn.execute("SELECT * FROM instruments WHERE ticker = ?", (ticker,)).fetchall()
        return [Instrument(instrument_id=r[0], security_id=r[1], exchange_id=r[2], ticker=r[3], asset_class=AssetClass(r[4])) for r in rows]

    def list_by_security(self, security_id: str) -> List[Instrument]:
        rows = self._conn.execute("SELECT * FROM instruments WHERE security_id = ?", (security_id,)).fetchall()
        return [Instrument(instrument_id=r[0], security_id=r[1], exchange_id=r[2], ticker=r[3], asset_class=AssetClass(r[4])) for r in rows]

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]


class NewsSourceRepository:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def save(self, source: NewsSource) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO news_sources (source_id, name, source_type, url, active) VALUES (?, ?, ?, ?, ?)",
            (source.source_id, source.name, source.source_type.value, source.url, int(source.active)),
        )
        self._conn.commit()

    def get(self, source_id: str) -> Optional[NewsSource]:
        row = self._conn.execute("SELECT * FROM news_sources WHERE source_id = ?", (source_id,)).fetchone()
        if not row:
            return None
        return NewsSource(source_id=row[0], name=row[1], source_type=SourceType(row[2]), url=row[3], active=bool(row[4]))

    def get_by_name(self, name: str) -> Optional[NewsSource]:
        row = self._conn.execute("SELECT * FROM news_sources WHERE name = ?", (name,)).fetchone()
        if not row:
            return None
        return NewsSource(source_id=row[0], name=row[1], source_type=SourceType(row[2]), url=row[3], active=bool(row[4]))

    def list_all(self) -> List[NewsSource]:
        rows = self._conn.execute("SELECT * FROM news_sources ORDER BY name").fetchall()
        return [NewsSource(source_id=r[0], name=r[1], source_type=SourceType(r[2]), url=r[3], active=bool(r[4])) for r in rows]
