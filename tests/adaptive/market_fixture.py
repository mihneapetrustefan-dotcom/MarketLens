"""
tests/adaptive/market_fixture.py
--------------------------------------------
A small, deterministic market database for tests that would otherwise
read `data/marketlens.db`.

That corpus is ~196 MB and gitignored, so it exists only on a developer
machine that has built it. A clean checkout -- GitHub Actions included --
does not have it, and tests that reached for it failed with
`FileNotFoundError` or `unable to open database file` rather than with
anything about the behaviour under test.

The tables here are copied from the production schema, so the code under
test runs its real SQL against a real SQLite file. Only the volume of
data differs: a few dozen instruments over a few hundred sessions,
generated from a fixed seed, so every run on every platform builds
byte-identical content.

What this fixture is NOT: evidence about the production corpus. A test
asserting something about the real instrument master cannot be served by
a database this module wrote, and must say so instead of pretending.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone

#: Mirrors the production definitions in src/data_access, reduced to the
#: tables the adaptive read paths actually query.
SCHEMA = (
    """CREATE TABLE companies (
            company_id      TEXT PRIMARY KEY,
            canonical_name  TEXT NOT NULL UNIQUE,
            aliases_json    TEXT NOT NULL DEFAULT '[]',
            sector_id       TEXT)""",
    """CREATE TABLE securities (
            security_id       TEXT PRIMARY KEY,
            company_id        TEXT,
            instrument_type   TEXT NOT NULL,
            currency          TEXT NOT NULL DEFAULT 'USD')""",
    """CREATE TABLE instruments (
            instrument_id   TEXT PRIMARY KEY,
            security_id     TEXT NOT NULL,
            exchange_id     TEXT NOT NULL,
            ticker          TEXT NOT NULL,
            asset_class     TEXT NOT NULL,
            UNIQUE (exchange_id, ticker))""",
    """CREATE TABLE price_candle_cache (
            instrument_id   TEXT NOT NULL,
            interval        TEXT NOT NULL,
            timestamp       TEXT NOT NULL,
            open            REAL,
            high            REAL,
            low             REAL,
            close           REAL,
            adjusted_close  REAL,
            volume          REAL,
            source          TEXT NOT NULL DEFAULT 'fixture',
            fetched_at      TEXT NOT NULL,
            PRIMARY KEY (instrument_id, interval, timestamp))""",
)

#: Enough sectors that a per-sector cap has something to cut, and enough
#: members per sector that the cap is reached at the caps tests use (10).
SECTORS = ("technology", "financials", "healthcare", "energy")
PER_SECTOR = 12

#: build_parent_universe requires 150 daily candles at as_of and volume
#: on at least 90% of them; 220 sessions clears both with room to spare.
SESSIONS = 220


def _business_days(last: datetime, count: int):
    """`count` weekday sessions ending at `last`, oldest first."""
    days, cursor = [], last
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(days))


def _members():
    """The instrument master: deterministic, varied in liquidity."""
    rows = []
    for sector_index, sector in enumerate(SECTORS):
        for rank in range(PER_SECTOR):
            index = sector_index * PER_SECTOR + rank
            rows.append({
                "instrument_id": "fixture-%03d" % index,
                "ticker": "FX%03d" % index,
                "sector_id": sector,
                # Spread liquidity across three orders of magnitude so
                # the per-sector ranking is a real ordering, not a tie.
                "base_volume": 500_000 * (rank + 1) * (sector_index + 1),
                "base_close": 10.0 + index,
            })
    return rows


def build(path: str, *, as_of: datetime = None, sessions: int = SESSIONS) -> str:
    """Write the fixture database at `path` and return that path."""
    as_of = as_of or datetime(2026, 8, 26, tzinfo=timezone.utc)
    members = _members()
    days = _business_days(as_of, sessions)

    conn = sqlite3.connect(path)
    try:
        for statement in SCHEMA:
            conn.execute(statement)
        for member in members:
            index = member["instrument_id"]
            conn.execute(
                "INSERT INTO companies (company_id, canonical_name, sector_id) "
                "VALUES (?, ?, ?)",
                ("co-" + index, "Fixture Company " + index, member["sector_id"]))
            conn.execute(
                "INSERT INTO securities (security_id, company_id, "
                "instrument_type, currency) VALUES (?, ?, 'equity', 'USD')",
                ("se-" + index, "co-" + index))
            conn.execute(
                "INSERT INTO instruments (instrument_id, security_id, "
                "exchange_id, ticker, asset_class) "
                "VALUES (?, ?, 'US_AND_INTL', ?, 'stock')",
                (index, "se-" + index, member["ticker"]))

            for step, day in enumerate(days):
                # A gentle deterministic drift; no randomness, so the
                # same bytes are produced on every platform and run.
                close = member["base_close"] * (1.0 + 0.001 * (step % 50))
                volume = member["base_volume"] * (1.0 + 0.01 * (step % 20))
                stamp = day.isoformat()
                conn.execute(
                    "INSERT INTO price_candle_cache (instrument_id, interval, "
                    "timestamp, open, high, low, close, adjusted_close, volume, "
                    "fetched_at) VALUES (?, '1d', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (index, stamp, close, close * 1.01, close * 0.99,
                     close, close, volume, stamp))
        conn.commit()
    finally:
        conn.close()
    return path


def build_for(instrument_ids, path: str, *, as_of: datetime = None,
              sessions: int = SESSIONS) -> str:
    """
    As `build`, plus candles for `instrument_ids`.

    The adaptive CLIs read their members from the committed parent
    universe artifact, not from the instrument master, so a fixture that
    is to exercise them needs candles under those real identifiers.
    """
    build(path, as_of=as_of, sessions=sessions)
    as_of = as_of or datetime(2026, 8, 26, tzinfo=timezone.utc)
    days = _business_days(as_of, sessions)
    conn = sqlite3.connect(path)
    try:
        for index, instrument_id in enumerate(sorted(instrument_ids)):
            base_close = 20.0 + (index % 180)
            base_volume = 1_000_000 * (1 + index % 40)
            for step, day in enumerate(days):
                close = base_close * (1.0 + 0.001 * (step % 50))
                volume = base_volume * (1.0 + 0.01 * (step % 20))
                stamp = day.isoformat()
                conn.execute(
                    "INSERT OR IGNORE INTO price_candle_cache (instrument_id, "
                    "interval, timestamp, open, high, low, close, "
                    "adjusted_close, volume, fetched_at) "
                    "VALUES (?, '1d', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (instrument_id, stamp, close, close * 1.01, close * 0.99,
                     close, close, volume, stamp))
        conn.commit()
    finally:
        conn.close()
    return path


def real_corpus_path(root: str) -> str:
    """Where the developer corpus lives, present or not."""
    return os.path.join(root, "data", "marketlens.db")


def has_real_corpus(root: str) -> bool:
    """True only on a machine that has built the local corpus."""
    return os.path.exists(real_corpus_path(root))
