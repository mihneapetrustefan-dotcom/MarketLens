#!/usr/bin/env python3
"""
run_daily.py
---------------
MarketLens daily automation entry point.

RESPONSIBILITY:
Run the CORE pipeline — RSS collection, processing, persistence,
scoring, recommendations, and the Dashboard — completely unattended,
via GitHub Actions, once a day, with NO user interaction of any kind.
This replaces the Colab notebook's manual "Run all" + Google Drive
authorization click with a script that just runs.

KEY DIFFERENCE FROM THE COLAB VERSION — no Google Drive:
Persistence (the SQLite database + the generated Dashboard) lives
inside THIS repository (`data/marketlens.db`, `docs/index.html`)
instead of Google Drive. The GitHub Actions workflow that calls this
script commits the updated files back to the repo after every run —
that's what makes the accumulated history durable across runs, exactly
like Drive did for the notebook, but requiring zero manual
authorization.

WHAT THIS SCRIPT DOES NOT DO: Google News Historical Backfill (see
run_weekly_backfill.py) — deliberately a separate, less frequent job,
since scanning every tracked company against Google News (with a
polite delay between each request) takes several minutes; running it
daily would slow down this fast, frequent core update for no benefit.

FAILURE DETECTION: this script returns a non-zero exit code if
something goes clearly wrong (e.g. zero articles collected — meaning
every RSS source is unreachable). GitHub Actions marks a run with a
non-zero exit as FAILED, and GitHub emails the repository owner about
failed workflow runs by default — so a silent, unattended failure
still gets noticed, without any extra monitoring setup.
"""

import os
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

# Make src/ importable regardless of the working directory this script
# is invoked from (works identically locally, in CI, anywhere).
REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from sources import RSS_FEEDS
from rss_collector import RSSCollector
from pipeline_core import process_articles
from news_database import NewsDatabase
from confidence_engine import ConfidenceEngine
from recommendation_engine import RecommendationEngine
from time_horizon_classifier import TimeHorizonClassifier
from recommendation_log import RecommendationLog
from upgrade_downgrade_tracker import UpgradeDowngradeTracker
from backtest_engine import BacktestEngine
from market_data import MarketDataFetcher, normalize_ticker_for_yfinance
from risk_score import RiskScoreCalculator
from portfolio_simulator import PortfolioSimulator
from sector_aggregator import SectorAggregator
from daily_summary import DailySummaryGenerator
from dashboard import DashboardGenerator
from ticker_registry import TICKER_REGISTRY
from sector_registry import COMPANY_SECTOR_MAP

DB_PATH = str(REPO_ROOT / "data" / "marketlens.db")
REPORT_PATH = str(REPO_ROOT / "docs" / "index.html")


