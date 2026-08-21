"""
dashboard.py
---------------
Dashboard v2 module for MarketLens.

RESPONSIBILITY:
Build a complete, self-contained HTML investment report from the
pipeline's final outputs — recommendations, sector stats, market
data, risk, portfolio simulation, upgrade/downgrade history, price
charts — with NOTHING removed or trimmed versus earlier versions.

STRUCTURE (v2 — restructured from a single long scrolling page):
A sticky left sidebar with anchor links to each section — Rezumat,
Watchlist (if any), Sectoare, Portofoliu, Date de piață, Schimbări —
so the whole report is still ONE continuous page (nothing hidden,
nothing lost), but with constant orientation instead of blind
scrolling. Uses plain HTML anchor links (`<a href="#id">`), so jumping
between sections works even with JavaScript disabled.

WHAT'S NEW IN v2 vs v1:
1. Watchlist is now a DEDICATED, PINNED SECTION at the top — not a
   filter that hides everything else. Watchlist entities are pulled
   out of the sector-grouped body (so they're never shown twice) and
   shown first, in full card detail, regardless of which sector
   they're in.
2. Each card's hidden "argument" now includes a REAL, CLICKABLE LINK
   to the representative source article — not just its title as
   plain text — so the person can go verify the actual news behind a
   recommendation in one click.

SAFETY: every piece of user-facing text is HTML-escaped before
insertion, and every value embedded inside an inline <script> block
(for Chart.js) is JSON-serialized with "</" escaped, so a stray
article title can never break the page structure or inject a script.
"""

import html as html_lib
import json
from collections import Counter
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional


