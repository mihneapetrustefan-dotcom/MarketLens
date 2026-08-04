"""
test_dashboard.py
--------------------
Unit tests for Dashboard v2 (dashboard.py) — the sector-grouped
redesign, with confidence bars, verified-track-record badges, a
collapsible evidence argument (breakdown + representative article),
sticky sector navigation, and a client-side search box.

TESTING STRATEGY:
DashboardGenerator only produces an HTML string — no rendering, no
browser, no JS execution. Tests assert specific substrings/attributes
appear correctly in the generated output. The search box's actual
filtering behavior is client-side JS and is not unit-tested here (it's
verified structurally: the script and data-search attributes exist);
this mirrors how the earlier <details> disclosure widget's open/close
behavior was never Python-tested either — only its HTML structure.
"""

import unittest
import tempfile
import os

from dashboard import DashboardGenerator


def make_recommendation(**overrides):
    base = {
        "entity": "Tesla",
        "recommendation": "BUY",
        "explanation": "BUY recommendation for Tesla, based on 3 articles.",
        "confidence_score": 0.75,
        "sufficient_data": True,
        "article_count": 3,
        "distinct_source_count": 3,
        "dominant_sentiment": "positive",
        "average_impact": 0.6,
        "volume_score": 0.6,
        "source_diversity_score": 0.75,
        "sentiment_consistency": 0.9,
    }
    base.update(overrides)
    return base


class TestReportStructure(unittest.TestCase):
    """Tests confirming the output is a well-formed, complete HTML document."""

    def setUp(self):
        self.generator = DashboardGenerator()

    def test_report_is_valid_html_document(self):
        html = self.generator.generate_report([make_recommendation()])
        self.assertTrue(html.strip().startswith("<!DOCTYPE html>"))
        self.assertIn("</html>", html)

    def test_report_includes_generation_timestamp(self):
        html = self.generator.generate_report([])
        self.assertIn("MarketLens ·", html)


class TestSummaryCounts(unittest.TestCase):
    """Tests for the BUY/SELL/HOLD KPI counts."""

    def setUp(self):
        self.generator = DashboardGenerator()

    def test_counts_reflect_recommendation_mix(self):
        recommendations = [
            make_recommendation(entity="Tesla", recommendation="BUY"),
            make_recommendation(entity="Bitcoin", recommendation="SELL"),
            make_recommendation(entity="Apple", recommendation="HOLD"),
            make_recommendation(entity="Nvidia", recommendation="HOLD"),
        ]
        html = self.generator.generate_report(recommendations)
        self.assertIn('style="color:#3ecf7e">1</div><div class="l">Buy', html)
        self.assertIn('style="color:#f0645f">1</div><div class="l">Sell', html)
        self.assertIn('>2</div><div class="l">Hold', html)

    def test_db_stats_appear_in_summary(self):
        html = self.generator.generate_report([], db_stats={"total_articles": 210, "distinct_sources": 9})
        self.assertIn("210", html)
        self.assertIn("9", html)