def main() -> int:
    """
    Run the full daily pipeline once. Returns a process exit code:
    0 on success, non-zero if something is clearly wrong (so CI marks
    the run as failed rather than silently producing a stale/empty
    report).
    """
    print(f"=== MarketLens daily run started at {datetime.now(timezone.utc).isoformat()} ===")

    # --- 1. Collect ---
    collector = RSSCollector(feeds=RSS_FEEDS)
    raw_articles = collector.collect_all()
    print(f"Collected {len(raw_articles)} raw articles from {len(RSS_FEEDS)} RSS sources")

    if not raw_articles:
        print("ERROR: zero articles collected — every configured source may be unreachable")
        return 1

    # --- 2. Process (Cleaner through Impact Engine) ---
    processed = process_articles(raw_articles)
    print(f"{len(processed)} article(s) remain after cleaning/deduplication/scoring")

    # --- 3. Persist, then load the FULL accumulated history ---
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = NewsDatabase(DB_PATH)
    newly_stored = db.save_articles(processed)
    all_articles = db.load_all_articles()
    stats = db.get_stats()
    print(f"Stored {newly_stored} new article(s); {stats['total_articles']} total now in the database")

    # --- 4. Score + recommend, over the FULL history, not just today's batch ---
    confidence_engine = ConfidenceEngine()
    entities = confidence_engine.score_all_entities(all_articles)
    recommendation_engine = RecommendationEngine()
    recommendations = recommendation_engine.recommend_all(entities)

    entity_articles_map = confidence_engine.aggregate_by_entity(all_articles)
    horizon_by_entity = TimeHorizonClassifier().classify_batch(entity_articles_map)
    for r in recommendations:
        h = horizon_by_entity.get(r["entity"])
        if h:
            r["time_horizon"] = h["time_horizon"]
            r["time_horizon_reason"] = h["reason"]

    # --- 5. Upgrade/downgrade vs. prior history, then log today's result ---
    rec_log = RecommendationLog(DB_PATH)
    previously_logged = rec_log.load_all()
    entity_to_ticker = {entry["name"]: entry["ticker"] for entry in TICKER_REGISTRY}
    upgrade_downgrade_results = UpgradeDowngradeTracker().compare_batch(recommendations, previously_logged)
    upgrade_downgrade_map = {r["entity"]: r for r in upgrade_downgrade_results}
    rec_log.log_recommendations(recommendations, ticker_lookup=entity_to_ticker)

    # --- 6. Backtest previously-logged recommendations old enough to check ---
    backtest_engine = BacktestEngine(holding_period_days=5)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=backtest_engine.holding_period_days)).isoformat()
    old_enough = rec_log.load_actionable_before(cutoff)
    if old_enough:
        backtest_result = backtest_engine.run_backtest(old_enough)
    else:
        backtest_result = {
            "results": [],
            "summary": {"total_recommendations": 0, "checked": 0, "skipped": 0,
                        "correct": 0, "hit_rate": None, "average_change_pct": None},
        }
    verified_track_record = {
        r["entity"]: r["was_correct"] for r in backtest_result["results"] if r.get("outcome") == "checked"
    }
    print(f"Backtest: {backtest_result['summary']['checked']} recommendation(s) checked, "
          f"hit rate {backtest_result['summary']['hit_rate']}")

    # --- 7. Market data + risk score, for actionable (BUY/SELL) entities only ---
    entity_to_category = {entry["name"]: entry["category"] for entry in TICKER_REGISTRY}
    yfinance_tickers = {}
    for r in recommendations:
        if r["recommendation"] not in ("BUY", "SELL"):
            continue
        ticker = entity_to_ticker.get(r["entity"])
        category = entity_to_category.get(r["entity"])
        if not ticker or not category:
            continue
        yf_symbol = normalize_ticker_for_yfinance(ticker, category)
        if yf_symbol:
            yfinance_tickers[yf_symbol] = r["entity"]

    market_fetcher = MarketDataFetcher()
    market_snapshots_raw = market_fetcher.get_snapshots_batch(list(yfinance_tickers.keys()))
    market_snapshots = {yf.replace("-USD", ""): snap for yf, snap in market_snapshots_raw.items()}

    risk_calculator = RiskScoreCalculator(lookback_days=30)
    risk_snapshots_raw = risk_calculator.get_risk_scores_batch(list(yfinance_tickers.keys()))
    risk_snapshots = {yf.replace("-USD", ""): snap for yf, snap in risk_snapshots_raw.items()}

    # --- 8. Portfolio simulation, sector macro view, daily summary text ---
    portfolio_result = PortfolioSimulator().simulate(backtest_result["results"])
    sector_scores = SectorAggregator().score_all_sectors(all_articles)
    daily_summary_text = DailySummaryGenerator().generate(recommendations, sector_scores, upgrade_downgrade_results)

    # --- 9. Dashboard ---
    dashboard = DashboardGenerator()
    report_html = dashboard.generate_report(
        recommendations=recommendations,
        articles=all_articles,
        db_stats=stats,
        market_data=market_snapshots,
        risk_data=risk_snapshots,
        sector_scores=sector_scores,
        upgrade_downgrade_map=upgrade_downgrade_map,
        portfolio_result=portfolio_result,
        daily_summary_text=daily_summary_text,
        entity_sector_map=dict(COMPANY_SECTOR_MAP),
        verified_track_record=verified_track_record,
        entity_articles_map=entity_articles_map,
    )
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    dashboard.save_report(report_html, REPORT_PATH)
    print(f"Dashboard saved to {REPORT_PATH} ({len(report_html)} characters)")

    rec_log.close()
    db.close()

    actionable = sum(1 for r in recommendations if r["recommendation"] in ("BUY", "SELL"))
    print(f"=== Run complete: {actionable} actionable recommendation(s) out of {len(recommendations)} entities ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
