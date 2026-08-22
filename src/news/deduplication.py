"""
src/news/deduplication.py
------------------------------
Multi-level article deduplication (Phase 2, spec §7).

STRATEGY — cheapest and most certain first, most expensive and least
certain last. Each level is only reached if every cheaper level missed:

    LEVEL 1  provider_article_id      exact, free, certain
    LEVEL 2  canonical URL            exact after normalization, free
    LEVEL 3  title + source + day     exact after normalization, free
    LEVEL 4  content similarity       computed, cheap-but-not-free

DELIBERATE NON-USE OF LLMs (spec §19): no level here calls a model.
Levels 1-3 are pure hash/string equality. Level 4 uses token-set
Jaccard similarity over the normalized title+summary — O(n) per
comparison against a bounded candidate set (only articles from the
same day are ever compared), not an embedding lookup and not an API
call. At 100k articles/day this stays tractable; a semantic/vector
approach would be the natural upgrade IF Jaccard proves insufficient
in practice, and is deliberately not pre-built here.

TRADE-OFF, stated plainly: Jaccard on title+summary catches syndicated
copies and light rewrites well. It will NOT catch a genuine rewrite
that shares little vocabulary, and it CAN in principle flag two
different articles that share heavy boilerplate. The threshold
(default 0.85) is deliberately conservative — biased toward missing a
duplicate rather than wrongly merging two distinct stories, since a
wrong merge loses information irrecoverably while a missed duplicate
merely leaves a redundant row.
"""

import re
import hashlib
import logging
from typing import Optional, List, Set, Tuple
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from src.domain.news_models import NormalizedArticle, DuplicateMatchLevel

logger = logging.getLogger("marketlens.news.deduplication")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


# Tracking parameters that change per-visit but never identify a
# different article — stripped before URL comparison so the same story
# shared via two campaigns isn't counted twice.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "ref_src", "src", "cmpid", "smid",
}


