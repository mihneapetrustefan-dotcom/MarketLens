"""
src/data_access/article_archive.py
-----------------------------------------------
Read-only access to the compressed article archive written by
`scripts/archive_old_articles.py`.

WHY THIS EXISTS
---------------
`archive_old_articles.py` moves articles older than its retention
window out of the live `articles` table into
`data/archives/articles_YYYY-MM.jsonl.gz`. Its own docstring states the
reason it archives instead of deleting:

    this project's Phase 7 (Historical Event Studies & Research
    Dataset) needs exactly the old articles this script would
    otherwise remove.

The rows were indeed kept. Nothing read them back. Every downstream
stage — `populate_events.py`, `compute_features.py` — queried
`FROM articles`, the live table only, so an archived article was
indistinguishable from an article that never existed. Measured
consequence (docs/MODEL_CLUSTER_INSUFFICIENCY_AUDIT_2026_11_01.md):
seven article- and event-derived features fell from ~100% coverage to
8.6-10.3%, and the design matrix lost every complete-case row.

This module is the missing reader. It answers two questions and
nothing else:

    contains(article_id)  -> was this article archived, or is it gone?
    fetch(article_ids)    -> the few fields features need, for those.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not write, move or delete anything, and it never touches the
database. It does not reconstruct archived rows into `articles` —
re-importing them would undo the size control that archiving exists to
provide. And it never invents a value: an article with no parseable
sentiment yields `sentiment_score=None`, never 0.0. A real stored
`{"score": 0.0}` yields 0.0, because a measured zero and an absent
measurement are different facts.

MEMORY: AN INDEX, NOT THE ARCHIVE
---------------------------------
Files are read line by line through `gzip.open(..., "rt")` and never
loaded whole. `ArchivedArticle` keeps four scalars per row —
`article_id`, `published_at`, `source`, `sentiment_score` — and drops
`title`, `summary`, `url`, `impact` and the rest on the floor. So the
resident cost is proportional to the number of article ids actually
asked for, times four small values, not to the 24 MB of compressed
records on disk.

`fetch` takes the whole set of wanted ids and makes ONE pass. Calling
it once per instrument would re-stream the archive once per
instrument; `scripts/compute_features.py` therefore resolves every
instrument's archived articles in a single call.

DUPLICATES ARE RESOLVED DETERMINISTICALLY
-----------------------------------------
The writer opens each month's file in APPEND mode, and its own comment
records an incident in which one article ended up stored seven times.
So duplicate `article_id`s are expected, and two copies can disagree
(a row re-archived after its sentiment was recomputed).

The rule is FIRST WINS, over files sorted by name and lines read in
file order. It is not "best" or "newest" — it is *stable*: the same
archive directory always yields the same answer, which is what
idempotent feature recomputation requires. A later duplicate is
counted in `duplicate_records` and otherwise ignored, so the
disagreement is visible rather than silently resolved.

PARTIAL FAILURE IS NORMAL, NOT FATAL
------------------------------------
A missing archive directory, a file that is not valid gzip, a file
truncated mid-stream, a line that is not JSON, a record with no
`article_id` — none of these raise. Each is counted and skipped, and
whatever was readable before the failure is kept. A feature pipeline
must not die because one archive file was interrupted on its way to
disk; it must compute what it can and report what it could not.
"""

from __future__ import annotations

import glob
import gzip
import json
import os
import zlib
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional

#: Filenames the writer produces: `articles_YYYY-MM.jsonl.gz`.
ARCHIVE_GLOB = "articles_*.jsonl.gz"

#: Everything a damaged archive file can raise on open or mid-stream.
#: `gzip.BadGzipFile` subclasses OSError (bad header, wrong magic);
#: `zlib.error` does NOT, and surfaces from a corrupt deflate stream;
#: `EOFError` is a file truncated mid-member; `UnicodeDecodeError` is
#: bytes that decompressed but are not UTF-8.
_UNREADABLE = (OSError, EOFError, UnicodeDecodeError, zlib.error)


@dataclass(frozen=True)
class ArchivedArticle:
    """
    One archived article, reduced to the fields features consume.

    `published_at` stays a raw string exactly as the live `articles`
    column stores it, so callers parse it with the same code path they
    already use for live rows rather than a second, divergent one.
    """
    article_id: str
    published_at: Optional[str]
    source: Optional[str]
    sentiment_score: Optional[float]


@dataclass
class ArchiveStats:
    """What a scan actually managed to read. Reported, never asserted."""
    files_seen: int = 0
    files_read: int = 0
    files_unreadable: List[str] = field(default_factory=list)
    lines_read: int = 0
    records_kept: int = 0
    malformed_lines: int = 0
    records_without_id: int = 0
    duplicate_records: int = 0

    def summary(self) -> str:
        parts = [f"{self.files_read}/{self.files_seen} fisiere",
                 f"{self.records_kept:,} articole"]
        if self.duplicate_records:
            parts.append(f"{self.duplicate_records:,} duplicate ignorate")
        if self.malformed_lines:
            parts.append(f"{self.malformed_lines:,} linii corupte")
        if self.records_without_id:
            parts.append(f"{self.records_without_id:,} fara id")
        if self.files_unreadable:
            parts.append(f"{len(self.files_unreadable)} fisiere necitibile")
        return ", ".join(parts)


