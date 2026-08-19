"""
test_dashboard.py
---------------------
Unit tests for Dashboard v2 (dashboard.py) — sidebar navigation,
pinned watchlist section, and source-linked arguments.
"""

import unittest

from dashboard import DashboardGenerator


def make_recommendation(entity="Tesla", recommendation="BUY", confidence_score=0.75,
                         dominant_sentiment="positive", article_count=10, distinct_source_count=5,
                         average_impact=0.5, explanation="Test explanation", **extra):
    rec = {
        "entity": entity, "recommendation": recommendation, "confidence_score": confidence_score,
        "dominant_sentiment": dominant_sentiment, "article_count": article_count,
        "distinct_source_count": distinct_source_count, "average_impact": average_impact,
        "explanation": explanation, "sufficient_data": True,
    }
    rec.update(extra)
    return rec


class TestReportStructure(unittest.TestCase):
    def setUp(self):
        self.generator = DashboardGenerator()

    def test_generates_valid_html_document(self):
        html = self.generator.generate_report([make_recommendation()])
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("</html>", html)

    def test_sidebar_present_with_anchor_links(self):
        html = self.generator.generate_report([make_recommendation()])
        self.assertIn('href="#rezumat"', html)
        self.assertIn('href="#sectoare"', html)
        self.assertIn('href="#portofoliu"', html)
        self.assertIn('href="#piata"', html)
        self.assertIn('href="#schimbari"', html)

    def test_chartjs_cdn_included(self):
        html = self.generator.generate_report([make_recommendation()])
        self.assertIn("chart.js", html.lower())

    def test_entity_name_appears_in_report(self):
        html = self.generator.generate_report([make_recommendation(entity="Tesla")])
        self.assertIn("Tesla", html)

    def test_empty_recommendations_does_not_crash(self):
        html = self.generator.generate_report([])
        self.assertIn("<!DOCTYPE html>", html)


class TestHoldGapDisplay(unittest.TestCase):
    """Tests for the v1.5 hold-gap proximity indicator on HOLD cards."""

    def setUp(self):
        self.generator = DashboardGenerator()

    def test_hold_gap_confidence_shown_on_card(self):
        rec = make_recommendation(entity="Tesla", recommendation="HOLD")
        rec["hold_gap"] = {"blocked_by": "confidence", "gap": 0.08, "threshold": 0.5}
        html = self.generator.generate_report([rec])
        self.assertIn("0.08", html)
        self.assertIn("încredere", html)

    def test_hold_gap_impact_shown_on_card(self):
        rec = make_recommendation(entity="Tesla", recommendation="HOLD")
        rec["hold_gap"] = {"blocked_by": "impact", "gap": 0.12, "threshold": 0.3}
        html = self.generator.generate_report([rec])
        self.assertIn("0.12", html)
        self.assertIn("impact", html)

    def test_no_hold_gap_renders_nothing_extra(self):
        rec = make_recommendation(entity="Tesla", recommendation="HOLD")
        rec["hold_gap"] = None
        html = self.generator.generate_report([rec])
        self.assertNotIn('<div class="hold-gap"', html)

    def test_missing_hold_gap_key_does_not_crash(self):
        rec = make_recommendation(entity="Tesla", recommendation="HOLD")
        rec.pop("hold_gap", None)
        html = self.generator.generate_report([rec])
        self.assertIn("Tesla", html)



    def setUp(self):
        self.generator = DashboardGenerator()

    def test_counts_reflect_recommendation_mix(self):
        recs = [
            make_recommendation(entity="A", recommendation="BUY"),
            make_recommendation(entity="B", recommendation="SELL"),
            make_recommendation(entity="C", recommendation="HOLD"),
            make_recommendation(entity="D", recommendation="HOLD"),
        ]
        html = self.generator.generate_report(recs)
        self.assertIn("1 BUY", html)
        self.assertIn("1 SELL", html)
        self.assertIn("2 HOLD", html)

    def test_strong_buy_counted_separately_and_rolled_into_hero_total(self):
        recs = [
            make_recommendation(entity="A", recommendation="STRONG_BUY"),
            make_recommendation(entity="B", recommendation="BUY"),
        ]
        html = self.generator.generate_report(recs)
        self.assertIn("2 BUY", html)  # hero line: STRONG_BUY + BUY combined
        self.assertIn("Strong Buy", html)  # dedicated KPI cell label present

    def test_strong_sell_counted_separately_and_rolled_into_hero_total(self):
        recs = [
            make_recommendation(entity="A", recommendation="STRONG_SELL"),
            make_recommendation(entity="B", recommendation="SELL"),
        ]
        html = self.generator.generate_report(recs)
        self.assertIn("2 SELL", html)
        self.assertIn("Strong Sell", html)

    def test_strong_buy_card_shows_star_prefixed_label(self):
        rec = make_recommendation(entity="Nvidia", recommendation="STRONG_BUY")
        html = self.generator.generate_report([rec])
        self.assertIn("STRONG BUY", html)
        self.assertIn("★", html)
        self.assertNotIn("STRONG_BUY<", html)  # underscore never leaks into the visible label