class DashboardGenerator:
    """
    Builds a self-contained HTML investment report: sidebar navigation,
    a pinned watchlist section, sector-grouped recommendation cards
    (each with a confidence bar, badges, a price sparkline, and a
    hidden, source-linked argument), a portfolio chart, and a market
    data table.
    """

    _RECOMMENDATION_COLORS = {
        "STRONG_BUY": "#2ecc71", "BUY": "#3ecf7e", "HOLD": "#8a8f98",
        "SELL": "#f0645f", "STRONG_SELL": "#e63946",
    }

    _SECTOR_TINTS = {
        "positive": "#0f3d24",
        "negative": "#3d1518",
        "mixed": "#3d3312",
        "neutral": "#1a1f2e",
    }

    _UNCATEGORIZED_SECTOR = "Altele"

    def _escape(self, value: Any) -> str:
        """HTML-escape any value before inserting it into the page — the single point every piece of text passes through."""
        if value is None:
            return ""
        return html_lib.escape(str(value))

    def _json_for_script(self, data: Any) -> str:
        """Serialize data for safe embedding inside an inline <script> block, escaping "</" as defense in depth."""
        return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

    def _anchor_id(self, text: str) -> str:
        """Build a safe HTML id/anchor slug from arbitrary text (sector names, entity names, etc.)."""
        return "".join(ch if ch.isalnum() else "-" for ch in text.lower()).strip("-")

    def _render_confidence_bar(self, confidence_score: Optional[float]) -> str:
        """Render a small horizontal confidence bar, color-coded green/amber/red by score."""
        if confidence_score is None:
            return ""
        pct = max(0, min(100, round(confidence_score * 100)))
        if confidence_score >= 0.7:
            color = "#3ecf7e"
        elif confidence_score >= 0.5:
            color = "#e8c547"
        else:
            color = "#8a8f98"
        return f"""
        <div class="conf-track"><div class="conf-fill" style="width:{pct}%; background:{color};"></div></div>
        <div class="conf-label">{self._escape(confidence_score)}</div>
        """

    def _render_verified_badge(
        self, entity: str, verified_track_record: Optional[Dict[str, Optional[bool]]]
    ) -> str:
        """Render a checkmark/cross badge if this entity's most recent recommendation has already been checked by Backtest Engine."""
        if not verified_track_record or entity not in verified_track_record:
            return ""
        was_correct = verified_track_record[entity]
        if was_correct is None:
            return ""
        if was_correct:
            return '<span class="badge-pill verified-ok">correct verificat</span>'
        return '<span class="badge-pill verified-bad">gresit verificat</span>'

    def _change_badge(self, entity: str, upgrade_downgrade_map: Optional[Dict[str, Dict[str, Any]]]) -> str:
        """Render an upgrade/downgrade badge if this entity's recommendation changed since it was last logged."""
        if not upgrade_downgrade_map or entity not in upgrade_downgrade_map:
            return ""
        change = upgrade_downgrade_map[entity].get("change")
        if change == "upgrade":
            return '<span class="badge-pill change-up">upgrade</span>'
        if change == "downgrade":
            return '<span class="badge-pill change-down">downgrade</span>'
        return ""

    def _render_breakdown(self, rec: Dict[str, Any]) -> str:
        """Render the Volume/Diversity/Consistency/Impact chips backing a recommendation's confidence score."""
        chips = []
        if rec.get("volume_score") is not None:
            chips.append(f'<span class="chip">Volum {self._escape(rec["volume_score"])}/1</span>')
        if rec.get("source_diversity_score") is not None:
            chips.append(f'<span class="chip">Diversitate surse {self._escape(rec["source_diversity_score"])}/1</span>')
        if rec.get("sentiment_consistency") is not None:
            chips.append(f'<span class="chip">Consistență {self._escape(rec["sentiment_consistency"])}/1</span>')
        if rec.get("average_impact") is not None:
            chips.append(f'<span class="chip">Impact {self._escape(rec["average_impact"])}/1</span>')
        if not chips:
            return ""
        return f'<div class="breakdown">{"".join(chips)}</div>'

    def _recency_weight_for_display(self, timestamp_str: Optional[str], half_life_days: float = 7.0) -> float:
        """
        Compute an exponential recency weight in (0, 1] for a
        timestamp, used ONLY to help pick which single article best
        represents an entity's card right now — a separate, much
        shorter-lived concern than Confidence Score's own Time Decay
        (which uses a 480h/20-day half-life to keep historical
        coverage meaningfully weighted in the SCORE). Here, the
        question is "what's the best article to SHOW someone today",
        where a 3-week-old article should rarely keep outranking a
        good recent one just because it once scored a slightly higher
        raw impact — a 7-day half-life reflects that.

        Returns 1.0 for a missing/unparseable timestamp (treated as
        "unknown age", not penalized) — the same resilience pattern
        used everywhere else timestamps are parsed in this project.
        """
        if not timestamp_str:
            return 1.0
        try:
            published = datetime.fromisoformat(str(timestamp_str).replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return 1.0

        age_days = (datetime.now(timezone.utc) - published).total_seconds() / 86400.0
        if age_days < 0:
            age_days = 0.0
        return 0.5 ** (age_days / half_life_days)

    def _representative_article(
        self, rec: Dict[str, Any], entity_articles: Optional[List[Dict[str, Any]]]
    ) -> str:
        """
        Pick the single article that best represents this entity's
        recommendation RIGHT NOW, and render it as a clickable link to
        the real source — so the person can verify the actual news in
        one click. Returns an empty string if no articles are
        available.

        WHY RECENCY IS NOW FACTORED IN (previously picked by raw
        impact alone): a single high-impact article from weeks ago
        could stay "the representative article" indefinitely, even
        once dozens of more recent (if less dramatic) articles had
        appeared — exactly the "the linked articles are always old"
        problem observed in real use. Impact is now weighted by
        recency before picking the best one, so a strong recent
        article can win out over a stale old one, while a genuinely
        much more impactful older article can still surface if
        nothing recent comes close — it's down-weighted, not excluded.
        """
        if not entity_articles:
            return ""

        def combined_score(article: Dict[str, Any]) -> float:
            impact = (article.get("impact") or {}).get("score", 0.0) or 0.0
            published = article.get("published_at") or article.get("collected_at")
            return impact * self._recency_weight_for_display(published)

        best = max(entity_articles, key=combined_score, default=None)
        if not best or not best.get("title"):
            return ""

        title = self._escape(best["title"])
        source = self._escape(best.get("source", ""))
        url = best.get("url")

        label = f'"{title}" — {source}' if source else f'"{title}"'
        if url:
            safe_url = self._escape(url)
            return f'<a class="source-link" href="{safe_url}" target="_blank" rel="noopener noreferrer">Vezi articolul sursa: {label}</a>'
        return f'<span class="source-link-inactive">{label}</span>'

    def _render_price_sparkline(self, entity: str, price_history: Optional[List[Dict[str, Any]]]) -> str:
        """Render one small sparkline chart (<canvas> + Chart.js config) for a single entity's recent closing prices."""
        if not price_history:
            return ""

        canvas_id = "spark-" + self._anchor_id(entity)
        labels = [self._escape(p.get("date", "")) for p in price_history]
        values = [p.get("close") for p in price_history]
        is_up = len(values) >= 2 and values[-1] is not None and values[0] is not None and values[-1] >= values[0]
        line_color = "#3ecf7e" if is_up else "#f0645f"

        return f"""
        <canvas id="{canvas_id}" height="26" class="sparkline"></canvas>
        <script>
          (function() {{
            var el = document.getElementById('{canvas_id}');
            if (el && window.Chart) {{
              new Chart(el, {{
                type: 'line',
                data: {{
                  labels: {self._json_for_script(labels)},
                  datasets: [{{
                    data: {self._json_for_script(values)},
                    borderColor: '{line_color}',
                    borderWidth: 1.5,
                    fill: false,
                    tension: 0.3,
                    pointRadius: 0,
                  }}]
                }},
                options: {{
                  responsive: true,
                  maintainAspectRatio: false,
                  plugins: {{ legend: {{ display: false }}, tooltip: {{ enabled: false }} }},
                  scales: {{ x: {{ display: false }}, y: {{ display: false }} }},
                  elements: {{ point: {{ radius: 0 }} }}
                }}
              }});
            }}
          }})();
        </script>
        """

    def _render_hold_gap(self, hold_gap: Optional[Dict[str, Any]]) -> str:
        """
        Render a small "how close to actionable" indicator for a HOLD
        blocked by a specific numeric gate (confidence or impact).
        Returns an empty string for anything else (BUY/SELL/STRONG_*,
        or a HOLD from insufficient data, which has no hold_gap).
        """
        if not hold_gap:
            return ""
        label = "încredere" if hold_gap["blocked_by"] == "confidence" else "impact"
        gap = hold_gap["gap"]
        # A small gap (close to crossing the threshold) is highlighted
        # more attentively than a large one — both are shown, but a
        # near-miss is visually distinct from "nowhere close".
        color = "#e8c547" if gap <= 0.1 else "#8c8470"
        return f'<div class="hold-gap" style="color:{color};">la {gap} de prag ({label})</div>'

    def _render_recommendation_card(
        self,
        rec: Dict[str, Any],
        upgrade_downgrade_map: Optional[Dict[str, Dict[str, Any]]] = None,
        verified_track_record: Optional[Dict[str, Optional[bool]]] = None,
        entity_articles_map: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        price_history_map: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        pinned: bool = False,
    ) -> str:
        """Render one entity as a styled card: confidence bar, badges, a price sparkline, and a hidden, source-linked argument."""
        color = self._RECOMMENDATION_COLORS.get(rec["recommendation"], "#8a8f98")
        horizon = rec.get("time_horizon")
        horizon_badge = f'<span class="badge-pill">{self._escape(horizon)}</span>' if horizon else ""
        change_badge = self._change_badge(rec["entity"], upgrade_downgrade_map)
        verified_badge = self._render_verified_badge(rec["entity"], verified_track_record)
        entity_articles = (entity_articles_map or {}).get(rec["entity"])
        representative = self._representative_article(rec, entity_articles)
        sparkline = self._render_price_sparkline(rec["entity"], (price_history_map or {}).get(rec["entity"]))
        hold_gap_html = self._render_hold_gap(rec.get("hold_gap"))
        pin_class = " pinned" if pinned else ""
        pin_icon = '<span class="pin-icon">*</span>' if pinned else ""
        # "STRONG_BUY" -> "★ STRONG BUY" for display — the underlying
        # value stays exactly "STRONG_BUY" everywhere else (data-search,
        # comparisons, etc.); only the label shown to the person changes.
        verdict_label = rec["recommendation"].replace("_", " ")
        if rec["recommendation"].startswith("STRONG_"):
            verdict_label = f"★ {verdict_label}"

        return f"""
        <div class="rec-card{pin_class}" style="box-shadow: inset 3px 0 0 {color};" data-search="{self._escape(rec['entity'].lower())}">
          <div class="rc-top">
            <div>
              <div class="rc-name">{pin_icon}{self._escape(rec['entity'])}</div>
              <div class="rc-tags">{horizon_badge}{change_badge}{verified_badge}</div>
              {hold_gap_html}
            </div>
            <span class="rc-verdict" style="background:{color}22; color:{color};">{self._escape(verdict_label)}</span>
          </div>
          {sparkline}
          {self._render_confidence_bar(rec.get('confidence_score'))}
          <details class="argument">
            <summary><span class="icon">i</span> Vezi argumentul</summary>
            <div class="argument-body">
              {self._escape(rec.get('explanation', ''))}
              {self._render_breakdown(rec)}
              {representative}
            </div>
          </details>
        </div>
        """

    def _group_by_sector(
        self, recommendations: List[Dict[str, Any]], entity_sector_map: Optional[Dict[str, str]]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Group recommendations by sector, using entity_sector_map. Unmapped entities fall back to an 'Altele' group."""
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        entity_sector_map = entity_sector_map or {}
        for rec in recommendations:
            sector = entity_sector_map.get(rec["entity"], self._UNCATEGORIZED_SECTOR)
            grouped.setdefault(sector, []).append(rec)
        return grouped

    def _render_sector_section(
        self,
        sector_name: str,
        sector_recs: List[Dict[str, Any]],
        sector_scores_by_name: Dict[str, Dict[str, Any]],
        upgrade_downgrade_map: Optional[Dict[str, Dict[str, Any]]],
        verified_track_record: Optional[Dict[str, Optional[bool]]],
        entity_articles_map: Optional[Dict[str, List[Dict[str, Any]]]],
        price_history_map: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> str:
        """Render one sector section: header with sector-level stats, then its entity cards."""
        stats = sector_scores_by_name.get(sector_name, {})
        article_count = stats.get("article_count", "-")
        source_count = stats.get("distinct_source_count", "-")
        dominant = stats.get("dominant_sentiment", "neutral")
        consistency = stats.get("sentiment_consistency")
        tint = self._SECTOR_TINTS.get(dominant, "#1a1f2e")
        consistency_text = f" · consistență {round(consistency * 100)}%" if consistency is not None else ""

        slug = self._anchor_id(sector_name)
        cards = "".join(
            self._render_recommendation_card(
                r, upgrade_downgrade_map, verified_track_record, entity_articles_map, price_history_map
            )
            for r in sector_recs
        )

        return f"""
        <div class="sector-block" id="sector-{slug}">
          <div class="sector-header" style="background:{tint};">
            <span class="sector-name">{self._escape(sector_name)}</span>
            <span class="sector-meta">{self._escape(article_count)} articole · {self._escape(source_count)} surse</span>
            <span class="sentiment-tag">{self._escape(dominant)}{consistency_text}</span>
          </div>
          <div class="rec-cards">{cards}</div>
        </div>
        """

    def _render_watchlist_section(
        self,
        watchlist_recs: List[Dict[str, Any]],
        upgrade_downgrade_map: Optional[Dict[str, Dict[str, Any]]],
        verified_track_record: Optional[Dict[str, Optional[bool]]],
        entity_articles_map: Optional[Dict[str, List[Dict[str, Any]]]],
        price_history_map: Optional[Dict[str, List[Dict[str, Any]]]],
    ) -> str:
        """Render the pinned Watchlist section — full-detail cards for the person's own chosen companies, shown first."""
        if not watchlist_recs:
            return ""
        cards = "".join(
            self._render_recommendation_card(
                r, upgrade_downgrade_map, verified_track_record, entity_articles_map, price_history_map, pinned=True
            )
            for r in watchlist_recs
        )
        return f"""
        <div class="section-title" id="watchlist">
          Watchlist-ul tau
          <span class="section-hint">companiile pe care le urmaresti, mereu sus, indiferent de sector</span>
        </div>
        <div class="rec-cards">{cards}</div>
        """

    def _render_market_data_table(
        self,
        market_data: Optional[Dict[str, Dict[str, Any]]],
        risk_data: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> str:
        """Render a factual market-data table — never a valuation verdict."""
        if not market_data:
            return '<p class="empty-state">Nicio dată de piață disponibilă.</p>'

        rows = []
        for ticker, snap in market_data.items():
            risk = (risk_data or {}).get(ticker, {})
            risk_cell = (
                f"{self._escape(risk.get('risk_level'))} ({self._escape(risk.get('annualized_volatility_pct'))}%)"
                if risk and not risk.get("error") else "-"
            )
            if snap.get("error"):
                rows.append(f"""
                <tr>
                  <td>{self._escape(ticker)}</td>
                  <td colspan="6" class="empty-state">{self._escape(snap["error"])}</td>
                </tr>
                """)
                continue

            rows.append(f"""
            <tr>
              <td>{self._escape(ticker)}</td>
              <td>{self._escape(snap.get('current_price'))}</td>
              <td>{self._escape(snap.get('daily_change_pct'))}%</td>
              <td>{self._escape(snap.get('pct_from_52w_high'))}%</td>
              <td>{self._escape(snap.get('pct_from_52w_low'))}%</td>
              <td>{self._escape(snap.get('trailing_pe'))}</td>
              <td>{risk_cell}</td>
            </tr>
            """)

        return f"""
        <table>
          <thead><tr><th>Ticker</th><th>Preț curent</th><th>Variație zilnică</th><th>Față de max 52săpt</th><th>Față de min 52săpt</th><th>P/E</th><th>Risc (volatilitate anualizată)</th></tr></thead>
          <tbody>{"".join(rows)}</tbody>
        </table>
        """

    def _render_macro_table(self, macro_snapshots: Optional[Dict[str, Dict[str, Any]]]) -> str:
        """
        Render a factual price table for Indices/Commodities/Forex —
        same "facts, no verdict" philosophy as the company market-data
        table. No BUY/SELL logic applies here (see market_instruments.py
        — these deliberately aren't news-detected entities), just the
        current snapshot from real market data.
        """
        if not macro_snapshots:
            return '<p class="empty-state">Nicio dată macro disponibilă.</p>'

        rows = []
        for name, snap in macro_snapshots.items():
            if snap.get("error"):
                rows.append(f'<tr><td>{self._escape(name)}</td><td colspan="2" class="empty-state">{self._escape(snap["error"])}</td></tr>')
                continue
            change = snap.get("daily_change_pct")
            color = "#3ecf7e" if (change or 0) >= 0 else "#d4695a"
            rows.append(f"""
            <tr>
              <td>{self._escape(name)}</td>
              <td>{self._escape(snap.get('current_price'))}</td>
              <td style="color:{color};">{self._escape(change)}%</td>
            </tr>
            """)

        return f"""
        <table>
          <thead><tr><th>Instrument</th><th>Preț curent</th><th>Variație zilnică</th></tr></thead>
          <tbody>{"".join(rows)}</tbody>
        </table>
        """

    def _render_macro_indicators(self, macro_indicators: Optional[List[Dict[str, Any]]]) -> str:
        """
        Render real macroeconomic indicators (GDP, inflation,
        unemployment, interest rates — via FRED) as plain facts, each
        with the date it was actually published (economic data is
        often published with a lag, unlike a live stock price).
        """
        if not macro_indicators:
            return '<p class="empty-state">Niciun indicator macroeconomic disponibil (necesită cheie FRED_API_KEY configurată).</p>'

        items = "".join(
            f"""
            <div class="index-box-like" style="display:inline-block; background:#1c1810; border:1px solid #33301f; border-radius:6px; padding:10px 16px; margin:0 8px 8px 0;">
              <div style="font-size:10px; color:#8c8470; text-transform:uppercase;">{self._escape(ind.get('label'))}</div>
              <div style="font-size:18px; font-weight:700; color:#f5f1e6;">{self._escape(ind.get('value'))}</div>
              <div style="font-size:10px; color:#8c8470;">la {self._escape(ind.get('date'))}</div>
            </div>
            """
            for ind in macro_indicators
        )
        return f'<div>{items}</div>'

    def _render_economic_calendar(self, fomc_meetings: Optional[List[Dict[str, Any]]]) -> str:
        """
        Render upcoming FOMC (Federal Reserve) meeting dates — see
        economic_calendar.py for why this is deliberately scoped to
        ONLY this one, precisely-known recurring event, rather than a
        full commercial-style economic calendar.
        """
        if not fomc_meetings:
            return '<p class="empty-state">Niciun eveniment programat disponibil.</p>'

        items = []
        for m in fomc_meetings:
            start = m.get("start")
            end = m.get("end")
            days_until = m.get("days_until")
            when = f"{self._escape(start)} – {self._escape(end)}" if start != end else self._escape(start)
            countdown = f"peste {days_until} zile" if days_until and days_until > 0 else "în curs / azi"
            items.append(f"""
            <div class="change-line">
              <b>Ședință FOMC (Fed)</b> — {when}
              <span style="color:#8c8470; margin-left:8px;">({countdown})</span>
            </div>
            """)
        return "".join(items)

    def _render_source_credibility(self, source_summary: Optional[List[Dict[str, Any]]]) -> str:
        """
        Render the source-tier transparency breakdown — see
        source_credibility.py for why this is a TRANSPARENCY layer
        (where did coverage come from), not a "fake news" verdict.
        """
        if not source_summary:
            return '<p class="empty-state">Nicio distribuție pe surse disponibilă încă.</p>'

        blocks = []
        for tier in source_summary:
            source_list = ", ".join(
                f'{self._escape(s["name"])} ({s["article_count"]})' for s in tier.get("sources", [])
            )
            blocks.append(f"""
            <div class="change-line">
              <b>{self._escape(tier.get('tier_label'))}</b> — {tier.get('article_count')} articole
              <div style="font-size:11px; color:#8c8470; margin-top:4px;">{source_list}</div>
            </div>
            """)
        return "".join(blocks)

    def _render_portfolio_summary(self, portfolio_result: Optional[Dict[str, Any]]) -> str:
        """Render the hypothetical portfolio simulation summary cards."""
        if not portfolio_result or not portfolio_result.get("trades_simulated"):
            return '<p class="empty-state">Nicio simulare de portofoliu disponibilă (necesită recomandări deja verificate prin Backtest).</p>'
        r = portfolio_result
        color = "#3ecf7e" if (r["total_return_pct"] or 0) >= 0 else "#f0645f"
        return f"""
        <div class="kpi-row">
          <div class="kpi"><div class="n">${self._escape(r['total_invested'])}</div><div class="l">Investit (simulat)</div></div>
          <div class="kpi"><div class="n">${self._escape(r['total_final_value'])}</div><div class="l">Valoare finală</div></div>
          <div class="kpi"><div class="n" style="color:{color}">{self._escape(r['total_return_pct'])}%</div><div class="l">Randament total</div></div>
          <div class="kpi"><div class="n">{self._escape(r['trades_simulated'])}</div><div class="l">Tranzacții simulate</div></div>
        </div>
        """

    def _render_portfolio_chart(self, portfolio_history: Optional[List[Dict[str, Any]]]) -> str:
        """Render the portfolio-return-over-time line chart (a <canvas> + Chart.js config)."""
        if not portfolio_history:
            return '<p class="empty-state">Niciun istoric de portofoliu încă — apare pe măsură ce pipeline-ul rulează automat, zi de zi.</p>'

        labels = [self._escape((s.get("recorded_at") or "")[:10]) for s in portfolio_history]
        values = [s.get("total_return_pct") for s in portfolio_history]

        return f"""
        <canvas id="portfolioChart" height="70"></canvas>
        <script>
          (function() {{
            if (window.Chart) {{
              new Chart(document.getElementById('portfolioChart'), {{
                type: 'line',
                data: {{
                  labels: {self._json_for_script(labels)},
                  datasets: [{{
                    label: 'Randament portofoliu simulat (%)',
                    data: {self._json_for_script(values)},
                    borderColor: '#4a90d9',
                    backgroundColor: 'rgba(74,144,217,0.15)',
                    fill: true,
                    tension: 0.25,
                    pointRadius: 2,
                  }}]
                }},
                options: {{
                  responsive: true,
                  plugins: {{ legend: {{ display: false }} }},
                  scales: {{
                    x: {{ ticks: {{ color: '#9aa0a6' }}, grid: {{ color: '#20232b' }} }},
                    y: {{ ticks: {{ color: '#9aa0a6' }}, grid: {{ color: '#20232b' }} }}
                  }}
                }}
              }});
            }}
          }})();
        </script>
        """

    def _render_accuracy_chart(self, accuracy_history: Optional[List[Dict[str, Any]]]) -> str:
        """
        Render the cumulative hit-rate-over-time line chart — makes
        VISIBLE (instead of just claimed) whether Backtest Engine's
        track record is actually improving as more recommendations get
        checked, rather than only ever showing a single current number.
        """
        if not accuracy_history:
            return '<p class="empty-state">Niciun istoric de precizie încă — apare pe măsură ce recomandările ajung la scadență și sunt verificate.</p>'

        labels = [self._escape((s.get("checked_at") or "")[:10]) for s in accuracy_history]
        values = [round((s.get("cumulative_hit_rate") or 0) * 100, 1) for s in accuracy_history]
        counts = [s.get("cumulative_checked") for s in accuracy_history]

        return f"""
        <canvas id="accuracyChart" height="70"></canvas>
        <script>
          (function() {{
            if (window.Chart) {{
              new Chart(document.getElementById('accuracyChart'), {{
                type: 'line',
                data: {{
                  labels: {self._json_for_script(labels)},
                  datasets: [{{
                    label: 'Rată de succes cumulativă (%)',
                    data: {self._json_for_script(values)},
                    borderColor: '#e8c547',
                    backgroundColor: 'rgba(232,197,71,0.12)',
                    fill: true,
                    tension: 0.25,
                    pointRadius: 2,
                  }}]
                }},
                options: {{
                  responsive: true,
                  plugins: {{ legend: {{ display: false }} }},
                  scales: {{
                    x: {{ ticks: {{ color: '#9aa0a6' }}, grid: {{ color: '#20232b' }} }},
                    y: {{ min: 0, max: 100, ticks: {{ color: '#9aa0a6' }}, grid: {{ color: '#20232b' }} }}
                  }}
                }}
              }});
            }}
          }})();
        </script>
        """

    def _render_calibration_chart(self, calibration_report: Optional[List[Dict[str, Any]]]) -> str:
        """
        Render the confidence calibration bar chart — checks whether a
        HIGHER confidence score actually correlates with being right
        MORE OFTEN, using real outcomes grouped by confidence bucket,
        instead of assuming the score means something because the
        formula that produces it looks reasonable. A count of checked
        recommendations is listed under each bar, since a bucket with
        very few checks is far less reliable than one with many.
        """
        if not calibration_report:
            return '<p class="empty-state">Niciun raport de calibrare încă — necesită recomandări verificate în mai multe intervale de încredere.</p>'

        labels = [self._escape(b["bucket_label"]) for b in calibration_report]
        values = [round((b.get("hit_rate") or 0) * 100, 1) for b in calibration_report]
        counts_line = " · ".join(
            f'{self._escape(b["bucket_label"])}: {b["count"]} verificări' for b in calibration_report
        )

        return f"""
        <canvas id="calibrationChart" height="70"></canvas>
        <div style="font-size:11px; color:#8c8470; margin-top:8px;">{counts_line}</div>
        <script>
          (function() {{
            if (window.Chart) {{
              new Chart(document.getElementById('calibrationChart'), {{
                type: 'bar',
                data: {{
                  labels: {self._json_for_script(labels)},
                  datasets: [{{
                    label: 'Rată de succes reală (%)',
                    data: {self._json_for_script(values)},
                    backgroundColor: '#4a90d9',
                  }}]
                }},
                options: {{
                  responsive: true,
                  plugins: {{ legend: {{ display: false }} }},
                  scales: {{
                    x: {{ ticks: {{ color: '#9aa0a6' }}, grid: {{ color: '#20232b' }} }},
                    y: {{ min: 0, max: 100, ticks: {{ color: '#9aa0a6' }}, grid: {{ color: '#20232b' }} }}
                  }}
                }}
              }});
            }}
          }})();
        </script>
        """

    def _render_changes_section(self, upgrade_downgrade_results: Optional[List[Dict[str, Any]]]) -> str:
        """Render the list of entities whose recommendation changed since it was last logged."""
        if not upgrade_downgrade_results:
            return '<p class="empty-state">Nicio schimbare de recomandare de urmărit încă.</p>'
        changes = [r for r in upgrade_downgrade_results if r.get("change") in ("upgrade", "downgrade")]
        if not changes:
            return '<p class="empty-state">Nicio schimbare azi — toate recomandările au rămas neschimbate.</p>'
        lines = []
        for r in changes:
            arrow = "up" if r["change"] == "upgrade" else "down"
            color = "#3ecf7e" if r["change"] == "upgrade" else "#f0645f"
            lines.append(
                f'<div class="change-line" style="color:{color};">{arrow} <b>{self._escape(r["entity"])}</b> '
                f'{self._escape(r.get("previous"))} -&gt; {self._escape(r.get("current"))}</div>'
            )
        return "".join(lines)

    def _render_events_section(self, events: Optional[List[Dict[str, Any]]]) -> str:
        """
        Render fused Events (see event_fusion.py) that are CONFIRMED
        by 2+ independent sources — multi-source confirmation is
        itself a real credibility signal, worth surfacing separately
        from the ordinary per-entity cards. Single-source events aren't
        hidden data, they just don't add anything beyond what that
        entity's own card in Sectoare already shows, so they're omitted
        here to keep this section meaningful rather than noisy.
        """
        if not events:
            return '<p class="empty-state">Niciun eveniment confirmat de mai multe surse încă.</p>'

        confirmed = [e for e in events if e.get("confirmed_by_multiple_sources")]
        if not confirmed:
            return '<p class="empty-state">Niciun eveniment confirmat de mai multe surse încă.</p>'

        # Most well-corroborated events first; capped so this section
        # stays scannable even with heavy news days.
        confirmed = sorted(confirmed, key=lambda e: e.get("source_count", 0), reverse=True)[:15]

        lines = []
        for e in confirmed:
            entity = self._escape(e.get("entity"))
            event_type = self._escape(e.get("event_type"))
            source_count = e.get("source_count", 0)
            rep_title = self._escape(e.get("representative_title") or "")
            rep_source = self._escape(e.get("representative_source") or "")
            rep_url = e.get("representative_url")
            label = f'"{rep_title}" — {rep_source}' if rep_source else f'"{rep_title}"'
            if rep_url:
                link = f'<a class="source-link" href="{self._escape(rep_url)}" target="_blank" rel="noopener noreferrer">{label}</a>'
            else:
                link = f'<span class="source-link-inactive">{label}</span>'
            lines.append(f"""
            <div class="change-line">
              <b>{entity}</b> · {event_type} · confirmat de {source_count} surse independente
              <div style="margin-top:4px;">{link}</div>
            </div>
            """)
        return "".join(lines)

    def _render_masthead(
        self,
        generated_at: str,
        sector_names: List[str],
        total_entities: int,
        watchlist_count: int,
        changes_count: int,
    ) -> str:
        """Render the newspaper-style masthead + horizontal section nav strip (replaces the old sidebar — same anchor ids/hrefs, so every section remains reachable via a plain link, JS or not)."""
        watchlist_link = (
            f'<a href="#watchlist">Watchlist <span class="count">{watchlist_count}</span></a>'
            if watchlist_count else ""
        )
        return f"""
        <div class="masthead">
          <div class="brand">The MarketLens Journal</div>
          <div class="sub">{generated_at} · {total_entities} companii urmărite</div>
        </div>
        <div class="nav-strip">
          <a href="#rezumat">Rezumat</a>
          {watchlist_link}
          <a href="#sectoare">Sectoare <span class="count">{len(sector_names)}</span></a>
          <a href="#portofoliu">Portofoliu</a>
          <a href="#evenimente">Evenimente</a>
          <a href="#piata">Date de piață</a>
          <a href="#schimbari">Schimbări <span class="count">{changes_count}</span></a>
          <span class="count">{total_entities} entități</span>
        </div>
        """

    def generate_report(
        self,
        recommendations: List[Dict[str, Any]],
        articles: Optional[List[Dict[str, Any]]] = None,
        db_stats: Optional[Dict[str, Any]] = None,
        market_data: Optional[Dict[str, Dict[str, Any]]] = None,
        risk_data: Optional[Dict[str, Dict[str, Any]]] = None,
        sector_scores: Optional[List[Dict[str, Any]]] = None,
        upgrade_downgrade_map: Optional[Dict[str, Dict[str, Any]]] = None,
        portfolio_result: Optional[Dict[str, Any]] = None,
        daily_summary_text: Optional[str] = None,
        entity_sector_map: Optional[Dict[str, str]] = None,
        verified_track_record: Optional[Dict[str, Optional[bool]]] = None,
        entity_articles_map: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        price_history_map: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        portfolio_history: Optional[List[Dict[str, Any]]] = None,
        watchlist: Optional[List[str]] = None,
        upgrade_downgrade_results: Optional[List[Dict[str, Any]]] = None,
        accuracy_history: Optional[List[Dict[str, Any]]] = None,
        calibration_report: Optional[List[Dict[str, Any]]] = None,
        events: Optional[List[Dict[str, Any]]] = None,
        macro_snapshots: Optional[Dict[str, Dict[str, Any]]] = None,
        macro_indicators: Optional[List[Dict[str, Any]]] = None,
        fomc_meetings: Optional[List[Dict[str, Any]]] = None,
        source_summary: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        Build the full HTML report as a single string.

        Args:
            recommendations: output of RecommendationEngine.recommend_all()
            articles: underlying processed articles; optional (kept for API compatibility)
            db_stats: output of NewsDatabase.get_stats(); optional
            market_data: output of MarketDataFetcher.get_snapshots_batch(); optional
            risk_data: output of RiskScoreCalculator.get_risk_scores_batch(); optional
            sector_scores: output of SectorAggregator.score_all_sectors(); optional
            upgrade_downgrade_map: entity -> UpgradeDowngradeTracker.compare_entity() result; optional
            portfolio_result: output of PortfolioSimulator.simulate(); optional
            daily_summary_text: output of DailySummaryGenerator.generate(); optional
            entity_sector_map: entity name -> sector name; drives sector grouping.
            verified_track_record: entity -> True/False/None, most recent Backtest Engine outcome.
            entity_articles_map: entity -> its articles, used to pick the
                representative source article (now shown as a clickable link).
            price_history_map: entity name -> price history series (sparklines).
            portfolio_history: output of PortfolioHistory.load_all().
            watchlist: optional list of entity names (case-insensitive).
                UNLIKE v1, this does NOT filter out everything else — it
                PINS those entities in a dedicated "Watchlist" section at
                the top, removed from their sector group below (so
                they're never shown twice). None/empty means no pinned
                section; the rest of the report is unaffected either way.
            upgrade_downgrade_results: the full list (not just the map) —
                used to render the "Schimbări" section. Derived from
                upgrade_downgrade_map's values if omitted.

        Returns:
            A complete, standalone HTML document (string).
        """
        articles = articles or []
        db_stats = db_stats or {}
        sector_scores = sector_scores or []
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        counts = {"STRONG_BUY": 0, "BUY": 0, "HOLD": 0, "SELL": 0, "STRONG_SELL": 0}
        for r in recommendations:
            counts[r["recommendation"]] = counts.get(r["recommendation"], 0) + 1
        total_buy = counts["BUY"] + counts["STRONG_BUY"]
        total_sell = counts["SELL"] + counts["STRONG_SELL"]

        watchlist_recs: List[Dict[str, Any]] = []
        remaining_recs = recommendations
        if watchlist:
            watchlist_lower = {name.lower() for name in watchlist}
            watchlist_recs = [r for r in recommendations if r["entity"].lower() in watchlist_lower]
            remaining_recs = [r for r in recommendations if r["entity"].lower() not in watchlist_lower]

        sector_scores_by_name = {s["sector"]: s for s in sector_scores}
        grouped = self._group_by_sector(remaining_recs, entity_sector_map)
        sector_names = sorted(
            grouped.keys(),
            key=lambda name: sector_scores_by_name.get(name, {}).get("article_count", 0),
            reverse=True,
        )

        sector_sections = "".join(
            self._render_sector_section(
                name, grouped[name], sector_scores_by_name,
                upgrade_downgrade_map, verified_track_record, entity_articles_map, price_history_map,
            )
            for name in sector_names
        )

        watchlist_section = self._render_watchlist_section(
            watchlist_recs, upgrade_downgrade_map, verified_track_record, entity_articles_map, price_history_map,
        )

        if upgrade_downgrade_results is None:
            upgrade_downgrade_results = list((upgrade_downgrade_map or {}).values())
        changes_count = sum(1 for r in upgrade_downgrade_results if r.get("change") in ("upgrade", "downgrade"))

        masthead = self._render_masthead(generated_at, sector_names, len(recommendations), len(watchlist_recs), changes_count)

        return f"""<!DOCTYPE html>
<html lang="ro">
<head>
<meta charset="utf-8">
<title>MarketLens — Raport de Investiții</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;800;900&family=Source+Sans+3:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family:'Source Sans 3', Georgia, serif; background:#0d0c0a; color:#eae6da; margin:0; padding:0; }}

  .masthead {{ text-align:center; border-bottom:4px double #eae6da; padding:28px 24px 16px 24px; }}
  .masthead .brand {{ font-family:'Playfair Display', serif; font-size:38px; font-weight:900; margin:0; letter-spacing:1px; color:#f5f1e6; }}
  .masthead .sub {{ font-size:11px; letter-spacing:3px; text-transform:uppercase; color:#8c8470; margin-top:6px; }}

  .nav-strip {{ display:flex; justify-content:center; flex-wrap:wrap; gap:26px; padding:12px 24px; border-bottom:1px solid #33301f; font-size:11px; letter-spacing:1px; text-transform:uppercase; position:sticky; top:0; background:#0d0c0a; z-index:10; }}
  .nav-strip a, .nav-strip span.count:last-child {{ margin:0 13px; }}
  .nav-strip a {{ color:#c8c2ae; text-decoration:none; }}
  .nav-strip a:hover {{ color:#d4915a; }}
  .nav-strip .count {{ color:#5f5a48; margin-left:3px; }}

  .main {{ max-width:1400px; margin:0 auto; padding:28px 40px 48px 40px; }}

  .hero-eyebrow {{ display:none; }}
  .hero-number {{ font-family:'Playfair Display', serif; font-size:52px; font-weight:800; text-align:center; margin:12px 0; color:#3ecf7e; background:none; -webkit-text-fill-color:initial; }}
  .hero-summary {{ max-width:720px; margin:0 auto 20px auto; text-align:center; font-size:13.5px; color:#c8c2ae; font-style:italic; line-height:1.6; }}

  .kpi-row {{ display:flex; justify-content:center; flex-wrap:wrap; gap:44px; border-top:1px solid #33301f; border-bottom:1px solid #33301f; padding:16px 0; margin:16px 0 8px 0; }}
  .kpi {{ text-align:center; background:none; padding:0; margin:0 22px; }}
  .kpi .n {{ font-size:20px; font-weight:700; display:block; color:#f5f1e6; }}
  .kpi .l {{ font-size:10px; text-transform:uppercase; letter-spacing:1px; color:#8c8470; margin-top:2px; }}

  .search-box {{ display:block; margin:22px auto; max-width:340px; width:100%; background:transparent; border:1px solid #33301f; border-radius:0; padding:9px 14px; color:#eae6da; font-family:'Source Sans 3', sans-serif; font-size:13px; text-align:center; }}
  .search-box::placeholder {{ color:#5f5a48; font-style:italic; }}

  .section-title {{ font-family:'Playfair Display', serif; font-size:23px; font-weight:800; text-transform:none; letter-spacing:0; border-bottom:2px solid #eae6da; padding-bottom:8px; margin:40px 0 18px 0; color:#f5f1e6; scroll-margin-top:56px; }}
  .section-hint {{ font-family:'Source Sans 3', sans-serif; font-size:11px; color:#8c8470; font-weight:400; text-transform:none; margin-left:10px; }}

  .rec-cards {{ columns:3; column-gap:32px; }}
  .rec-card {{ background:none; border-radius:0; padding:0; break-inside:avoid; display:block; margin-bottom:24px; padding-bottom:18px; border-bottom:1px solid #2a2717; }}
  .rec-card.pinned {{ box-shadow:none !important; background:#1c1810; padding:14px 16px; border-bottom:none; border-left:3px solid #d4b545; margin-bottom:16px; }}
  .rc-top {{ display:flex; justify-content:space-between; align-items:flex-start; }}
  .rc-name {{ font-family:'Playfair Display', serif; font-size:18px; font-weight:700; color:#f5f1e6; }}
  .pin-icon {{ color:#d4b545; margin-right:5px; }}
  .rc-tags {{ display:flex; gap:6px; flex-wrap:wrap; margin-top:6px; }}
  .hold-gap {{ font-size:10px; margin-top:4px; font-style:italic; }}
  .badge-pill {{ font-size:9px; font-weight:700; text-transform:uppercase; letter-spacing:0.5px; background:none; color:#8c8470; border:1px solid #33301f; padding:2px 7px; border-radius:0; }}
  .badge-pill.change-up {{ background:none; color:#3ecf7e; border-color:#254a35; }}
  .badge-pill.change-down {{ background:none; color:#d4695a; border-color:#4a2a28; }}
  .badge-pill.verified-ok {{ background:none; color:#3ecf7e; border-color:#254a35; }}
  .badge-pill.verified-bad {{ background:none; color:#d4695a; border-color:#4a2a28; }}
  .rc-verdict {{ font-family:'Source Sans 3', sans-serif; font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:1px; padding:3px 10px; border-radius:0; }}
  .sparkline {{ width:100%; max-height:24px; margin-top:8px; }}

  .conf-track {{ height:2px; border-radius:0; background:#2a2717; margin-top:10px; overflow:hidden; }}
  .conf-fill {{ height:100%; }}
  .conf-label {{ font-size:10px; color:#8c8470; margin-top:3px; }}

  details.argument {{ margin-top:10px; }}
  details.argument summary {{ cursor:pointer; list-style:none; font-size:11px; color:#8c8470; display:flex; align-items:center; gap:5px; user-select:none; }}
  details.argument summary::-webkit-details-marker {{ display:none; }}
  details.argument summary .icon {{ display:inline-flex; align-items:center; justify-content:center; width:15px; height:15px; border-radius:50%; border:1px solid #5f5a48; color:#8c8470; font-size:9px; font-weight:800; }}
  details.argument[open] summary .icon {{ background:#d4915a; border-color:#d4915a; color:#0d0c0a; }}
  .argument-body {{ background:none; border-radius:0; padding:0; margin-top:8px; font-size:12.5px; line-height:1.6; color:#c8c2ae; border-left:2px solid #2a2717; padding-left:12px; }}
  .breakdown {{ display:flex; gap:6px; flex-wrap:wrap; margin:8px 0; }}
  .breakdown .chip {{ font-size:9.5px; background:none; color:#8c8470; border:1px solid #33301f; padding:2px 7px; border-radius:0; }}
  .source-link {{ display:block; color:#d4915a; text-decoration:none; margin-top:8px; font-style:italic; }}
  .source-link:hover {{ text-decoration:underline; }}
  .source-link-inactive {{ display:block; color:#5f5a48; font-style:italic; margin-top:8px; }}

  .sector-block {{ margin-bottom:8px; scroll-margin-top:56px; }}
  .sector-header {{ display:flex; align-items:baseline; gap:14px; padding:0 0 10px 0; margin-bottom:18px; border-bottom:1px solid #33301f; background:none !important; border-radius:0; }}
  .sector-name {{ font-family:'Playfair Display', serif; font-size:19px; font-weight:800; text-transform:none; letter-spacing:0; color:#f5f1e6; }}
  .sector-meta {{ font-size:11px; opacity:0.7; color:#8c8470; }}
  .sentiment-tag {{ margin-left:auto; font-size:10px; font-weight:600; text-transform:uppercase; letter-spacing:0.5px; padding:0; border-radius:0; background:none !important; color:#8c8470; }}

  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th {{ text-align:left; font-family:'Playfair Display', serif; font-weight:700; color:#f5f1e6; padding:8px 10px; border-bottom:2px solid #eae6da; font-size:12px; text-transform:none; }}
  td {{ padding:8px 10px; border-bottom:1px solid #2a2717; color:#c8c2ae; }}

  .change-line {{ font-size:13px; padding:8px 0; border-bottom:1px solid #2a2717; }}
  .empty-state {{ color:#8c8470; font-size:13px; font-style:italic; }}
  .footer {{ margin-top:48px; padding:20px 0 32px 0; border-top:1px solid #33301f; font-size:11px; color:#5f5a48; text-align:center; }}
</style>
</head>
<body>
  {masthead}

  <div class="main">
    <div id="rezumat"></div>
    <div class="hero-number">{total_buy} BUY · {total_sell} SELL · {counts['HOLD']} HOLD</div>
    {f'<div class="hero-summary">{self._escape(daily_summary_text)}</div>' if daily_summary_text else ''}

    <div class="kpi-row">
      <div class="kpi"><div class="n" style="color:#2ecc71">{counts['STRONG_BUY']}</div><div class="l">Strong Buy</div></div>
      <div class="kpi"><div class="n" style="color:#3ecf7e">{counts['BUY']}</div><div class="l">Buy</div></div>
      <div class="kpi"><div class="n">{counts['HOLD']}</div><div class="l">Hold</div></div>
      <div class="kpi"><div class="n" style="color:#d4695a">{counts['SELL']}</div><div class="l">Sell</div></div>
      <div class="kpi"><div class="n" style="color:#e63946">{counts['STRONG_SELL']}</div><div class="l">Strong Sell</div></div>
      <div class="kpi"><div class="n">{self._escape(db_stats.get('total_articles', '-'))}</div><div class="l">Articole</div></div>
      <div class="kpi"><div class="n">{self._escape(db_stats.get('distinct_sources', '-'))}</div><div class="l">Surse</div></div>
    </div>

    <input class="search-box" type="text" placeholder="Caută o companie..." oninput="marketlensFilter(this.value)">

    {watchlist_section}

    <div class="section-title" id="sectoare">Sectoare <span class="section-hint">{len(sector_names)} sectoare, toate entitățile urmărite</span></div>
    {sector_sections}

    <div class="section-title" id="portofoliu">Simulare portofoliu <span class="section-hint">(recomandări deja verificate prin Backtest)</span></div>
    {self._render_portfolio_summary(portfolio_result)}
    <div class="chart-frame" style="margin-top:16px;">{self._render_portfolio_chart(portfolio_history)}</div>

    <div class="section-title">Rată de succes în timp <span class="section-hint">(precizie cumulativă a Backtest Engine)</span></div>
    <div class="chart-frame">{self._render_accuracy_chart(accuracy_history)}</div>

    <div class="section-title">Calibrarea încrederii <span class="section-hint">(înseamnă cu adevărat ceva scorul de încredere?)</span></div>
    <div class="chart-frame">{self._render_calibration_chart(calibration_report)}</div>

    <div class="section-title" id="evenimente">Evenimente confirmate <span class="section-hint">(aceeași știre, raportată independent de mai multe surse)</span></div>
    {self._render_events_section(events)}

    <div class="section-title" id="piata">Date de piață <span class="section-hint">(fapte reale — fără verdict de subevaluare/supraevaluare)</span></div>
    {self._render_market_data_table(market_data, risk_data)}

    <div class="section-title">Prezentare macro <span class="section-hint">(indici, mărfuri — fapte reale, fără verdict)</span></div>
    {self._render_macro_table(macro_snapshots)}

    <div class="section-title">Indicatori macroeconomici <span class="section-hint">(date reale, publicate de Fed St. Louis — FRED)</span></div>
    {self._render_macro_indicators(macro_indicators)}

    <div class="section-title">Calendar economic <span class="section-hint">(ședințe Fed cunoscute, anunțate oficial)</span></div>
    {self._render_economic_calendar(fomc_meetings)}

    <div class="section-title">Credibilitate surse <span class="section-hint">(transparență — de unde vine acoperirea, nu un verdict de adevăr)</span></div>
    {self._render_source_credibility(source_summary)}

    <div class="section-title" id="schimbari">Schimbări recente</div>
    {self._render_changes_section(upgrade_downgrade_results)}

    <div class="footer">MarketLens — raport generat automat. Nu constituie sfat financiar.</div>
  </div>

  <script>
    function marketlensFilter(query) {{
      var q = query.trim().toLowerCase();
      document.querySelectorAll('.rec-card').forEach(function(card) {{
        var match = card.getAttribute('data-search').indexOf(q) !== -1;
        card.style.display = match ? '' : 'none';
      }});
    }}
  </script>
</body>
</html>"""

    def save_report(self, html: str, path: str) -> None:
        """Write the generated HTML report to a file."""
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
