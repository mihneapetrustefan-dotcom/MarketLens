"""
event_detector.py
---------------------
Event Detector module for MarketLens.

RESPONSIBILITY:
Classify each article by EVENT TYPE (earnings, acquisition, CEO
change, lawsuit, layoffs, etc. — see event_lexicon.py) — independent
of and complementary to Sentiment Engine. Sentiment answers "is this
good or bad news?"; Event Detector answers "what KIND of thing
happened?". An article can match zero, one, or several event types
(e.g. "CEO resigns amid shareholder lawsuit" is both CEO_CHANGE and
LAWSUIT).

DESIGN — mirrors Sentiment Engine's matching approach:
Case-insensitive phrase matching with word boundaries (via regex),
over the article's title + summary — the exact same mechanism already
proven in sentiment_engine.py, applied to a different lexicon.
"""

import re
import logging
from typing import List, Dict, Any, Optional

from event_lexicon import EVENT_LEXICON

logger = logging.getLogger("marketlens.event_detector")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class EventDetector:
    """
    Classifies articles by event type using case-insensitive,
    word-boundary phrase matching against a configurable lexicon.
    """

    def __init__(self, lexicon: Optional[Dict[str, List[str]]] = None):
        """
        Args:
            lexicon: event_type -> list of trigger phrases. Defaults to
                EVENT_LEXICON. Injectable so tests can use a small,
                controlled lexicon instead of the full real one.
        """
        self.lexicon = lexicon if lexicon is not None else EVENT_LEXICON
        self._compiled = {
            event_type: [re.compile(r"\b" + re.escape(phrase) + r"\b", re.IGNORECASE) for phrase in phrases]
            for event_type, phrases in self.lexicon.items()
        }

    def detect_in_text(self, text: str) -> List[Dict[str, Any]]:
        """
        Detect every event type that matches somewhere in the given
        text.

        Returns:
            A list of {"event_type", "matched_phrase"} dicts — one
            entry per MATCHING event type (at most one match reported
            per type, even if multiple phrases for that type match).
            Empty list if nothing matches — most routine articles
            aren't about any specific corporate event, and that's a
            legitimate, common result, not a failure.
        """
        if not text:
            return []

        matches = []
        for event_type, patterns in self._compiled.items():
            for pattern in patterns:
                found = pattern.search(text)
                if found:
                    matches.append({"event_type": event_type, "matched_phrase": found.group(0)})
                    break  # one match is enough evidence for this event type
        return matches

    def detect_in_article(self, article: Dict[str, Any]) -> Dict[str, Any]:
        """
        Detect event types for one article (using its title + summary)
        and return a NEW dict with an added "events" field — never
        mutates the input, consistent with every other detector in
        this project.
        """
        text = f"{article.get('title', '')} {article.get('summary', '')}"
        events = self.detect_in_text(text)
        return {**article, "events": events}

    def detect_batch(self, articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Detect event types for a whole batch of articles."""
        tagged = [self.detect_in_article(a) for a in articles]

        with_events = sum(1 for a in tagged if a["events"])
        logger.info(
            "Event Detector: %d of %d articles classified into at least one event type",
            with_events, len(tagged),
        )
        return tagged
