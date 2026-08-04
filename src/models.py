"""
models.py
---------
Defines the standardized data structure used across the entire MarketLens
platform for representing a single news article.

DESIGN DECISION:
We use a Python `dataclass` instead of a plain dict because:
- It gives us type safety, IDE autocompletion, and a self-documenting schema.
- It still converts cleanly to/from a plain dict via `to_dict()`, which is
  what the project spec requires (`all_news` = list of dictionaries).
  The dataclass is our internal, type-checked representation; the dict
  is the "wire format" every other module in MarketLens will consume.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional
import uuid


@dataclass
class NewsArticle:
    """
    Standardized representation of a single news article, regardless of
    which collector (RSS, API, or Web Scraper) produced it.

    Every collector MUST output articles in this exact shape, so that
    downstream modules (Cleaner, Duplicate Detector, Sentiment Engine,
    Ticker Detector, etc.) never need to know where the article came from.
    """

    # Unique identifier, generated automatically per article.
    # UUID4 is used (instead of an incrementing counter) because collection
    # can run across multiple sources/processes in parallel, and UUID4
    # guarantees uniqueness without any shared, coordinated state.
    article_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    # --- Core content fields ---
    title: str = ""
    summary: str = ""          # short description/snippet from the feed
    url: str = ""               # canonical link to the full article
    source: str = ""            # human-readable source name, e.g. "Reuters"
    category: str = ""          # market category, e.g. "stocks", "crypto", "bvb"

    # --- Temporal fields ---
    published_at: Optional[datetime] = None          # parsed publication datetime (UTC)
    collected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))  # when WE collected it

    # --- Metadata reserved for later modules (kept here now for schema stability,
    #     so we don't have to migrate the schema when those modules are built) ---
    raw_language: Optional[str] = None  # e.g. "ro", "en" — filled in later by language detection

    def to_dict(self) -> dict:
        """
        Convert this article into a plain, JSON-safe dictionary.

        Why this exists:
        - Downstream storage (JSON files, databases) and every other
          MarketLens module expect plain dicts, not dataclass instances.
        - datetime objects are not natively JSON-serializable, so we
          convert them to ISO 8601 strings here, once, at the boundary
          of this class — callers never need to worry about it again.
        """
        data = asdict(self)
        if isinstance(data.get("published_at"), datetime):
            data["published_at"] = data["published_at"].isoformat()
        if isinstance(data.get("collected_at"), datetime):
            data["collected_at"] = data["collected_at"].isoformat()
        return data
