"""
src/news/normalizer.py
---------------------------
RawArticle -> NormalizedArticle (Phase 2, spec §3, §4, §5).

RESPONSIBILITY:
The single place where a provider's own response shape is translated
into the internal canonical article. Nothing downstream ever sees a
provider-specific field — that is the whole point of this boundary.

VALIDATION (spec: "do not silently accept corrupt data"): an article
missing the minimum viable fields is NOT dropped and NOT quietly
accepted — it is normalized with processing_status=REJECTED and a
stated rejection_reason, so it stays inspectable and recoverable.
"""

import re
import uuid
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, Dict, Any

from src.domain.news_models import (
    RawArticle, NormalizedArticle, ProcessingStatus,
)
from src.news.deduplication import (
    canonicalize_url, compute_fingerprint, compute_content_fingerprint,
)

logger = logging.getLogger("marketlens.news.normalizer")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


def parse_timestamp(value: Any) -> Optional[datetime]:
    """
    Parse a provider timestamp into timezone-aware UTC, never raising.

    Accepts ISO-8601 (with or without 'Z'), and unix epoch seconds
    (int/float — the shape Finnhub uses). A naive datetime is treated
    as UTC, since every provider MarketLens currently uses publishes in
    UTC; that assumption is stated here rather than hidden, because a
    future provider publishing local time would need explicit handling.
    Returns None for anything unparseable.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


class ArticleNormalizer:
    """
    Converts RawArticle -> NormalizedArticle.

    Provider-specific FIELD NAMES are declared as data (see
    _FIELD_MAP), not as branching logic scattered through the codebase
    — adding a provider means adding a mapping entry, not an `if
    provider == ...` somewhere new (spec §5).
    """

    # provider -> canonical field -> the provider's own key(s), tried in order.
    _FIELD_MAP: Dict[str, Dict[str, List[str]]] = {
        "rss": {
            "title": ["title"],
            "summary": ["summary", "description"],
            "url": ["url", "link"],
            "published": ["published_at", "published", "pubDate"],
            "source_name": ["source", "source_name"],
            "author": ["author"],
            "language": ["language"],
        },
        "finnhub": {
            "title": ["headline", "title"],
            "summary": ["summary"],
            "url": ["url"],
            "published": ["datetime", "published_at"],
            "source_name": ["source"],
            "author": ["author"],
            "language": ["language"],
        },
        "alpha_vantage": {
            "title": ["title"],
            "summary": ["summary"],
            "url": ["url"],
            "published": ["time_published", "published_at"],
            "source_name": ["source"],
            "author": ["authors", "author"],
            "language": ["language"],
        },
    }

    # Fallback used for any provider without an explicit mapping — the
    # generic shape the existing pipeline already produces.
    _DEFAULT_MAP = _FIELD_MAP["rss"]

    def __init__(self, min_title_length: int = 3):
        """
        Args:
            min_title_length: an article whose title is shorter than
                this is rejected as unusable. Default 3 — low enough
                to keep terse real headlines, high enough to catch
                empty/garbage rows.
        """
        self.min_title_length = min_title_length

    def _pick(self, payload: Dict[str, Any], provider: str, canonical_field: str) -> Any:
        """
        Resolve one canonical field from a provider payload.

        Tries the provider's OWN declared key names first, then falls
        back to the generic key names. That fallback matters in
        practice: providers do rename response fields, and without it a
        single renamed key would silently reject an entire feed. It is
        not a licence to accept garbage — _validate() still rejects an
        article if no mapping produced the required fields.
        """
        mapping = self._FIELD_MAP.get(provider, self._DEFAULT_MAP)
        for key in mapping.get(canonical_field, []):
            if payload.get(key) not in (None, ""):
                return payload[key]
        for key in self._DEFAULT_MAP.get(canonical_field, []):
            if payload.get(key) not in (None, ""):
                return payload[key]
        return None

    def normalize(self, raw: RawArticle, source_id: Optional[str] = None) -> NormalizedArticle:
        """
        Convert one RawArticle into a NormalizedArticle.

        Args:
            raw: the provider's preserved response record.
            source_id: canonical NewsSource id (Phase 1), when the
                caller has already resolved it. Left None otherwise —
                the source NAME is always carried regardless, since
                existing code joins on it.

        Returns:
            A NormalizedArticle. Never raises: an unusable article
            comes back with status REJECTED and a rejection_reason.
        """
        payload = raw.payload or {}
        provider = raw.provider

        title = self._pick(payload, provider, "title")
        summary = self._pick(payload, provider, "summary")
        url = self._pick(payload, provider, "url")
        source_name = self._pick(payload, provider, "source_name")
        author = self._pick(payload, provider, "author")
        language = self._pick(payload, provider, "language")
        published_at = parse_timestamp(self._pick(payload, provider, "published"))

        if isinstance(author, list):
            author = ", ".join(str(a) for a in author) or None

        article = NormalizedArticle(
            article_id=self._build_article_id(raw, url, title),
            provider=provider,
            provider_article_id=raw.provider_article_id,
            raw_id=raw.raw_id,
            source_id=source_id,
            source_name=str(source_name) if source_name else None,
            source_url=str(url) if url else None,
            canonical_url=canonicalize_url(url),
            title=str(title).strip() if title else "",
            summary=str(summary).strip() if summary else None,
            language=str(language) if language else None,
            author=str(author) if author else None,
            published_at=published_at,
            ingested_at=raw.fetched_at,
            processing_status=ProcessingStatus.NORMALIZED,
        )

        rejection = self._validate(article)
        if rejection:
            article.processing_status = ProcessingStatus.REJECTED
            article.rejection_reason = rejection
            logger.debug("Article rejected (%s): %s", rejection, article.article_id)
            return article

        article.fingerprint = compute_fingerprint(article.title, article.source_name, article.published_at)
        article.content_fingerprint = compute_content_fingerprint(article.title, article.summary)
        return article

    def _validate(self, article: NormalizedArticle) -> Optional[str]:
        """Return a stated reason if the article is unusable, else None. Corrupt data is flagged, never silently accepted."""
        if not article.title or len(article.title) < self.min_title_length:
            return "missing or too-short title"
        if not article.source_url and not article.canonical_url:
            return "missing url"
        if article.published_at is None:
            return "missing or unparseable publication timestamp"
        # A publication time meaningfully in the future indicates a
        # provider clock/parse problem — flagged rather than trusted,
        # since a wrong published_at directly corrupts look-ahead-bias
        # protection later (spec §9).
        if article.ingested_at and article.published_at > article.ingested_at:
            drift_seconds = (article.published_at - article.ingested_at).total_seconds()
            if drift_seconds > 86400:
                return "publication timestamp is more than a day after ingestion (provider clock error)"
        return None

    def _build_article_id(self, raw: RawArticle, url: Any, title: Any) -> str:
        """
        Build a DETERMINISTIC internal id, so re-ingesting the same
        article produces the same id — which is what makes ingestion
        idempotent at the storage layer (spec §6).

        Preference order: provider+provider_id (most stable), then
        canonical URL, then provider+normalized title. A random UUID is
        the last resort, used only when an article has no stable
        identifying feature at all.
        """
        if raw.provider_article_id:
            basis = f"{raw.provider}:{raw.provider_article_id}"
        elif url:
            canonical = canonicalize_url(url)
            basis = f"url:{canonical}" if canonical else f"url:{url}"
        elif title:
            basis = f"{raw.provider}:title:{str(title).strip().lower()}"
        else:
            return f"art-{uuid.uuid4().hex[:16]}"
        return "art-" + __import__("hashlib").sha256(basis.encode("utf-8")).hexdigest()[:20]

    def normalize_batch(self, raws: List[RawArticle], source_id_by_name: Optional[Dict[str, str]] = None) -> List[NormalizedArticle]:
        """
        Normalize a batch. `source_id_by_name` optionally resolves each
        article's source NAME to a canonical Phase 1 NewsSource id.
        """
        source_id_by_name = source_id_by_name or {}
        results = []
        for raw in raws:
            article = self.normalize(raw)
            if article.source_name and not article.source_id:
                article.source_id = source_id_by_name.get(article.source_name)
            results.append(article)
        return results
