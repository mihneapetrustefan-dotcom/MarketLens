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
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone

# Make src/ importable regardless of the working directory this script
# is invoked from (works identically locally, in CI, anywhere).
REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from sources import RSS_FEEDS
from rss_collector import RSSCollector
from finnhub_news_collector import FinnhubNewsCollector
from alpha_vantage_news_collector import AlphaVantageNewsCollector
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
from portfolio_history import PortfolioHistory
from sector_aggregator import SectorAggregator
from daily_summary import DailySummaryGenerator
from dashboard import DashboardGenerator
from email_notifier import EmailNotifier, build_alert_email
from ticker_registry import TICKER_REGISTRY
from sector_registry import COMPANY_SECTOR_MAP

DB_PATH = str(REPO_ROOT / "data" / "marketlens.db")
REPORT_PATH = str(REPO_ROOT / "docs" / "index.html")
WATCHLIST_PATH = str(REPO_ROOT / "watchlist.txt")


def load_watchlist():
    """
    Load the optional user-maintained watchlist file (one company name
    per line, '#' comments and blank lines ignored).

    Returns:
        A list of company names, or None if the file doesn't exist or
        is empty — meaning "show everything", the exact same behavior
        as before this feature existed. The watchlist ONLY affects
        which entities appear on the Dashboard; data collection and
        scoring still cover every tracked company regardless, so
        broader corroboration isn't lost by narrowing the display.
    """
    if not os.path.exists(WATCHLIST_PATH):
        return None
    with open(WATCHLIST_PATH, encoding="utf-8") as f:
        names = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
    return names or None


_logger = logging.getLogger("marketlens.run_daily")
if not _logger.handlers:
    logging.basicConfig(level=logging.INFO)