class TestSectorGrouping(unittest.TestCase):
    def setUp(self):
        self.generator = DashboardGenerator()

    def test_entities_grouped_by_sector(self):
        recs = [make_recommendation(entity="Tesla"), make_recommendation(entity="Bitcoin")]
        entity_sector_map = {"Tesla": "Automotive", "Bitcoin": "Cryptocurrency"}
        html = self.generator.generate_report(recs, entity_sector_map=entity_sector_map)
        self.assertIn("AUTOMOTIVE", html.upper())
        self.assertIn("CRYPTOCURRENCY", html.upper())

    def test_unmapped_entity_falls_back_to_altele(self):
        recs = [make_recommendation(entity="Unknown Co")]
        html = self.generator.generate_report(recs, entity_sector_map={})
        self.assertIn("ALTELE", html.upper())

    def test_sector_header_shows_stats(self):
        recs = [make_recommendation(entity="Tesla")]
        entity_sector_map = {"Tesla": "Automotive"}
        sector_scores = [{"sector": "Automotive", "article_count": 42, "distinct_source_count": 5,
                           "dominant_sentiment": "positive", "sentiment_consistency": 0.8, "average_impact": 0.4}]
        html = self.generator.generate_report(recs, entity_sector_map=entity_sector_map, sector_scores=sector_scores)
        self.assertIn("42", html)


class TestConfidenceBar(unittest.TestCase):
    def setUp(self):
        self.generator = DashboardGenerator()

    def test_high_confidence_renders_green(self):
        rec = make_recommendation(confidence_score=0.8)
        html = self.generator.generate_report([rec])
        self.assertIn("#3ecf7e", html)

    def test_none_confidence_does_not_crash(self):
        rec = make_recommendation(confidence_score=None)
        html = self.generator.generate_report([rec])
        self.assertIn(rec["entity"], html)


class TestVerifiedBadge(unittest.TestCase):
    def setUp(self):
        self.generator = DashboardGenerator()

    def test_correct_prior_outcome_shows_positive_badge(self):
        rec = make_recommendation(entity="Tesla")
        html = self.generator.generate_report([rec], verified_track_record={"Tesla": True})
        self.assertIn("verified-ok", html)

    def test_incorrect_prior_outcome_shows_negative_badge(self):
        rec = make_recommendation(entity="Tesla")
        html = self.generator.generate_report([rec], verified_track_record={"Tesla": False})
        self.assertIn("verified-bad", html)

    def test_no_track_record_shows_no_badge(self):
        rec = make_recommendation(entity="Tesla")
        html = self.generator.generate_report([rec], verified_track_record=None)
        # The CSS rule ".verified-ok { ... }" is always present in the
        # <style> block regardless of usage — check for the actual
        # rendered badge <span>, not the bare class-name substring.
        self.assertNotIn('class="badge-pill verified-ok"', html)
        self.assertNotIn('class="badge-pill verified-bad"', html)

    def test_none_value_for_entity_shows_no_badge(self):
        rec = make_recommendation(entity="Tesla")
        html = self.generator.generate_report([rec], verified_track_record={"Tesla": None})
        self.assertNotIn('class="badge-pill verified-ok"', html)
        self.assertNotIn('class="badge-pill verified-bad"', html)


