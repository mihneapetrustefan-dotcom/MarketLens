"""
fred_connector.py
---------------------
FRED (Federal Reserve Economic Data) connector for MarketLens.

RESPONSIBILITY:
Fetch the LATEST value of a small, fixed set of key US macroeconomic
indicators (GDP, unemployment rate, inflation/CPI, federal funds rate,
10-year Treasury yield) from the Federal Reserve Bank of St. Louis'
free FRED API — real, government-published data, not news-derived
sentiment. Used purely as FACTUAL macro context, following the exact
same "facts, no verdict" philosophy already established for the Date
de piață market table.

WHY A SMALL, FIXED SERIES LIST (not an open-ended search): FRED hosts
800,000+ series; an open-ended integration would need its own curation
discipline. A short, well-known, curated list — same philosophy as
company_registry.py's curated companies — is far easier to reason
about and verify than an unbounded one.

REQUIRES AN API KEY (free, from fred.stlouisfed.org) — read from the
FRED_API_KEY environment variable, never hardcoded. Cleanly returns no
data if not configured, the same resilience pattern already used by
every other optional integration in this project (Finnhub, Alpha
Vantage, Email Notifier).
"""

import os
import json
import logging
from urllib.request import urlopen
from urllib.parse import urlencode
from typing import Dict, Any, Optional, List

logger = logging.getLogger("marketlens.fred_connector")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class FredConnector:
    """Fetches the latest published value of key macro indicators from FRED."""

    # series_id -> human-readable Romanian label. Verified against
    # FRED's own documentation (fred.stlouisfed.org) — these are among
    # the platform's most widely used, long-standing series.
    DEFAULT_SERIES: Dict[str, str] = {
        "GDP": "PIB (SUA)",
        "UNRATE": "Rata șomajului (SUA)",
        "CPIAUCSL": "Indice prețuri de consum — inflație (SUA)",
        "FEDFUNDS": "Rata dobânzii de referință Fed",
        "DGS10": "Randament trezorerie SUA, 10 ani",
    }

    def __init__(self, api_key: Optional[str] = None, series: Optional[Dict[str, str]] = None):
        """
        Args:
            api_key: FRED API key. Defaults to the FRED_API_KEY
                environment variable — never hardcode a real key.
            series: series_id -> label mapping to fetch. Defaults to
                DEFAULT_SERIES.
        """
        self.api_key = api_key if api_key is not None else os.environ.get("FRED_API_KEY")
        self.series = series if series is not None else dict(self.DEFAULT_SERIES)

    def is_configured(self) -> bool:
        """Whether an API key is available."""
        return bool(self.api_key)

    def fetch_latest_observation_raw(self, series_id: str) -> Any:
        """
        Perform the actual HTTP GET for one series' most recent
        observation. Isolated as its own method — same pattern as
        every other network call in this project — so unit tests can
        mock it with no real request.
        """
        params = urlencode({
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 1,
        })
        url = f"https://api.stlouisfed.org/fred/series/observations?{params}"
        with urlopen(url, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    def get_latest_value(self, series_id: str, label: str) -> Optional[Dict[str, Any]]:
        """
        Fetch and standardize the latest observation for one series.

        Returns:
            {"series_id", "label", "value", "date"}, or None on any
            failure — missing configuration, network error, unexpected
            response shape, or a "." missing-value placeholder (FRED's
            own convention for a not-yet-published data point) — never
            raises.
        """
        if not self.is_configured():
            return None

        try:
            data = self.fetch_latest_observation_raw(series_id)
        except Exception as exc:  # noqa: BLE001 — one bad series must never break the batch
            logger.error("FRED fetch failed for '%s': %s", series_id, exc)
            return None

        observations = data.get("observations") if isinstance(data, dict) else None
        if not observations:
            return None

        latest = observations[0]
        raw_value = latest.get("value")
        if raw_value in (None, "."):
            return None
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            return None

        return {"series_id": series_id, "label": label, "value": value, "date": latest.get("date")}

    def get_all_latest(self) -> List[Dict[str, Any]]:
        """
        Fetch the latest value for every configured series.

        Returns:
            A list of standardized observations (see get_latest_value).
            A series that fails or isn't yet published is simply
            omitted — never included as a broken/placeholder entry.
        """
        results = []
        for series_id, label in self.series.items():
            result = self.get_latest_value(series_id, label)
            if result:
                results.append(result)

        logger.info("FRED: %d/%d macro indicator(s) fetched successfully", len(results), len(self.series))
        return results