def _safe_stage(step_name, fallback, func, *args, **kwargs):
    """
    Run a SECONDARY pipeline stage (one whose failure shouldn't stop
    the whole run from producing a report) with a safe fallback value.

    WHY THIS EXISTS: some stages are essential — if RSS collection or
    Dashboard generation fails, there's genuinely nothing useful to
    report, and the run SHOULD fail loudly (see the explicit checks
    elsewhere in main()). But other stages are enrichment — Time
    Horizon, Backtest, Market Data, Portfolio Simulation, Sector
    Aggregator, Daily Summary — and a bug or a transient failure in
    ANY ONE of them (e.g. a malformed article breaking Sector
    Aggregator) previously meant the ENTIRE run failed and produced NO
    report at all, even though every other stage had already succeeded.
    This wrapper means a secondary stage failing degrades gracefully
    to a safe default instead, and the run still finishes with
    whatever it was able to compute.

    Args:
        step_name: human-readable name, used in the warning message.
        fallback: value returned if `func` raises.
        func, *args, **kwargs: the stage to run.

    Returns:
        func(*args, **kwargs)'s result, or `fallback` if it raised.
    """
    try:
        return func(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — a secondary stage must never take down the whole run
        print(f"WARNING: '{step_name}' failed ({exc}) — continuing with a fallback so the report still generates")
        _logger.error("Secondary stage '%s' failed", step_name, exc_info=True)
        return fallback


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

    # --- 1b. Optional real API sources (Finnhub, Alpha Vantage) —
    # both no-op cleanly if their API key secret isn't configured, so
    # this is always safe to call regardless of setup.
    stock_tickers = [entry["ticker"] for entry in TICKER_REGISTRY if entry["category"] == "stocks"]

    finnhub = FinnhubNewsCollector()
    if finnhub.is_configured():
        finnhub_articles = finnhub.collect_batch(stock_tickers)
        raw_articles.extend(finnhub_articles)
        print(f"Collected {len(finnhub_articles)} additional article(s) from Finnhub")
    else:
        print("Finnhub not configured (FINNHUB_API_KEY secret not set) — skipping")

    alpha_vantage = AlphaVantageNewsCollector()
    if alpha_vantage.is_configured():
        av_articles = alpha_vantage.collect_batch(stock_tickers)
        raw_articles.extend(av_articles)
        print(f"Collected {len(av_articles)} additional article(s) from Alpha Vantage")
    else:
        print("Alpha Vantage not configured (ALPHA_VANTAGE_API_KEY secret not set) — skipping")

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
    horizon_by_entity = _safe_stage(
        "Time Horizon Classifier", {}, TimeHorizonClassifier().classify_batch, entity_articles_map
    )
    for r in recommendations:
        h = horizon_by_entity.get(r["entity"])
        if h:
            r["time_horizon"] = h["time_horizon"]
            r["time_horizon_reason"] = h["reason"]

    # --- 5. Upgrade/downgrade vs. prior history, then log today's result ---
    rec_log = RecommendationLog(DB_PATH)
    previously_logged = _safe_stage("RecommendationLog.load_all", [], rec_log.load_all)
    entity_to_ticker = {entry["name"]: entry["ticker"] for entry in TICKER_REGISTRY}
    upgrade_downgrade_results = _safe_stage(
        "Upgrade/Downgrade Tracker", [], UpgradeDowngradeTracker().compare_batch, recommendations, previously_logged
    )
    upgrade_downgrade_map = {r["entity"]: r for r in upgrade_downgrade_results}
    _safe_stage(
        "RecommendationLog.log_recommendations", None,
        rec_log.log_recommendations, recommendations, ticker_lookup=entity_to_ticker,
    )

    # --- 5b. Real-time alert via Email (only if there's something worth alerting about) ---
    email_alert = _safe_stage("build_alert_email", None, build_alert_email, upgrade_downgrade_results)
    if email_alert:
        subject, body = email_alert
        email_notifier = EmailNotifier()
        if email_notifier.is_configured():
            sent = _safe_stage("EmailNotifier.send_message", False, email_notifier.send_message, subject, body)
            print(f"Email alert {'sent' if sent else 'FAILED to send'}")
        else:
            print("Email not configured (SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD/ALERT_EMAIL_TO secrets not set) — skipping")
    else:
        print("No upgrade/downgrade changes today — no alert needed")

    # --- 6. Backtest previously-logged recommendations old enough to check ---
    backtest_engine = BacktestEngine()
    old_enough = _safe_stage(
        "RecommendationLog.load_actionable_due_for_check", [],
        rec_log.load_actionable_due_for_check,
        backtest_engine.holding_period_days_by_horizon, backtest_engine.holding_period_days,
    )
    empty_backtest = {
        "results": [],
        "summary": {"total_recommendations": 0, "checked": 0, "skipped": 0,
                    "correct": 0, "hit_rate": None, "average_change_pct": None},
    }
    if old_enough:
        backtest_result = _safe_stage("Backtest Engine", empty_backtest, backtest_engine.run_backtest, old_enough)
    else:
        backtest_result = empty_backtest
         def _persist_backtest_results():
        for r in backtest_result["results"]:
            rec_log.mark_checked(r["id"], r.get("was_correct"))
    _safe_stage("RecommendationLog.mark_checked (batch)", None, _persist_backtest_results)

    verified_track_record = _safe_stage(
        "RecommendationLog.load_latest_verified_outcome_per_entity", {},
        rec_log.load_latest_verified_outcome_per_entity,
    )
    }
    print(f"Backtest: {backtest_result['summary']['checked']} recommendation(s) checked, "
          f"hit rate {backtest_result['summary']['hit_rate']}")

    # --- 7. Market data + risk score, for actionable (BUY/SELL) entities only ---
    def _build_market_data():
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

        # Price history for the sparkline chart on each card — keyed by
        # ENTITY name (not ticker), matching the convention Dashboard
        # already uses for upgrade_downgrade_map/verified_track_record.
        price_history_raw = market_fetcher.get_price_history_batch(list(yfinance_tickers.keys()), days=30)
        price_history_map = {
            yfinance_tickers[yf_symbol]: series
            for yf_symbol, series in price_history_raw.items()
            if series
        }

        risk_calculator = RiskScoreCalculator(lookback_days=30)
        risk_snapshots_raw = risk_calculator.get_risk_scores_batch(list(yfinance_tickers.keys()))
        risk_snapshots = {yf.replace("-USD", ""): snap for yf, snap in risk_snapshots_raw.items()}

        return yfinance_tickers, market_snapshots, price_history_map, risk_snapshots

    yfinance_tickers, market_snapshots, price_history_map, risk_snapshots = _safe_stage(
        "Market Data / Risk Score", ({}, {}, {}, {}), _build_market_data,
    )

    # --- 8. Portfolio simulation (+ persisted history), sector macro view, daily summary text ---
    portfolio_result = _safe_stage(
        "Portfolio Simulator",
        {"total_invested": 0.0, "total_final_value": 0.0, "total_return_pct": None, "trades_simulated": 0, "trades": []},
        PortfolioSimulator().simulate, backtest_result["results"],
    )

    def _log_and_load_portfolio_history():
        portfolio_history_log = PortfolioHistory(DB_PATH)
        portfolio_history_log.log_snapshot(portfolio_result)
        history = portfolio_history_log.load_all()
        portfolio_history_log.close()
        return history

    portfolio_history = _safe_stage("Portfolio History", [], _log_and_load_portfolio_history)

    sector_scores = _safe_stage("Sector Aggregator", [], SectorAggregator().score_all_sectors, all_articles)
    daily_summary_text = _safe_stage(
        "Daily Summary Generator", None,
        DailySummaryGenerator().generate, recommendations, sector_scores, upgrade_downgrade_results,
    )

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
        price_history_map=price_history_map,
        portfolio_history=portfolio_history,
        watchlist=load_watchlist(),
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
