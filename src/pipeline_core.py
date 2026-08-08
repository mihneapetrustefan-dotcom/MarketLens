"""
pipeline_core.py
-------------------
Shared pipeline orchestration helper for MarketLens automation.

RESPONSIBILITY:
Run the standard article-processing chain (News Cleaner through Impact
Engine) over a batch of raw collected articles. Both `run_daily.py`
and `run_weekly_backfill.py` need this exact sequence — defining it
here, once, means it can never silently drift out of sync between the
two entry points.
"""

from typing import List, Dict, Any

from news_cleaner import NewsCleaner
from duplicate_detector import DuplicateDetector
from company_detector import CompanyDetector
from ticker_detector import TickerDetector
from sector_detector import SectorDetector
from event_detector import EventDetector
from sentiment_engine import SentimentEngine
from impact_engine import ImpactEngine


def process_articles(raw_articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Run raw, freshly-collected articles through the full processing
    chain: clean -> deduplicate -> detect companies/tickers/sectors ->
    detect event type -> sentiment -> impact. Returns the fully-tagged
    article list, ready to be persisted and scored.
    """
    p = NewsCleaner().clean_batch(raw_articles)
    p = DuplicateDetector().deduplicate(p)
    p = CompanyDetector().detect_batch(p)
    p = TickerDetector().detect_batch(p)
    p = SectorDetector().detect_batch(p)
    p = EventDetector().detect_batch(p)
    p = SentimentEngine().analyze_batch(p)
    p = ImpactEngine().score_batch(p)
    return p