class TestSectorGrouping(unittest.TestCase):
    """Tests for the sector-grouped layout — the core of the v2 redesign."""

    def setUp(self):
        self.generator = DashboardGenerator()

    def test_entities_grouped_under_their_sector(self):
        recs = [
            make_recommendation(entity="Tesla"),
            make_recommendation(entity="Ford Motor Company", recommendation="HOLD"),
        ]
        html = self.generator.generate_report(recs, entity_sector_map={
            "Tesla": "Automotive", "Ford Motor Company": "Automotive",
        })
        self.assertIn("AUTOMOTIVE", html.upper())
        # Both entities' names must appear within the generated document.
        self.assertIn("Tesla", html)
        self.assertIn("Ford Motor Company", html)

    def test_unknown_entity_falls_back_to_other_group(self):
        recs = [make_recommendation(entity="Some New Company")]
        html = self.generator.generate_report(recs, entity_sector_map={})
        self.assertIn("Altele", html)

    def test_sector_header_shows_aggregate_stats(self):
        recs = [make_recommendation(entity="Tesla")]
        sector_scores = [{
            "sector": "Automotive", "article_count": 42, "distinct_source_count": 5,
            "dominant_sentiment": "positive", "sentiment_consistency": 0.8, "average_impact": 0.4,
        }]
        html = self.generator.generate_report(
            recs, entity_sector_map={"Tesla": "Automotive"}, sector_scores=sector_scores,
        )
        self.assertIn("42", html)
        self.assertIn("positive", html)

    def test_jump_nav_links_to_each_sector(self):
        recs = [
            make_recommendation(entity="Tesla"),
            make_recommendation(entity="Bitcoin"),
        ]
        html = self.generator.generate_report(
            recs, entity_sector_map={"Tesla": "Automotive", "Bitcoin": "Cryptocurrency"},
        )
        self.assertIn('href="#sector-automotive"', html)
        self.assertIn('href="#sector-cryptocurrency"', html)

    def test_sector_sections_are_collapsible_details(self):
        html = self.generator.generate_report([make_recommendation()], entity_sector_map={"Tesla": "Automotive"})
        self.assertIn('<details class="sector-block"', html)
        self.assertIn("open>", html)  # starts expanded by default


class TestConfidenceBar(unittest.TestCase):
    """Tests for the visual confidence bar."""

    def setUp(self):
        self.generator = DashboardGenerator()

    def test_confidence_bar_reflects_score(self):
        rec = make_recommendation(confidence_score=0.87)
        html = self.generator.generate_report([rec])
        self.assertIn("width:87%", html)
        self.assertIn("0.87", html)

    def test_low_confidence_uses_warning_color(self):
        rec = make_recommendation(confidence_score=0.3)
        html = self.generator.generate_report([rec])
        self.assertIn("#f0645f", html)  # low-confidence red


class TestVerifiedBadge(unittest.TestCase):
    """Tests for the verified track-record badge."""

    def setUp(self):
        self.generator = DashboardGenerator()

    def test_correct_prior_outcome_shows_positive_badge(self):
        rec = make_recommendation(entity="Tesla")
        html = self.generator.generate_report([rec], verified_track_record={"Tesla": True})
        self.assertIn("verificare: corectă", html)

    def test_incorrect_prior_outcome_shows_negative_badge(self):
        rec = make_recommendation(entity="Tesla")
        html = self.generator.generate_report([rec], verified_track_record={"Tesla": False})
        self.assertIn("verificare: greșită", html)

    def test_no_track_record_shows_no_badge(self):
        rec = make_recommendation(entity="Tesla")
        html = self.generator.generate_report([rec], verified_track_record=None)
        self.assertNotIn("verificare:", html)

    def test_none_value_for_entity_shows_no_badge(self):
        rec = make_recommendation(entity="Tesla")
        html = self.generator.generate_report([rec], verified_track_record={"Tesla": None})
        self.assertNotIn("verificare:", html)


