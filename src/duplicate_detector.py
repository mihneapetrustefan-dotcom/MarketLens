"""
duplicate_detector.py
-----------------------
Duplicate Detector module for MarketLens.

RESPONSIBILITY:
Given a list of cleaned articles (the output of News Cleaner), this
module handles TWO distinct problems, on purpose:

1. EXACT duplicates — the same article appearing twice in the batch
   (e.g. an RSS entry re-collected across two polling runs). These are
   copies of ONE underlying source, not independent confirmation, so
   only the first occurrence is kept; the rest are discarded.

2. NEAR-duplicate CLUSTERS — different articles, from DIFFERENT
   sources, describing the SAME underlying event (e.g. "Fed raises
   rates" reported independently by Reuters, CNBC, and MarketWatch).
   These are NOT merged or deleted: every article in such a cluster is
   tagged with a shared `duplicate_group_id` and `duplicate_group_size`.
   This distinction matters because the future Recommendation Engine
   must never base a recommendation on a single article — it needs to
   know exactly how many INDEPENDENT sources are confirming the same
   story, and that count is exactly `duplicate_group_size`.

This module does NOT do sentiment analysis, company/ticker detection,
or scoring — only identifies which articles are copies of each other
and which independently corroborate the same event.
"""

import re
import uuid
import logging
from itertools import combinations
from typing import List, Dict, Any, Set

logger = logging.getLogger("marketlens.duplicate_detector")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class DisjointSet:
    """
    Union-Find (Disjoint Set Union) data structure with path compression
    and union by rank.

    WHY THIS EXISTS: near-duplicate detection is fundamentally a graph
    connectivity problem — if article A is similar to B, and B is
    similar to C, then A, B, and C all belong in the SAME group, even
    if A and C were never directly compared as "similar enough" to each
    other. A plain "compare every pair, merge if similar" approach
    without this structure would miss that transitive chain. DSU solves
    exactly this, in near-linear time.
    """

    def __init__(self, size: int):
        # Each element starts as its own parent (its own group of one).
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, x: int) -> int:
        """Find the representative (root) of x's group, compressing the
        path along the way so future lookups are faster."""
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])  # path compression
        return self.parent[x]

    def union(self, a: int, b: int) -> None:
        """Merge the groups containing a and b into one group."""
        root_a, root_b = self.find(a), self.find(b)
        if root_a == root_b:
            return  # already in the same group
        # Union by rank: attach the smaller tree under the larger one,
        # keeping the overall structure shallow (faster future finds).
        if self.rank[root_a] < self.rank[root_b]:
            root_a, root_b = root_b, root_a
        self.parent[root_b] = root_a
        if self.rank[root_a] == self.rank[root_b]:
            self.rank[root_a] += 1


