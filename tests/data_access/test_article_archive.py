"""
tests/data_access/test_article_archive.py
-----------------------------------------------------------
Tests for src/data_access/article_archive.py.

The archive is the only surviving copy of the articles that
`archive_old_articles.py` removed from the live table, so the reader
that brings them back has to be unexcitable: a missing directory, a
file that is not gzip, a file truncated mid-stream, a line that is not
JSON and a record with no id must each degrade to "that one is
unavailable" rather than to an exception that takes the feature run
down with it.

Two properties get the most attention. DUPLICATES: the writer appends,
and its own comment records an incident in which one article was
stored seven times, so conflicting copies are expected and the
resolution must be deterministic rather than merely plausible.
MISSING VALUES: an absent sentiment is None and a stored zero is 0.0,
because `_clean_rows` and every downstream feature treat those as
different facts.
"""

import gzip
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.article_archive import (
    ArchivedArticle, ArticleArchive, archive_dir_for,
)
from scripts.archive_old_articles import archive_dir_for as writer_archive_dir_for


def write_archive(directory, month, records, mode="at"):
    """One `articles_YYYY-MM.jsonl.gz`, in the writer's exact format."""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"articles_{month}.jsonl.gz")
    with gzip.open(path, mode, encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def record(article_id, published_at="2026-07-01T10:00:00+00:00",
           source="CNBC", score=0.25, **extra):
    """A record shaped like `dict(row)` from `SELECT * FROM articles`."""
    out = {
        "article_id": article_id,
        "url": f"http://example.test/{article_id}",
        "title": "T",
        "summary": "S",
        "source": source,
        "published_at": published_at,
        "collected_at": published_at,
        "stored_at": published_at,
        "sentiment": None if score is None else json.dumps(
            {"score": score, "label": "neutral", "confidence": 0.5}),
    }
    out.update(extra)
    return out


class ArchiveTempDir(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.archive_dir = os.path.join(self.root, "archives")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def archive(self):
        return ArticleArchive(self.archive_dir)


class TestArchiveLocation(ArchiveTempDir):
    def test_archive_dir_matches_the_writer_exactly(self):
        # The reader must look where the writer writes. If these ever
        # diverge the archive becomes invisible again, silently.
        for db in ("/x/y/marketlens.db", "data/marketlens.db", "copy.db"):
            self.assertEqual(archive_dir_for(db), writer_archive_dir_for(db))

    def test_for_database_derives_a_sibling_archives_directory(self):
        db_path = os.path.join(self.root, "marketlens.db")
        archive = ArticleArchive.for_database(db_path)
        self.assertEqual(os.path.normpath(archive.archive_dir),
                         os.path.normpath(self.archive_dir))

    def test_files_are_returned_in_sorted_order(self):
        write_archive(self.archive_dir, "2026-08", [record("b")])
        write_archive(self.archive_dir, "2026-07", [record("a")])
        names = [os.path.basename(p) for p in self.archive().files()]
        self.assertEqual(names, ["articles_2026-07.jsonl.gz", "articles_2026-08.jsonl.gz"])


class TestMissingAndEmptyArchive(ArchiveTempDir):
    def test_absent_directory_is_not_an_error(self):
        archive = self.archive()
        self.assertFalse(archive.available)
        self.assertEqual(archive.files(), [])
        self.assertEqual(archive.article_ids(), frozenset())
        self.assertFalse(archive.contains("anything"))
        self.assertEqual(archive.fetch(["anything"]), {})

    def test_empty_directory_is_not_an_error(self):
        os.makedirs(self.archive_dir)
        self.assertFalse(self.archive().available)

    def test_directory_with_unrelated_files_is_ignored(self):
        os.makedirs(self.archive_dir)
        with open(os.path.join(self.archive_dir, "README.txt"), "w") as handle:
            handle.write("not an archive")
        self.assertFalse(self.archive().available)

    def test_fetch_of_nothing_reads_nothing(self):
        write_archive(self.archive_dir, "2026-07", [record("a")])
        archive = self.archive()
        self.assertEqual(archive.fetch([]), {})
        self.assertEqual(archive.stats.lines_read, 0)


class TestReading(ArchiveTempDir):
    def test_archived_article_is_found_by_id(self):
        write_archive(self.archive_dir, "2026-07", [record("a1"), record("a2")])
        archive = self.archive()
        self.assertTrue(archive.contains("a1"))
        self.assertTrue(archive.contains("a2"))
        self.assertFalse(archive.contains("a3"))

    def test_contains_is_false_for_none_and_empty(self):
        write_archive(self.archive_dir, "2026-07", [record("a1")])
        archive = self.archive()
        self.assertFalse(archive.contains(None))
        self.assertFalse(archive.contains(""))

    def test_fetch_returns_only_the_requested_ids(self):
        write_archive(self.archive_dir, "2026-07",
                      [record("a1"), record("a2"), record("a3")])
        found = self.archive().fetch(["a1", "a3", "missing"])
        self.assertEqual(sorted(found), ["a1", "a3"])
        self.assertNotIn("missing", found)

    def test_fetch_carries_the_four_fields_features_need(self):
        write_archive(self.archive_dir, "2026-07", [
            record("a1", published_at="2026-07-04T09:30:00+00:00",
                   source="Reuters", score=-0.4)])
        found = self.archive().fetch(["a1"])["a1"]
        self.assertIsInstance(found, ArchivedArticle)
        self.assertEqual(found.article_id, "a1")
        self.assertEqual(found.published_at, "2026-07-04T09:30:00+00:00")
        self.assertEqual(found.source, "Reuters")
        self.assertAlmostEqual(found.sentiment_score, -0.4)

    def test_only_scalars_are_retained_not_the_whole_record(self):
        # The memory budget is the point: the archive is 24 MB of
        # records on disk and must not become 24 MB of objects.
        write_archive(self.archive_dir, "2026-07",
                      [record("a1", title="x" * 5000, summary="y" * 5000)])
        found = self.archive().fetch(["a1"])["a1"]
        self.assertFalse(hasattr(found, "title"))
        self.assertFalse(hasattr(found, "summary"))
        self.assertEqual(
            set(found.__dataclass_fields__),
            {"article_id", "published_at", "source", "sentiment_score"})

    def test_several_months_are_all_read(self):
        write_archive(self.archive_dir, "2026-06", [record("june")])
        write_archive(self.archive_dir, "2026-07", [record("july")])
        write_archive(self.archive_dir, "2026-08", [record("august")])
        found = self.archive().fetch(["june", "july", "august"])
        self.assertEqual(sorted(found), ["august", "july", "june"])


class TestMissingValuesStayMissing(ArchiveTempDir):
    def test_absent_sentiment_yields_none_not_zero(self):
        write_archive(self.archive_dir, "2026-07", [record("a1", score=None)])
        self.assertIsNone(self.archive().fetch(["a1"])["a1"].sentiment_score)

    def test_stored_zero_score_is_preserved_as_zero(self):
        # A measured neutral is 0.0 and must not be confused with
        # "no measurement", which is None.
        write_archive(self.archive_dir, "2026-07", [record("a1", score=0.0)])
        score = self.archive().fetch(["a1"])["a1"].sentiment_score
        self.assertIsNotNone(score)
        self.assertEqual(score, 0.0)

    def test_sentiment_without_a_score_key_yields_none(self):
        write_archive(self.archive_dir, "2026-07", [
            dict(record("a1"), sentiment=json.dumps({"label": "neutral"}))])
        self.assertIsNone(self.archive().fetch(["a1"])["a1"].sentiment_score)

    def test_unparseable_sentiment_yields_none(self):
        write_archive(self.archive_dir, "2026-07",
                      [dict(record("a1"), sentiment="{not json")])
        self.assertIsNone(self.archive().fetch(["a1"])["a1"].sentiment_score)

    def test_boolean_score_is_rejected_rather_than_cast(self):
        # True would otherwise become 1.0 — an invented measurement.
        write_archive(self.archive_dir, "2026-07", [
            dict(record("a1"), sentiment=json.dumps({"score": True}))])
        self.assertIsNone(self.archive().fetch(["a1"])["a1"].sentiment_score)

    def test_missing_published_at_is_reported_as_none(self):
        write_archive(self.archive_dir, "2026-07",
                      [dict(record("a1"), published_at=None)])
        self.assertIsNone(self.archive().fetch(["a1"])["a1"].published_at)

    def test_non_string_source_is_reported_as_none(self):
        write_archive(self.archive_dir, "2026-07",
                      [dict(record("a1"), source=17)])
        self.assertIsNone(self.archive().fetch(["a1"])["a1"].source)


class TestDuplicates(ArchiveTempDir):
    """
    The writer appends. Its own comment records 18,986 archive lines
    for 3,105 real articles, so duplicates are not hypothetical.
    """

    def test_duplicate_within_one_file_keeps_the_first(self):
        write_archive(self.archive_dir, "2026-07", [
            record("a1", source="First", score=0.1),
            record("a1", source="Second", score=0.9),
        ])
        found = self.archive().fetch(["a1"])["a1"]
        self.assertEqual(found.source, "First")
        self.assertAlmostEqual(found.sentiment_score, 0.1)

    def test_conflicting_duplicates_are_counted_not_merged(self):
        write_archive(self.archive_dir, "2026-07", [
            record("a1", source="First", score=0.1),
            record("a1", source="Second", score=0.9),
            record("a1", source="Third", score=-0.5),
        ])
        archive = self.archive()
        found = archive.fetch(["a1"])
        self.assertEqual(len(found), 1)
        self.assertEqual(found["a1"].source, "First")
        self.assertEqual(archive.stats.duplicate_records, 2)

    def test_duplicate_across_files_keeps_the_earliest_filename(self):
        # Files are read in sorted (= chronological) name order, so the
        # answer does not depend on directory iteration order.
        write_archive(self.archive_dir, "2026-08", [record("a1", source="August")])
        write_archive(self.archive_dir, "2026-07", [record("a1", source="July")])
        self.assertEqual(self.archive().fetch(["a1"])["a1"].source, "July")

    def test_duplicate_resolution_is_stable_across_repeated_reads(self):
        write_archive(self.archive_dir, "2026-07", [
            record("a1", source="First", score=0.1),
            record("a1", source="Second", score=0.9),
        ])
        first = ArticleArchive(self.archive_dir).fetch(["a1"])["a1"]
        second = ArticleArchive(self.archive_dir).fetch(["a1"])["a1"]
        third = ArticleArchive(self.archive_dir).fetch(["a1"])["a1"]
        self.assertEqual(first, second)
        self.assertEqual(second, third)

    def test_duplicates_do_not_inflate_the_id_index(self):
        write_archive(self.archive_dir, "2026-07",
                      [record("a1"), record("a1"), record("a2")])
        archive = self.archive()
        self.assertEqual(archive.article_ids(), frozenset({"a1", "a2"}))
        self.assertEqual(archive.stats.duplicate_records, 1)


class TestDamagedArchives(ArchiveTempDir):
    def test_malformed_json_line_is_skipped_and_counted(self):
        path = write_archive(self.archive_dir, "2026-07", [record("good1")])
        with gzip.open(path, "at", encoding="utf-8") as handle:
            handle.write("{this is not json\n")
        write_archive(self.archive_dir, "2026-08", [record("good2")])
        archive = self.archive()
        found = archive.fetch(["good1", "good2"])
        self.assertEqual(sorted(found), ["good1", "good2"])
        self.assertEqual(archive.stats.malformed_lines, 1)

    def test_json_line_that_is_not_an_object_is_skipped(self):
        path = write_archive(self.archive_dir, "2026-07", [record("good1")])
        with gzip.open(path, "at", encoding="utf-8") as handle:
            handle.write('["a", "list"]\n')
        archive = self.archive()
        self.assertEqual(sorted(archive.fetch(["good1"])), ["good1"])
        self.assertEqual(archive.stats.malformed_lines, 1)

    def test_record_without_article_id_is_skipped_and_counted(self):
        payload = record("x")
        del payload["article_id"]
        write_archive(self.archive_dir, "2026-07", [payload, record("good1")])
        archive = self.archive()
        self.assertEqual(sorted(archive.fetch(["good1"])), ["good1"])
        self.assertEqual(archive.stats.records_without_id, 1)

    def test_blank_lines_are_ignored_without_being_called_malformed(self):
        path = write_archive(self.archive_dir, "2026-07", [record("good1")])
        with gzip.open(path, "at", encoding="utf-8") as handle:
            handle.write("\n\n")
        archive = self.archive()
        self.assertEqual(sorted(archive.fetch(["good1"])), ["good1"])
        self.assertEqual(archive.stats.malformed_lines, 0)

    def test_file_that_is_not_gzip_is_reported_unreadable_not_raised(self):
        os.makedirs(self.archive_dir, exist_ok=True)
        with open(os.path.join(self.archive_dir, "articles_2026-07.jsonl.gz"), "wb") as handle:
            handle.write(b"this is plain text, not gzip at all")
        write_archive(self.archive_dir, "2026-08", [record("good1")])
        archive = self.archive()
        found = archive.fetch(["good1"])  # must not raise
        self.assertEqual(sorted(found), ["good1"])
        self.assertIn("articles_2026-07.jsonl.gz", archive.stats.files_unreadable)

    def test_truncated_gzip_keeps_the_records_read_before_the_break(self):
        # A file interrupted on its way to disk. Everything decoded
        # before the truncation is real data and must survive.
        path = write_archive(self.archive_dir, "2026-07",
                             [record(f"a{i}") for i in range(200)])
        with open(path, "rb") as handle:
            blob = handle.read()
        with open(path, "wb") as handle:
            handle.write(blob[: int(len(blob) * 0.6)])
        write_archive(self.archive_dir, "2026-08", [record("later")])
        archive = self.archive()
        found = archive.fetch([f"a{i}" for i in range(200)] + ["later"])
        self.assertIn("later", found)
        self.assertGreater(len(found), 1, "partial read produced nothing at all")
        self.assertEqual(archive.stats.files_unreadable,
                         ["articles_2026-07.jsonl.gz"])

    def test_partially_readable_archive_still_answers_contains(self):
        os.makedirs(self.archive_dir, exist_ok=True)
        with open(os.path.join(self.archive_dir, "articles_2026-06.jsonl.gz"), "wb") as handle:
            handle.write(b"\x1f\x8b garbage after a plausible magic number")
        write_archive(self.archive_dir, "2026-07", [record("survivor")])
        archive = self.archive()
        self.assertTrue(archive.contains("survivor"))
        self.assertFalse(archive.contains("never-existed"))


class TestDeterminismAndStreaming(ArchiveTempDir):
    def test_repeated_fetch_returns_identical_results(self):
        write_archive(self.archive_dir, "2026-07",
                      [record(f"a{i}", score=i / 10) for i in range(25)])
        wanted = [f"a{i}" for i in range(25)]
        first = ArticleArchive(self.archive_dir).fetch(wanted)
        second = ArticleArchive(self.archive_dir).fetch(wanted)
        self.assertEqual(first, second)

    def test_fetch_makes_one_pass_regardless_of_how_many_ids(self):
        write_archive(self.archive_dir, "2026-07",
                      [record(f"a{i}") for i in range(40)])
        one = ArticleArchive(self.archive_dir)
        one.fetch(["a0"])
        many = ArticleArchive(self.archive_dir)
        many.fetch([f"a{i}" for i in range(40)])
        self.assertEqual(one.stats.lines_read, many.stats.lines_read)
        self.assertEqual(one.stats.lines_read, 40)

    def test_id_index_is_cached_rather_than_rescanned(self):
        write_archive(self.archive_dir, "2026-07",
                      [record(f"a{i}") for i in range(12)])
        archive = self.archive()
        archive.contains("a0")
        after_first = archive.stats.lines_read
        for i in range(12):
            archive.contains(f"a{i}")
        self.assertEqual(archive.stats.lines_read, after_first)

    def test_stats_summary_is_printable_without_an_archive(self):
        self.assertIsInstance(self.archive().stats.summary(), str)


if __name__ == "__main__":
    unittest.main()