class TestArgumentBreakdown(unittest.TestCase):
    """Tests for the hidden, expandable argument (breakdown + representative article)."""

    def setUp(self):
        self.generator = DashboardGenerator()

    def test_breakdown_shows_all_four_components(self):
        rec = make_recommendation(volume_score=0.6, source_diversity_score=0.75,
                                   sentiment_consistency=0.9, average_impact=0.5)
        html = self.generator.generate_report([rec])
        self.assertIn("Volum", html)
        self.assertIn("Diversitate surse", html)
        self.assertIn("Consistență sentiment", html)
        self.assertIn("Impact mediu", html)

    def test_representative_article_picks_highest_impact_matching_sentiment(self):
        rec = make_recommendation(entity="Tesla", dominant_sentiment="positive")
        articles = [
            {"title": "Tesla faces minor recall", "source": "A",
             "sentiment": {"label": "negative"}, "impact": {"score": 0.9}},
            {"title": "Tesla beats earnings estimates", "source": "Reuters",
             "sentiment": {"label": "positive"}, "impact": {"score": 0.6}},
            {"title": "Tesla stock climbs on strong deliveries", "source": "CNBC",
             "sentiment": {"label": "positive"}, "impact": {"score": 0.8}},
        ]
        html = self.generator.generate_report(
            [rec], entity_articles_map={"Tesla": articles},
        )
        # Must pick the highest-impact POSITIVE article (0.8, CNBC),
        # not the highest-impact article overall (0.9, negative).
        self.assertIn("Tesla stock climbs on strong deliveries", html)
        self.assertNotIn("Tesla faces minor recall", html)

    def test_no_articles_map_omits_representative_article_gracefully(self):
        rec = make_recommendation()
        html = self.generator.generate_report([rec], entity_articles_map=None)
        self.assertIn("Tesla", html)  # still renders fine


class TestUpgradeDowngradeBadge(unittest.TestCase):
    def setUp(self):
        self.generator = DashboardGenerator()

    def test_upgrade_badge_appears(self):
        rec = make_recommendation(entity="Tesla")
        udg_map = {"Tesla": {"entity": "Tesla", "change": "upgrade"}}
        html = self.generator.generate_report([rec], upgrade_downgrade_map=udg_map)
        self.assertIn("upgrade", html)

    def test_no_map_does_not_break_rendering(self):
        rec = make_recommendation(entity="Tesla")
        html = self.generator.generate_report([rec], upgrade_downgrade_map=None)
        self.assertIn("Tesla", html)


class TestSearchBox(unittest.TestCase):
    """Tests for the search box's HTML structure (JS filtering itself is not Python-testable)."""

    def setUp(self):
        self.generator = DashboardGenerator()

    def test_search_input_present(self):
        html = self.generator.generate_report([make_recommendation()])
        self.assertIn('id="marketlens-search"', html)

    def test_cards_have_data_search_attribute(self):
        rec = make_recommendation(entity="Tesla")
        html = self.generator.generate_report([rec])
        self.assertIn('data-search="tesla"', html)

    def test_filter_script_present(self):
        html = self.generator.generate_report([make_recommendation()])
        self.assertIn("function marketlensFilter", html)


class TestHtmlEscaping(unittest.TestCase):
    """Tests confirming real-world content is safely escaped."""

    def setUp(self):
        self.generator = DashboardGenerator()

    def test_special_characters_in_entity_name_are_escaped(self):
        rec = make_recommendation(entity="AT&T", explanation="Explanation with <b>tags</b> & symbols")
        html = self.generator.generate_report([rec])
        self.assertIn("AT&amp;T", html)
        self.assertIn("&lt;b&gt;tags&lt;/b&gt;", html)
        self.assertNotIn("<b>tags</b>", html)