class TestArgumentBreakdown(unittest.TestCase):
    def setUp(self):
        self.generator = DashboardGenerator()

    def test_breakdown_chips_appear_when_scores_present(self):
        rec = make_recommendation(volume_score=0.8, source_diversity_score=0.9,
                                   sentiment_consistency=0.94, average_impact=0.6)
        html = self.generator.generate_report([rec])
        self.assertIn("Volum 0.8/1", html)
        self.assertIn("Diversitate surse 0.9/1", html)
        self.assertIn("Consistență 0.94/1", html)
        self.assertIn("Impact 0.6/1", html)

    def test_missing_breakdown_scores_renders_nothing_extra(self):
        rec = make_recommendation()
        rec.pop("average_impact", None)
        html = self.generator.generate_report([rec])
        self.assertIn(rec["entity"], html)


class TestRepresentativeArticleSourceLink(unittest.TestCase):
    def setUp(self):
        self.generator = DashboardGenerator()

    def test_article_with_url_renders_clickable_link(self):
        rec = make_recommendation(entity="Tesla")
        articles_map = {"Tesla": [{"title": "Tesla beats estimates", "source": "Reuters",
                                    "url": "https://example.com/article1", "impact": {"score": 0.6}}]}
        html = self.generator.generate_report([rec], entity_articles_map=articles_map)
        self.assertIn('href="https://example.com/article1"', html)
        self.assertIn("Tesla beats estimates", html)
        self.assertIn("Reuters", html)

    def test_article_without_url_renders_as_inactive_text_not_link(self):
        rec = make_recommendation(entity="Tesla")
        articles_map = {"Tesla": [{"title": "Tesla news", "source": "Reuters", "url": None, "impact": {"score": 0.5}}]}
        html = self.generator.generate_report([rec], entity_articles_map=articles_map)
        self.assertIn("source-link-inactive", html)
        self.assertNotIn('href="None"', html)

    def test_picks_highest_impact_article_among_several(self):
        rec = make_recommendation(entity="Tesla")
        articles_map = {"Tesla": [
            {"title": "Low impact story", "source": "A", "url": "http://a", "impact": {"score": 0.1}},
            {"title": "High impact story", "source": "B", "url": "http://b", "impact": {"score": 0.9}},
        ]}
        html = self.generator.generate_report([rec], entity_articles_map=articles_map)
        self.assertIn("High impact story", html)
        self.assertNotIn("Low impact story", html)

    def test_no_articles_for_entity_does_not_crash(self):
        rec = make_recommendation(entity="Tesla")
        html = self.generator.generate_report([rec], entity_articles_map={})
        self.assertIn(rec["entity"], html)

    def test_recent_lower_impact_article_beats_stale_high_impact_one(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        rec = make_recommendation(entity="Tesla")
        articles_map = {"Tesla": [
            {"title": "Old blowout earnings report", "source": "A", "url": "http://a",
             "impact": {"score": 0.9}, "published_at": (now - timedelta(days=30)).isoformat()},
            {"title": "Fresh routine update", "source": "B", "url": "http://b",
             "impact": {"score": 0.5}, "published_at": now.isoformat()},
        ]}
        html = self.generator.generate_report([rec], entity_articles_map=articles_map)
        self.assertIn("Fresh routine update", html)
        self.assertNotIn("Old blowout earnings report", html)

    def test_much_more_impactful_old_article_can_still_win(self):
        # Recency down-weights, but doesn't wipe out a genuinely huge
        # impact gap versus a near-irrelevant recent article.
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        rec = make_recommendation(entity="Tesla")
        articles_map = {"Tesla": [
            {"title": "Massive historic event", "source": "A", "url": "http://a",
             "impact": {"score": 1.0}, "published_at": (now - timedelta(days=10)).isoformat()},
            {"title": "Trivial mention", "source": "B", "url": "http://b",
             "impact": {"score": 0.02}, "published_at": now.isoformat()},
        ]}
        html = self.generator.generate_report([rec], entity_articles_map=articles_map)
        self.assertIn("Massive historic event", html)

    def test_missing_timestamp_falls_back_to_pure_impact_comparison(self):
        rec = make_recommendation(entity="Tesla")
        articles_map = {"Tesla": [
            {"title": "No date low impact", "source": "A", "url": "http://a", "impact": {"score": 0.2}},
            {"title": "No date high impact", "source": "B", "url": "http://b", "impact": {"score": 0.8}},
        ]}
        html = self.generator.generate_report([rec], entity_articles_map=articles_map)
        self.assertIn("No date high impact", html)
        self.assertNotIn("No date low impact", html)

    def test_url_is_html_escaped(self):
        rec = make_recommendation(entity="Tesla")
        articles_map = {"Tesla": [{"title": "Title", "source": "S",
                                    "url": 'http://example.com/"><script>alert(1)</script>',
                                    "impact": {"score": 0.5}}]}
        html = self.generator.generate_report([rec], entity_articles_map=articles_map)
        self.assertNotIn("<script>alert(1)</script>", html)


class TestUpgradeDowngradeBadge(unittest.TestCase):
    def setUp(self):
        self.generator = DashboardGenerator()

    def test_upgrade_badge_appears(self):
        rec = make_recommendation(entity="Tesla")
        udg_map = {"Tesla": {"entity": "Tesla", "change": "upgrade"}}
        html = self.generator.generate_report([rec], upgrade_downgrade_map=udg_map)
        self.assertIn("change-up", html)

    def test_downgrade_badge_appears(self):
        rec = make_recommendation(entity="Tesla")
        udg_map = {"Tesla": {"entity": "Tesla", "change": "downgrade"}}
        html = self.generator.generate_report([rec], upgrade_downgrade_map=udg_map)
        self.assertIn("change-down", html)

    def test_unchanged_shows_no_badge(self):
        rec = make_recommendation(entity="Tesla")
        udg_map = {"Tesla": {"entity": "Tesla", "change": "unchanged"}}
        html = self.generator.generate_report([rec], upgrade_downgrade_map=udg_map)
        # ".change-up { ... }" is always present as a CSS rule in the
        # <style> block regardless of usage — check for the actual
        # rendered badge <span>, not the bare class-name substring.
        self.assertNotIn('class="badge-pill change-up"', html)
        self.assertNotIn('class="badge-pill change-down"', html)


class TestWatchlistSection(unittest.TestCase):
    """Tests for the v2 PINNED watchlist section (not a filter)."""

    def setUp(self):
        self.generator = DashboardGenerator()

    def test_watchlist_entity_appears_in_pinned_section(self):
        recs = [make_recommendation(entity="Tesla"), make_recommendation(entity="Apple")]
        html = self.generator.generate_report(recs, watchlist=["Tesla"])
        self.assertIn('id="watchlist"', html)
        self.assertIn("pinned", html)

    def test_non_watchlist_entities_still_shown_in_sectors(self):
        recs = [make_recommendation(entity="Tesla"), make_recommendation(entity="Apple")]
        entity_sector_map = {"Tesla": "Automotive", "Apple": "Technology"}
        html = self.generator.generate_report(recs, watchlist=["Tesla"], entity_sector_map=entity_sector_map)
        self.assertIn("Apple", html)
        self.assertIn("TECHNOLOGY", html.upper())

    def test_watchlist_entity_not_duplicated_in_sector_section(self):
        recs = [make_recommendation(entity="Tesla")]
        entity_sector_map = {"Tesla": "Automotive"}
        html = self.generator.generate_report(recs, watchlist=["Tesla"], entity_sector_map=entity_sector_map)
        self.assertNotIn("AUTOMOTIVE", html.upper())

    def test_no_watchlist_shows_all_entities_normally_no_pinned_section(self):
        recs = [make_recommendation(entity="Tesla"), make_recommendation(entity="Apple")]
        html = self.generator.generate_report(recs, watchlist=None)
        self.assertNotIn('id="watchlist"', html)
        self.assertIn("Tesla", html)
        self.assertIn("Apple", html)

    def test_empty_watchlist_list_behaves_like_none(self):
        recs = [make_recommendation(entity="Tesla")]
        html = self.generator.generate_report(recs, watchlist=[])
        self.assertNotIn('id="watchlist"', html)

    def test_watchlist_matching_is_case_insensitive(self):
        recs = [make_recommendation(entity="Tesla")]
        html = self.generator.generate_report(recs, watchlist=["tesla"])
        self.assertIn('id="watchlist"', html)

    def test_sidebar_shows_watchlist_link_when_present(self):
        recs = [make_recommendation(entity="Tesla"), make_recommendation(entity="Apple")]
        html = self.generator.generate_report(recs, watchlist=["Tesla", "Apple"])
        self.assertIn('href="#watchlist"', html)

    def test_sidebar_omits_watchlist_link_when_absent(self):
        recs = [make_recommendation(entity="Tesla")]
        html = self.generator.generate_report(recs, watchlist=None)
        self.assertNotIn('href="#watchlist"', html)


class TestMarketDataSection(unittest.TestCase):
    def setUp(self):
        self.generator = DashboardGenerator()

    def test_renders_ticker_row_with_figures(self):
        market_data = {"TSLA": {"current_price": 220.0, "daily_change_pct": 2.5,
                                 "pct_from_52w_high": -12.0, "pct_from_52w_low": 46.67, "trailing_pe": 45.5}}
        html = self.generator.generate_report([], market_data=market_data)
        self.assertIn("TSLA", html)
        self.assertIn("220.0", html)

    def test_never_renders_valuation_verdict_language(self):
        market_data = {"TSLA": {"current_price": 220.0, "daily_change_pct": 2.5,
                                 "pct_from_52w_high": -12.0, "pct_from_52w_low": 46.67, "trailing_pe": 45.5}}
        html = self.generator.generate_report([], market_data=market_data)
        self.assertNotIn("undervalued", html.lower())
        self.assertNotIn("overvalued", html.lower())
        self.assertNotIn("subevaluat", html.lower())
        self.assertNotIn("supraevaluat", html.lower())

    def test_ticker_with_error_shows_error_not_figures(self):
        market_data = {"BAD": {"error": "Market data unavailable: timeout"}}
        html = self.generator.generate_report([], market_data=market_data)
        self.assertIn("Market data unavailable", html)

    def test_no_market_data_shows_empty_state(self):
        html = self.generator.generate_report([], market_data=None)
        self.assertIn("Nicio dată de piață disponibilă", html)

    def test_risk_data_appears_alongside_market_data(self):
        market_data = {"TSLA": {"current_price": 220.0, "daily_change_pct": 1.0,
                                 "pct_from_52w_high": -5.0, "pct_from_52w_low": 30.0, "trailing_pe": 40.0}}
        risk_data = {"TSLA": {"risk_level": "High", "annualized_volatility_pct": 55.3}}
        html = self.generator.generate_report([], market_data=market_data, risk_data=risk_data)
        self.assertIn("High", html)
        self.assertIn("55.3", html)


class TestPortfolioSection(unittest.TestCase):
    def setUp(self):
        self.generator = DashboardGenerator()

    def test_portfolio_result_appears_in_summary(self):
        portfolio_result = {"total_invested": 2000.0, "total_final_value": 2200.0,
                             "total_return_pct": 10.0, "trades_simulated": 2, "trades": []}
        html = self.generator.generate_report([], portfolio_result=portfolio_result)
        self.assertIn("2200.0", html)
        self.assertIn("10.0", html)

    def test_no_portfolio_result_shows_empty_state(self):
        html = self.generator.generate_report([], portfolio_result=None)
        self.assertIn("Nicio simulare de portofoliu disponibilă", html)

    def test_portfolio_history_renders_chart(self):
        history = [{"recorded_at": "2026-08-01T09:00:00+00:00", "total_return_pct": 2.0},
                   {"recorded_at": "2026-08-02T09:00:00+00:00", "total_return_pct": 5.5}]
        html = self.generator.generate_report([], portfolio_history=history)
        self.assertIn('id="portfolioChart"', html)
        self.assertIn("2026-08-01", html)

    def test_no_portfolio_history_shows_empty_state(self):
        html = self.generator.generate_report([], portfolio_history=None)
        self.assertIn("Niciun istoric de portofoliu", html)


class TestAccuracyChart(unittest.TestCase):
    """Tests for the v1.6 hit-rate-over-time chart."""

    def setUp(self):
        self.generator = DashboardGenerator()

    def test_accuracy_history_renders_chart(self):
        history = [
            {"checked_at": "2026-08-01T09:00:00+00:00", "cumulative_hit_rate": 1.0, "cumulative_checked": 1},
            {"checked_at": "2026-08-02T09:00:00+00:00", "cumulative_hit_rate": 0.667, "cumulative_checked": 3},
        ]
        html = self.generator.generate_report([], accuracy_history=history)
        self.assertIn('id="accuracyChart"', html)
        self.assertIn("2026-08-01", html)

    def test_hit_rate_converted_to_percentage_in_chart_data(self):
        history = [{"checked_at": "2026-08-01T09:00:00+00:00", "cumulative_hit_rate": 0.75, "cumulative_checked": 4}]
        html = self.generator.generate_report([], accuracy_history=history)
        self.assertIn("75.0", html)

    def test_no_accuracy_history_shows_empty_state(self):
        html = self.generator.generate_report([], accuracy_history=None)
        self.assertIn("Niciun istoric de precizie", html)

    def test_empty_accuracy_history_list_shows_empty_state(self):
        html = self.generator.generate_report([], accuracy_history=[])
        self.assertIn("Niciun istoric de precizie", html)

    def test_missing_accuracy_history_param_does_not_crash(self):
        html = self.generator.generate_report([])
        self.assertIn("Rată de succes în timp", html)


class TestCalibrationChart(unittest.TestCase):
    """Tests for the v1.7 confidence calibration chart."""

    def setUp(self):
        self.generator = DashboardGenerator()

    def test_calibration_report_renders_chart(self):
        report = [
            {"bucket_label": "0.5-0.6", "bucket_min": 0.5, "bucket_max": 0.6, "count": 4, "correct": 1, "hit_rate": 0.25},
            {"bucket_label": "0.9-1.0", "bucket_min": 0.9, "bucket_max": 1.0, "count": 6, "correct": 5, "hit_rate": 0.833},
        ]
        html = self.generator.generate_report([], calibration_report=report)
        self.assertIn('id="calibrationChart"', html)
        self.assertIn("0.5-0.6", html)
        self.assertIn("0.9-1.0", html)

    def test_hit_rate_converted_to_percentage(self):
        report = [{"bucket_label": "0.7-0.8", "bucket_min": 0.7, "bucket_max": 0.8, "count": 4, "correct": 3, "hit_rate": 0.75}]
        html = self.generator.generate_report([], calibration_report=report)
        self.assertIn("75.0", html)

    def test_counts_shown_alongside_chart(self):
        report = [{"bucket_label": "0.5-0.6", "bucket_min": 0.5, "bucket_max": 0.6, "count": 12, "correct": 4, "hit_rate": 0.333}]
        html = self.generator.generate_report([], calibration_report=report)
        self.assertIn("12 verificări", html)

    def test_no_calibration_report_shows_empty_state(self):
        html = self.generator.generate_report([], calibration_report=None)
        self.assertIn("Niciun raport de calibrare", html)

    def test_empty_calibration_report_list_shows_empty_state(self):
        html = self.generator.generate_report([], calibration_report=[])
        self.assertIn("Niciun raport de calibrare", html)

    def test_missing_calibration_report_param_does_not_crash(self):
        html = self.generator.generate_report([])
        self.assertIn("Calibrarea încrederii", html)


class TestPriceSparkline(unittest.TestCase):
    def setUp(self):
        self.generator = DashboardGenerator()

    def test_sparkline_renders_for_entity_with_history(self):
        rec = make_recommendation(entity="Tesla")
        price_history_map = {"Tesla": [{"date": "2026-07-01", "close": 200.0}, {"date": "2026-07-02", "close": 210.0}]}
        html = self.generator.generate_report([rec], price_history_map=price_history_map)
        self.assertIn("spark-tesla", html)

    def test_no_price_history_renders_no_sparkline_canvas(self):
        rec = make_recommendation(entity="Tesla")
        html = self.generator.generate_report([rec], price_history_map={})
        self.assertNotIn("spark-tesla", html)


class TestChangesSection(unittest.TestCase):
    def setUp(self):
        self.generator = DashboardGenerator()

    def test_shows_upgrade_and_downgrade_lines(self):
        results = [
            {"entity": "Tesla", "previous": "HOLD", "current": "BUY", "change": "upgrade"},
            {"entity": "Bitcoin", "previous": "BUY", "current": "SELL", "change": "downgrade"},
        ]
        html = self.generator.generate_report([], upgrade_downgrade_results=results)
        self.assertIn("Tesla", html)
        self.assertIn("Bitcoin", html)

    def test_no_changes_shows_empty_state(self):
        results = [{"entity": "Tesla", "previous": "HOLD", "current": "HOLD", "change": "unchanged"}]
        html = self.generator.generate_report([], upgrade_downgrade_results=results)
        self.assertIn("Nicio schimbare azi", html)

    def test_empty_results_shows_empty_state(self):
        html = self.generator.generate_report([], upgrade_downgrade_results=[])
        self.assertIn("empty-state", html)

    def test_derives_from_upgrade_downgrade_map_when_results_omitted(self):
        udg_map = {"Tesla": {"entity": "Tesla", "previous": "HOLD", "current": "BUY", "change": "upgrade"}}
        html = self.generator.generate_report([], upgrade_downgrade_map=udg_map)
        self.assertIn("change-line", html)


class TestHtmlEscaping(unittest.TestCase):
    def setUp(self):
        self.generator = DashboardGenerator()

    def test_entity_name_with_html_is_escaped(self):
        rec = make_recommendation(entity="<script>alert(1)</script>")
        html = self.generator.generate_report([rec])
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_explanation_with_html_is_escaped(self):
        rec = make_recommendation(explanation="<img src=x onerror=alert(1)>")
        html = self.generator.generate_report([rec])
        self.assertNotIn("<img src=x onerror=alert(1)>", html)


class TestSaveReport(unittest.TestCase):
    def test_writes_html_to_file(self):
        import tempfile
        import os
        generator = DashboardGenerator()
        html = generator.generate_report([make_recommendation()])
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = os.path.join(tmp_dir, "report.html")
            generator.save_report(html, path)
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertEqual(content, html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