def _sentiment_score(raw: Optional[str]) -> Optional[float]:
    """
    Pull the numeric score out of the stored sentiment blob.

    Mirrors `scripts/compute_features.load_entity_articles` exactly,
    including its tolerance of legacy rows that predate schema
    validation. Returns None when there is no score to report — and
    returns 0.0 when the stored score IS zero.
    """
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    score = parsed.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    return float(score)


def archive_dir_for(db_path: str) -> str:
    """
    Where this database's archives live.

    Identical rule to `scripts/archive_old_articles.archive_dir_for`:
    a sibling `archives/` directory beside the database file. Restated
    here rather than imported so that `src/` does not depend on
    `scripts/`; the two are tested against each other.
    """
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "archives")


class ArticleArchive:
    """
    The archived articles for one database, read lazily and at most
    once per question.

    Construct it from a database path with `for_database`, which is
    what every caller does — the archive directory is derived from the
    database, so a rehearsal against a throwaway copy reads that copy's
    archives and not the repository's.
    """

    def __init__(self, archive_dir: Optional[str]):
        self.archive_dir = archive_dir
        self.stats = ArchiveStats()
        self._ids: Optional[FrozenSet[str]] = None

    @classmethod
    def for_database(cls, db_path: str) -> "ArticleArchive":
        return cls(archive_dir_for(db_path))

    # ---------------------------------------------------------------- files

    def files(self) -> List[str]:
        """
        Archive files, sorted by name — which is chronological, because
        the names carry `YYYY-MM`. The sort is what makes "first wins"
        on duplicates deterministic rather than filesystem-dependent.
        """
        if not self.archive_dir or not os.path.isdir(self.archive_dir):
            return []
        return sorted(glob.glob(os.path.join(self.archive_dir, ARCHIVE_GLOB)))

    @property
    def available(self) -> bool:
        """True when there is at least one archive file to read."""
        return bool(self.files())

    # ------------------------------------------------------------ streaming

    def _stream(self):
        """
        Yield `(article_id, record)` for every readable archive line.

        Never raises. A file that cannot be opened or that fails
        mid-stream is recorded in `stats.files_unreadable` and
        abandoned at the point of failure — the lines already yielded
        from it stay valid and are kept.
        """
        files = self.files()
        self.stats.files_seen = len(files)
        for path in files:
            try:
                with gzip.open(path, "rt", encoding="utf-8") as handle:
                    for line in handle:
                        self.stats.lines_read += 1
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except (ValueError, TypeError):
                            self.stats.malformed_lines += 1
                            continue
                        if not isinstance(record, dict):
                            self.stats.malformed_lines += 1
                            continue
                        article_id = record.get("article_id")
                        if not isinstance(article_id, str) or not article_id:
                            self.stats.records_without_id += 1
                            continue
                        yield article_id, record
            except _UNREADABLE:
                # Truncated gzip, wrong magic bytes, unreadable file,
                # or bytes that are not UTF-8. Whatever was already
                # yielded from this file stays valid and is kept; the
                # rest of the file is abandoned.
                self.stats.files_unreadable.append(os.path.basename(path))
                continue
            self.stats.files_read += 1

    # ------------------------------------------------------------- queries

    def article_ids(self) -> FrozenSet[str]:
        """
        Every archived article id.

        One streaming pass, ids only — no record is retained. Cached,
        because the stale-event sweeps ask `contains` once per
        candidate and a pass per candidate would be absurd.
        """
        if self._ids is None:
            seen = set()
            for article_id, _record in self._stream():
                if article_id in seen:
                    self.stats.duplicate_records += 1
                    continue
                seen.add(article_id)
                self.stats.records_kept += 1
            self._ids = frozenset(seen)
        return self._ids

    def contains(self, article_id: Optional[str]) -> bool:
        """Was this article archived? False for None and for unknown ids."""
        if not article_id:
            return False
        return article_id in self.article_ids()

    def fetch(self, wanted: Iterable[str]) -> Dict[str, ArchivedArticle]:
        """
        The archived rows for `wanted`, as `{article_id: ArchivedArticle}`.

        ONE pass over the archive regardless of how many ids are asked
        for, and only the wanted ids are retained. Ids with no archived
        record are simply absent from the result — never present with
        invented values.

        On duplicates the FIRST record encountered wins (see the module
        docstring); later ones increment `stats.duplicate_records`.
        """
        wanted_set = {a for a in wanted if a}
        found: Dict[str, ArchivedArticle] = {}
        if not wanted_set:
            return found
        for article_id, record in self._stream():
            if article_id not in wanted_set:
                continue
            if article_id in found:
                self.stats.duplicate_records += 1
                continue
            published_at = record.get("published_at")
            source = record.get("source")
            found[article_id] = ArchivedArticle(
                article_id=article_id,
                published_at=published_at if isinstance(published_at, str) else None,
                source=source if isinstance(source, str) else None,
                sentiment_score=_sentiment_score(record.get("sentiment")),
            )
            self.stats.records_kept += 1
        return found