class DuplicateDetector:
    """
    Detects exact duplicate articles and clusters near-duplicate
    articles (same event, different sources) using title similarity.
    """

    # Matches words made of letters/digits, including Romanian
    # diacritics — needed since headlines can be in Romanian or English.
    _WORD_RE = re.compile(r"[a-zA-Z0-9ăâîșțĂÂÎȘȚ]+")

    # Common English + Romanian filler words. Removing these before
    # comparing titles prevents two UNRELATED headlines from looking
    # falsely similar just because they share words like "the"/"și"/"la".
    _STOPWORDS = {
        # English
        "the", "a", "an", "in", "on", "at", "of", "to", "for", "and", "or",
        "is", "are", "was", "were", "with", "from", "by", "as", "that",
        "this", "it", "be", "has", "have", "will", "after", "over", "into",
        # Romanian
        "si", "la", "in", "pe", "cu", "de", "un", "o", "sa", "se", "ce",
        "din", "pentru", "dupa", "este", "sunt", "a", "ai", "al", "ale",
        "cel", "cea", "care", "mai", "au", "va", "fi",
    }

    # Words shorter than this are dropped along with stopwords — very
    # short tokens (e.g. leftover single letters) rarely carry meaning
    # and mostly just add noise to the similarity calculation.
    _MIN_WORD_LENGTH = 3

    def __init__(self, similarity_threshold: float = 0.5, same_source_duplicate_threshold: float = 0.75):
        """
        Args:
            similarity_threshold: minimum Jaccard similarity for two
                articles from DIFFERENT sources to be considered
                independent corroboration of the same event (see
                group_near_duplicates). 0.5 is a deliberately moderate
                default.
            same_source_duplicate_threshold: minimum Jaccard similarity
                for two articles from the SAME source to be treated as
                a republished near-duplicate of one story (see
                collapse_same_source_near_duplicates). This is set
                stricter than similarity_threshold on purpose: within
                one source we only want to catch near-IDENTICAL titles
                (e.g. a "FOTO ..." story re-published minutes later as
                "VIDEO&FOTO ..."), not merely related articles — a
                single outlet's own related coverage of a broader theme
                is normal editorial output, not duplication.
        """
        self.similarity_threshold = similarity_threshold
        self.same_source_duplicate_threshold = same_source_duplicate_threshold

    def _tokenize(self, title: str) -> Set[str]:
        """
        Convert a title into a set of meaningful, lowercase keywords.

        WHY A SET (not a list): duplicate-detection via Jaccard
        similarity only cares whether a keyword is PRESENT in a title,
        not how many times it appears — sets are the natural
        representation and make the similarity formula below trivial.
        """
        words = self._WORD_RE.findall(title.lower())
        return {
            w for w in words
            if w not in self._STOPWORDS and len(w) >= self._MIN_WORD_LENGTH
        }

    def _jaccard_similarity(self, tokens_a: Set[str], tokens_b: Set[str]) -> float:
        """
        Compute Jaccard similarity: |intersection| / |union|.

        WHY JACCARD (vs. e.g. character-level diffing): headlines about
        the same event, written independently by different outlets,
        rarely share exact phrasing but DO tend to share the key
        nouns/verbs ("Fed", "rates", "raises"). Jaccard directly
        measures shared-keyword overlap, which is a good, cheap proxy
        for "same underlying story" without needing embeddings or a
        trained model — appropriate for a v1, dependency-free module.
        """
        if not tokens_a or not tokens_b:
            return 0.0
        intersection = tokens_a & tokens_b
        union = tokens_a | tokens_b
        return len(intersection) / len(union)

    def remove_exact_duplicates(self, articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Drop articles whose (cleaned) URL has already been seen earlier
        in the list, keeping only the first occurrence of each URL.

        WHY URL (not title): the URL is the strongest possible signal
        that two entries are literally the SAME fetch of the SAME
        article — title text could coincidentally match without being
        the same article, but an identical canonical URL essentially
        never is a coincidence.

        Articles with no URL at all are never treated as duplicates of
        each other (an empty string would otherwise cause every
        URL-less article to collapse into the first one).
        """
        seen_urls: Set[str] = set()
        deduplicated: List[Dict[str, Any]] = []

        for article in articles:
            url = (article.get("url") or "").strip().lower()
            if url:
                if url in seen_urls:
                    logger.info("Discarding exact duplicate (repeated URL): %s", url)
                    continue
                seen_urls.add(url)
            deduplicated.append(article)

        return deduplicated

    def collapse_same_source_near_duplicates(self, articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Collapse near-identical titles published by the SAME source into
        a single kept occurrence (the first seen).

        WHY THIS EXISTS: some outlets occasionally republish the same
        story with a minor title variation — e.g. a photo report first
        published as "FOTO Stânci aruncate în aer..." and shortly after
        updated to "VIDEO&FOTO Stânci aruncate în aer...". These have
        DIFFERENT URLs (so exact-URL dedup misses them) but are clearly
        the SAME underlying article, not two pieces of coverage. Using
        `same_source_duplicate_threshold` (stricter than the cross-
        source threshold) keeps this narrowly scoped to near-identical
        titles, not merely related same-source coverage.

        Returns:
            A new list, in original order, with same-source
            near-duplicates removed (first occurrence kept).
        """
        n = len(articles)
        if n == 0:
            return []

        token_sets = [self._tokenize(article.get("title", "")) for article in articles]
        dsu = DisjointSet(n)

        for i, j in combinations(range(n), 2):
            # Only compare articles from the SAME source here — this
            # method's entire purpose is catching same-outlet republishes.
            if articles[i].get("source") != articles[j].get("source"):
                continue
            similarity = self._jaccard_similarity(token_sets[i], token_sets[j])
            if similarity >= self.same_source_duplicate_threshold:
                dsu.union(i, j)

        seen_roots: Set[int] = set()
        result: List[Dict[str, Any]] = []
        for i, article in enumerate(articles):
            root = dsu.find(i)
            if root in seen_roots:
                logger.info(
                    "Discarding same-source near-duplicate: [%s] %r",
                    article.get("source", "?"), article.get("title", ""),
                )
                continue
            seen_roots.add(root)
            result.append(article)

        return result

    def group_near_duplicates(self, articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Cluster articles describing the same underlying event and tag
        every article with its cluster's `duplicate_group_id` and
        `duplicate_group_size`.

        IMPORTANT: only articles from DIFFERENT sources are ever placed
        in the same group here. The whole point of this method is to
        measure INDEPENDENT corroboration of a story (Reuters + CNBC +
        MarketWatch all reporting the same event) — two similar
        articles from the SAME source are either a same-outlet
        republish (already handled by
        collapse_same_source_near_duplicates) or simply that outlet's
        own related coverage, neither of which represents independent
        confirmation and grouping them would inflate confidence
        incorrectly downstream (in the future Recommendation Engine).

        Returns:
            A NEW list (original list/dicts untouched — same
            copy-don't-mutate discipline as News Cleaner), in the same
            order as the input, with the two new fields added to every
            article. An article with no similar peers still gets a
            group of size 1 — that's meaningful information too (an
            uncorroborated, single-source story).
        """
        n = len(articles)
        if n == 0:
            return []

        # Precompute each title's keyword set ONCE — comparing n articles
        # pairwise means each title would otherwise be re-tokenized up to
        # (n-1) times, which is pure wasted work at scale.
        token_sets = [self._tokenize(article.get("title", "")) for article in articles]

        dsu = DisjointSet(n)
        for i, j in combinations(range(n), 2):
            # Skip same-source pairs entirely — see method docstring.
            if articles[i].get("source") == articles[j].get("source"):
                continue
            similarity = self._jaccard_similarity(token_sets[i], token_sets[j])
            if similarity >= self.similarity_threshold:
                dsu.union(i, j)

        # Collect the members of each connected component (= each group).
        groups_by_root: Dict[int, List[int]] = {}
        for i in range(n):
            root = dsu.find(i)
            groups_by_root.setdefault(root, []).append(i)

        # Assign a stable group_id + group_size to every article, then
        # rebuild the result in the ORIGINAL input order (grouping by
        # root would otherwise scramble the order, which is confusing
        # for anyone reading the output and unnecessary for correctness).
        group_id_by_index: Dict[int, str] = {}
        group_size_by_index: Dict[int, int] = {}
        for member_indices in groups_by_root.values():
            group_id = str(uuid.uuid4())
            size = len(member_indices)
            for idx in member_indices:
                group_id_by_index[idx] = group_id
                group_size_by_index[idx] = size

        result: List[Dict[str, Any]] = []
        for i, article in enumerate(articles):
            tagged = dict(article)  # shallow copy — never mutate the input
            tagged["duplicate_group_id"] = group_id_by_index[i]
            tagged["duplicate_group_size"] = group_size_by_index[i]
            result.append(tagged)

        return result

    def deduplicate(self, articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Full pipeline:
        1. Remove exact duplicates (identical URL).
        2. Collapse same-source near-duplicate republishes (different
           URL, near-identical title, same outlet).
        3. Cluster the remaining articles into cross-source
           near-duplicate (same-event) groups.

        Returns:
            A list of articles (exact + same-source dupes removed),
            each tagged with `duplicate_group_id` and
            `duplicate_group_size` reflecting ONLY independent,
            cross-source corroboration.
        """
        before = len(articles)
        no_exact_dupes = self.remove_exact_duplicates(articles)
        exact_removed = before - len(no_exact_dupes)

        no_same_source_dupes = self.collapse_same_source_near_duplicates(no_exact_dupes)
        same_source_removed = len(no_exact_dupes) - len(no_same_source_dupes)

        grouped = self.group_near_duplicates(no_same_source_dupes)
        multi_source_groups = len({a["duplicate_group_id"] for a in grouped if a["duplicate_group_size"] > 1})

        logger.info(
            "Duplicate Detector: %d exact duplicates removed, %d same-source republishes "
            "collapsed, %d articles remain, %d of them belong to a cross-source "
            "(independently corroborated) group",
            exact_removed, same_source_removed, len(grouped), multi_source_groups,
        )
        return grouped