class TestMarketDataSection(unittest.TestCase):
    def setUp(self):
        self.generator = DashboardGenerator()

    def test_renders_ticker_row_with_figures(self):
        market_data = {"TSLA": {"current_price": 220.0, "daily_change_pct": 2.5,
                                 "pct_from_52w_high": -12.0, "pct_from_52w_low": 46.67, "trailing_pe": 45.5}}
        html = self.generator.generate_report([], market_data=market_data)
        self.assertIn("TSLA", html)
        self.assertIn("220.0", html)

    def test_never_renders_undervalued_or_overvalued_labels(self):
        market_data = {"TSLA": {"current_price": 220.0, "daily_change_pct": 2.5,
                                 "pct_from_52w_high": -12.0, "pct_from_52w_low": 46.67, "trailing_pe": 45.5}}
        html = self.generator.generate_report([], market_data=market_data)
        self.assertNotIn("undervalued", html.lower())
        self.assertNotIn("overvalued", html.lower())
        self.assertNotIn("subevaluat", html.lower())
        self.assertNotIn("supraevaluat", html.lower())

    def test_ticker_with_error_shows_error_message(self):
        market_data = {"BAD": {"error": "Market data unavailable: timeout"}}
        html = self.generator.generate_report([], market_data=market_data)
        self.assertIn("Market data unavailable", html)

    def test_risk_data_appears_in_market_table(self):
        market_data = {"TSLA": {"current_price": 220.0, "daily_change_pct": 1.0,
                                 "pct_from_52w_high": -5.0, "pct_from_52w_low": 30.0, "trailing_pe": 40.0}}
        risk_data = {"TSLA": {"ticker": "TSLA", "annualized_volatility_pct": 55.3, "risk_level": "High"}}
        html = self.generator.generate_report([], market_data=market_data, risk_data=risk_data)
        self.assertIn("High", html)
        self.assertIn("55.3", html)

    def test_no_market_data_shows_empty_state(self):
        html = self.generator.generate_report([], market_data=None)
        self.assertIn("Nicio dată de piață disponibilă", html)


class TestPortfolioSection(unittest.TestCase):
    def setUp(self):
        self.generator = DashboardGenerator()

    def test_portfolio_result_appears_in_summary(self):
        portfolio_result = {
            "total_invested": 2000.0, "total_final_value": 2200.0,
            "total_return_pct": 10.0, "trades_simulated": 2, "trades": [],
        }
        html = self.generator.generate_report([], portfolio_result=portfolio_result)
        self.assertIn("2200.0", html)
        self.assertIn("10.0", html)

    def test_no_portfolio_result_shows_empty_state(self):
        html = self.generator.generate_report([], portfolio_result=None)
        self.assertIn("Nicio simulare de portofoliu disponibilă", html)


class TestDailySummary(unittest.TestCase):
    def setUp(self):
        self.generator = DashboardGenerator()

    def test_daily_summary_text_appears_when_provided(self):
        html = self.generator.generate_report([], daily_summary_text="Today: 3 BUY and 1 SELL.")
        self.assertIn("Today: 3 BUY and 1 SELL.", html)

    def test_no_daily_summary_shows_default_text(self):
        html = self.generator.generate_report([], daily_summary_text=None)
        self.assertIn("Raport de investiții generat automat.", html)


class TestBackwardCompatibleStandaloneSections(unittest.TestCase):
    """Tests for the retained standalone sector-distribution/table methods."""

    def setUp(self):
        self.generator = DashboardGenerator()

    def test_sector_distribution_renders_bars(self):
        articles = [{"sectors": [{"sector": "Technology", "source": "company", "via": ["Apple"]}]}]
        result = self.generator._render_sector_distribution(articles)
        self.assertIn("Technology", result)

    def test_sector_distribution_empty_state(self):
        result = self.generator._render_sector_distribution([])
        self.assertIn("Nicio clasificare pe sector disponibilă", result)

    def test_sector_scores_table_renders_rows(self):
        sector_scores = [{
            "sector": "Technology", "article_count": 42, "distinct_source_count": 5,
            "dominant_sentiment": "positive", "sentiment_consistency": 0.8, "average_impact": 0.4,
        }]
        result = self.generator._render_sector_scores_table(sector_scores)
        self.assertIn("Technology", result)
        self.assertIn("42", result)

    def test_sector_scores_table_empty_state(self):
        result = self.generator._render_sector_scores_table(None)
        self.assertIn("Nicio perspectivă pe sector disponibilă", result)


class TestSaveReport(unittest.TestCase):
    def setUp(self):
        self.generator = DashboardGenerator()

    def test_saves_html_content_to_file(self):
        html = self.generator.generate_report([make_recommendation()])
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = os.path.join(tmp_dir, "report.html")
            self.generator.save_report(html, path)
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertEqual(content, html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
