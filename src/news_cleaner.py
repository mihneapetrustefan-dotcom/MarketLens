"""
news_cleaner.py
-----------------
News Cleaner module for MarketLens.

RESPONSIBILITY (Single Responsibility Principle):
Takes raw, standardized article dicts (produced by any Collector — RSS,
API, or Web Scraper) and produces a CLEANED version of the same schema:
- HTML tags/entities removed from text fields
- whitespace normalized
- tracking query parameters stripped from URLs
- articles with no usable content discarded

This module does NOT do NLP (no sentiment, no ticker/company detection,
no duplicate detection) — those remain separate modules, so each can be
built, tested, and replaced independently.
"""

import re
import html
import logging
from typing import List, Dict, Any, Optional
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

logger = logging.getLogger("marketlens.news_cleaner")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class NewsCleaner:
    """
    Cleans and validates standardized news article dictionaries.
    """

    # Precompiled once at class level (not per call): this pattern never
    # changes and is applied to every article, so compiling it once
    # avoids needless re-compilation overhead at scale.
    _HTML_TAG_RE = re.compile(r"<[^>]+>")

    # Collapses any run of whitespace (spaces, tabs, newlines) into one space.
    _WHITESPACE_RE = re.compile(r"\s+")

    # Known tracking query parameters, stripped from every URL. Kept as
    # a set (not hardcoded inline) so new ones can be added later
    # without touching the cleaning logic itself.
    _TRACKING_PARAMS = {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "cmpid",
    }

    # Minimum number of words a cleaned title must have to count as a
    # real headline rather than empty/junk content.
    MIN_TITLE_WORDS = 3

    def strip_html(self, text: str) -> str:
        """
        Remove HTML tags and decode HTML entities from a text field.

        WHY NEEDED: several RSS feeds (Yahoo Finance, MarketWatch, ...)
        embed raw HTML inside <summary> (e.g. "<p>Some text</p>&nbsp;").
        Downstream NLP modules (sentiment, ticker detection) need plain
        text, not markup.

        We use a regex tag-stripper + html.unescape() instead of a full
        HTML parser (e.g. BeautifulSoup) to keep this module dependency-
        free — RSS summaries are simple enough that this is reliable and
        far cheaper to run at the scale this platform targets (thousands
        of articles).
        """
        if not text:
            return ""
        # Strip tags FIRST, then unescape entities. Doing it in this
        # order matters: unescaping first could turn literal text like
        # "&lt;script&gt;" into a real <script> tag that then gets
        # silently stripped away, losing information about what the
        # original text actually said.
        without_tags = self._HTML_TAG_RE.sub(" ", text)
        return html.unescape(without_tags)

    def normalize_whitespace(self, text: str) -> str:
        """
        Collapse any sequence of whitespace into a single space and trim
        leading/trailing whitespace.

        WHY NEEDED: HTML stripping above often leaves double spaces or
        stray newlines where tags used to be. Downstream text matching
        (Duplicate Detector) and NLP work best on normalized text.
        """
        if not text:
            return ""
        return self._WHITESPACE_RE.sub(" ", text).strip()

    def clean_text(self, text: str) -> str:
        """
        Full text-cleaning pipeline for one field: strip HTML, then
        normalize whitespace. Combined into a single method because
        every text field (title, summary) needs both steps, always in
        this order — avoids callers forgetting one of the two steps.
        """
        return self.normalize_whitespace(self.strip_html(text))

    def clean_url(self, url: str) -> str:
        """
        Remove tracking query parameters from a URL while preserving
        everything else (scheme, host, path, non-tracking params).

        WHY NEEDED: many sources append tracking params (utm_source=Rss,
        fbclid=..., etc.) that make otherwise-identical article URLs
        look different. Since the next module (Duplicate Detector) will
        likely use the URL as one signal for identifying duplicates,
        canonicalizing it here prevents false negatives there.
        """
        if not url:
            return ""
        parts = urlsplit(url)
        # parse_qsl preserves parameter order and handles repeated keys;
        # we drop anything matching our known tracking-param set.
        filtered_params = [
            (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in self._TRACKING_PARAMS
        ]
        clean_query = urlencode(filtered_params)
        # Fragment (#...) is intentionally dropped: it never affects
        # which article a URL points to and is a common source of
        # spurious "different URL, same article" mismatches.
        return urlunsplit((parts.scheme, parts.netloc, parts.path, clean_query, ""))

    def is_valid(self, article: Dict[str, Any]) -> bool:
        """
        Decide whether a (post-cleaning) article carries enough real
        content to be worth keeping.

        Current rule (intentionally simple for v1): the title must exist
        and contain at least MIN_TITLE_WORDS words. More sophisticated
        quality checks (summary length, language detection) can be
        layered on later without changing this method's contract.
        """
        title = article.get("title", "")
        if not title:
            return False
        return len(title.split()) >= self.MIN_TITLE_WORDS

    def clean_article(self, article: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Clean a single article dictionary.

        Returns:
            A NEW dict with cleaned fields, or None if the article
            should be discarded entirely (no usable content). Returning
            None (instead of raising) lets clean_batch simply filter
            out the Nones with a list comprehension — no try/except
            needed at the call site.

        DESIGN DECISION: we build a new dict rather than mutating the
        input in place. This keeps the method side-effect-free and
        leaves the original `all_news` list intact for debugging/audit
        purposes (e.g. comparing raw vs. cleaned side by side).
        """
        cleaned = dict(article)  # shallow copy — never mutate the caller's dict
        cleaned["title"] = self.clean_text(article.get("title", ""))
        cleaned["summary"] = self.clean_text(article.get("summary", ""))
        cleaned["url"] = self.clean_url(article.get("url", ""))

        if not self.is_valid(cleaned):
            logger.info(
                "Discarding low-content article from '%s': %r",
                article.get("source", "?"), cleaned.get("title", ""),
            )
            return None

        return cleaned

    def clean_batch(self, all_news: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Clean an entire batch of articles (the `all_news` list produced
        by any Collector).

        Returns:
            A new list containing only the cleaned, valid articles.
            Invalid/empty articles are silently dropped from the result
            (each drop is still logged individually by clean_article,
            for traceability).
        """
        cleaned_news: List[Dict[str, Any]] = []
        discarded = 0

        for article in all_news:
            cleaned = self.clean_article(article)
            if cleaned is not None:
                cleaned_news.append(cleaned)
            else:
                discarded += 1

        logger.info(
            "News Cleaner: %d cleaned, %d discarded (of %d total)",
            len(cleaned_news), discarded, len(all_news),
        )
        return cleaned_news
