"""
sentiment_engine.py
-----------------------
Sentiment Engine module for MarketLens.

RESPONSIBILITY:
Score the financial/market tone of each article — positive, negative,
or neutral — using a curated lexicon of finance-specific words/phrases
(sentiment_lexicon.py), rather than a generic sentiment model. This
matters because generic sentiment analysis performs poorly on
financial text: a word can carry a completely different charge in a
market context than in everyday language.

OUTPUT SHAPE per article, under the `sentiment` key:
    {
        "score": float in [-1.0, 1.0],   # overall tone
        "label": "positive" | "negative" | "neutral",
        "matched_positive": [...],        # positive words/phrases found
        "matched_negative": [...],        # negative words/phrases found
        "confidence": float in [0.0, 1.0] # how much signal was found
    }

KNOWN v1 LIMITATION (documented, not hidden): this module does NOT
handle negation. "Shares did NOT rise" is still scored as positive,
because "rise" is matched with no awareness of the preceding "not".
Negation handling is a reasonable improvement for a future version —
left out here to keep this first version simple, correct, and fully
tested for exactly what it claims to do.
"""

import re
import logging
from typing import List, Dict, Any, Optional, Tuple

from sentiment_lexicon import POSITIVE_WORDS, NEGATIVE_WORDS

logger = logging.getLogger("marketlens.sentiment_engine")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class SentimentEngine:
    """
    Scores article text for financial sentiment using a word/phrase
    lexicon (positive vs. negative), with no reliance on external ML
    models — deterministic, fast, and fully unit-testable.
    """

    # A score at or above this (in absolute value) is labeled non-
    # neutral. Below this threshold, near-even pos/neg counts are
    # treated as "neutral" rather than forcing a label from noise.
    _NEUTRAL_THRESHOLD = 0.2

    # Number of matched sentiment words at which confidence saturates
    # to 1.0. WHY 5: a single matched word could be incidental (e.g. one
    # stray positive term in an otherwise neutral article); five or more
    # independent matches is a much stronger basis for confidence. This
    # is a deliberately simple, documented heuristic for v1.
    _CONFIDENCE_SATURATION_COUNT = 5

    def __init__(
        self,
        positive_words: Optional[List[str]] = None,
        negative_words: Optional[List[str]] = None,
    ):
        """
        Args:
            positive_words / negative_words: lexicon overrides, mainly
                for isolated unit testing. Default to the built-in
                POSITIVE_WORDS / NEGATIVE_WORDS.
        """
        self.positive_words = positive_words if positive_words is not None else POSITIVE_WORDS
        self.negative_words = negative_words if negative_words is not None else NEGATIVE_WORDS

        # Precompile one word-boundary regex per lexicon entry, once, at
        # construction time — this module runs over thousands of
        # articles, so avoiding repeated regex compilation matters.
        # Case-INSENSITIVE: these are ordinary words/phrases (not short
        # acronyms), so there's no meaningful collision risk from
        # ignoring case, unlike Ticker Detector's short symbols.
        self._positive_patterns: List[Tuple[str, "re.Pattern"]] = [
            (word, re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE))
            for word in self.positive_words
        ]
        self._negative_patterns: List[Tuple[str, "re.Pattern"]] = [
            (word, re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE))
            for word in self.negative_words
        ]

    def _find_matches(self, text: str, patterns: List[Tuple[str, "re.Pattern"]]) -> List[str]:
        """
        Return every DISTINCT word/phrase (from the given pattern list)
        found in the text, preserving lexicon order.
        """
        return [word for word, pattern in patterns if pattern.search(text)]

    def analyze_text(self, text: str) -> Dict[str, Any]:
        """
        Score a block of text for financial sentiment.

        Returns:
            A dict with "score", "label", "matched_positive",
            "matched_negative", and "confidence" — see module docstring
            for the exact shape.
        """
        if not text:
            return {
                "score": 0.0, "label": "neutral",
                "matched_positive": [], "matched_negative": [],
                "confidence": 0.0,
            }

        matched_positive = self._find_matches(text, self._positive_patterns)
        matched_negative = self._find_matches(text, self._negative_patterns)

        total_matches = len(matched_positive) + len(matched_negative)
        if total_matches == 0:
            score = 0.0
        else:
            # Normalized to [-1, 1]: +1 means every matched signal was
            # positive, -1 means every one was negative, 0 means an
            # even split — independent of HOW MANY words were matched,
            # which is instead what `confidence` communicates.
            score = (len(matched_positive) - len(matched_negative)) / total_matches

        if score > self._NEUTRAL_THRESHOLD:
            label = "positive"
        elif score < -self._NEUTRAL_THRESHOLD:
            label = "negative"
        else:
            label = "neutral"

        confidence = min(1.0, total_matches / self._CONFIDENCE_SATURATION_COUNT)

        return {
            "score": round(score, 3),
            "label": label,
            "matched_positive": matched_positive,
            "matched_negative": matched_negative,
            "confidence": round(confidence, 3),
        }

    def analyze_article(self, article: Dict[str, Any]) -> Dict[str, Any]:
        """
        Score one article's title + summary and return a NEW article
        dict tagged with `sentiment`.

        Follows the same copy-don't-mutate discipline as every other
        module in this pipeline.
        """
        combined_text = f"{article.get('title', '')} {article.get('summary', '')}"
        sentiment = self.analyze_text(combined_text)

        tagged = dict(article)
        tagged["sentiment"] = sentiment
        return tagged

    def analyze_batch(self, articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Run sentiment scoring over an entire batch of articles.

        Returns:
            A new list, same order, every article tagged with
            `sentiment`.
        """
        tagged_articles = [self.analyze_article(article) for article in articles]

        label_counts: Dict[str, int] = {"positive": 0, "negative": 0, "neutral": 0}
        for article in tagged_articles:
            label_counts[article["sentiment"]["label"]] += 1

        logger.info(
            "Sentiment Engine: %d positive, %d negative, %d neutral (of %d total)",
            label_counts["positive"], label_counts["negative"], label_counts["neutral"],
            len(tagged_articles),
        )
        return tagged_articles