def canonicalize_url(url: Optional[str]) -> Optional[str]:
    """
    Normalize a URL for comparison: lowercase scheme/host, drop 'www.',
    strip tracking parameters, drop fragments, remove a trailing slash.
    Returns None for a missing/unparseable URL rather than raising.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if not parsed.netloc:
        return None

    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    kept_params = [(k, v) for k, v in parse_qsl(parsed.query) if k.lower() not in _TRACKING_PARAMS]
    path = parsed.path.rstrip("/") or "/"

    return urlunparse((parsed.scheme.lower() or "https", host, path, "", urlencode(kept_params), ""))


def normalize_title(title: Optional[str]) -> str:
    """Lowercase, strip punctuation and collapse whitespace — so trivial formatting differences don't defeat matching."""
    if not title:
        return ""
    cleaned = re.sub(r"[^\w\s]", " ", title.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def compute_fingerprint(title: Optional[str], source_name: Optional[str], published_at) -> Optional[str]:
    """
    Level 3 key: normalized title + source + publication DAY.

    Day-level (not exact-timestamp) granularity is deliberate: the same
    story is frequently stamped minutes apart by the same outlet across
    its feeds, and an exact-timestamp key would miss those.
    """
    normalized = normalize_title(title)
    if not normalized:
        return None
    day = published_at.date().isoformat() if published_at else "no-date"
    basis = f"{normalized}|{(source_name or '').lower()}|{day}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def compute_content_fingerprint(title: Optional[str], summary: Optional[str]) -> Optional[str]:
    """Level 4 basis: a hash of the combined normalized title+summary — used for exact content matches before falling back to similarity scoring."""
    combined = f"{normalize_title(title)} {normalize_title(summary)}".strip()
    if not combined:
        return None
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def _token_set(title: Optional[str], summary: Optional[str]) -> Set[str]:
    """
    Tokenize title+summary for similarity comparison.

    SHORT-TOKEN RULE: tokens of 3+ characters are kept, AND so is any
    shorter token containing a digit. That exception matters: in
    financial headlines the short tokens are frequently the ONLY thing
    distinguishing two otherwise-identical stories — "Q2" vs "Q3", "up
    5%" vs "up 8%", "FY24" vs "FY25". Dropping them (the naive
    stopword-length approach) would make genuinely different quarterly
    results look like duplicates of each other.
    """
    text = f"{normalize_title(title)} {normalize_title(summary)}"
    return {t for t in text.split() if len(t) > 2 or any(ch.isdigit() for ch in t)}


def jaccard_similarity(a: NormalizedArticle, b: NormalizedArticle) -> float:
    """
    Token-set Jaccard similarity over title+summary, in [0.0, 1.0].
    Returns 0.0 when either side has no usable tokens (never raises,
    never claims similarity it cannot support).
    """
    tokens_a, tokens_b = _token_set(a.title, a.summary), _token_set(b.title, b.summary)
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    return intersection / union if union else 0.0


class DeduplicationEngine:
    """
    Decides whether an incoming article duplicates one already known,
    and if so, at which level it matched.
    """

    def __init__(self, similarity_threshold: float = 0.85):
        """
        Args:
            similarity_threshold: minimum Jaccard score for a Level 4
                match. Default 0.85 — deliberately conservative (see
                the module docstring's stated trade-off).
        """
        self.similarity_threshold = similarity_threshold

    def find_duplicate(
        self, article: NormalizedArticle, candidates: List[NormalizedArticle]
    ) -> Tuple[Optional[NormalizedArticle], DuplicateMatchLevel]:
        """
        Check `article` against a bounded candidate set (the caller is
        responsible for keeping that set small — see
        NewsRepository.find_dedup_candidates, which restricts it to the
        same publication day).

        Returns:
            (matched_article, level) — or (None, DuplicateMatchLevel.NONE)
            if no level matched. An article never matches itself.
        """
        others = [c for c in candidates if c.article_id != article.article_id]

        # LEVEL 1 — same provider, same provider-side id.
        if article.provider_article_id:
            for candidate in others:
                if (candidate.provider == article.provider
                        and candidate.provider_article_id == article.provider_article_id):
                    return candidate, DuplicateMatchLevel.PROVIDER_ID

        # LEVEL 2 — same canonical URL (works ACROSS providers too:
        # two providers syndicating one story usually share its URL).
        article_url = article.canonical_url or canonicalize_url(article.source_url)
        if article_url:
            for candidate in others:
                candidate_url = candidate.canonical_url or canonicalize_url(candidate.source_url)
                if candidate_url and candidate_url == article_url:
                    return candidate, DuplicateMatchLevel.CANONICAL_URL

        # LEVEL 3 — same normalized title + source + day.
        fingerprint = article.fingerprint or compute_fingerprint(article.title, article.source_name, article.published_at)
        if fingerprint:
            for candidate in others:
                candidate_fp = candidate.fingerprint or compute_fingerprint(
                    candidate.title, candidate.source_name, candidate.published_at
                )
                if candidate_fp and candidate_fp == fingerprint:
                    return candidate, DuplicateMatchLevel.TITLE_SOURCE_TIME

        # LEVEL 4 — content similarity (exact content hash first, then Jaccard).
        content_fp = article.content_fingerprint or compute_content_fingerprint(article.title, article.summary)
        if content_fp:
            for candidate in others:
                candidate_cfp = candidate.content_fingerprint or compute_content_fingerprint(candidate.title, candidate.summary)
                if candidate_cfp and candidate_cfp == content_fp:
                    return candidate, DuplicateMatchLevel.CONTENT_SIMILARITY

        for candidate in others:
            if jaccard_similarity(article, candidate) >= self.similarity_threshold:
                return candidate, DuplicateMatchLevel.CONTENT_SIMILARITY

        return None, DuplicateMatchLevel.NONE

    def mark_if_duplicate(
        self, article: NormalizedArticle, candidates: List[NormalizedArticle]
    ) -> NormalizedArticle:
        """
        Convenience wrapper: run find_duplicate() and, on a match, set
        `duplicate_of` / `duplicate_match_level` on the article in
        place. The duplicate is NEVER discarded — it is kept, flagged,
        and pointed at its canonical original (spec §23: never silently
        discard data).
        """
        match, level = self.find_duplicate(article, candidates)
        if match:
            # Point at the ORIGINAL, not at another duplicate — keeps
            # duplicate chains one level deep and always resolvable.
            article.duplicate_of = match.duplicate_of or match.article_id
            article.duplicate_match_level = level
            logger.debug("Duplicate detected (%s): %s -> %s", level.value, article.article_id, article.duplicate_of)
        return article
