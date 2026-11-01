"""
tests/scripts/test_event_retention.py
-----------------------------------------------------------
Tests for src/events/retention.py and the two rebuild sweeps that use
it, in scripts/populate_events.py and scripts/fuse_events.py.

THE DEFECT THESE GUARD
----------------------
Both scripts used to delete everything the current run did not
re-produce. `archive_old_articles.py --keep-days 60` moves old
articles out of the live `articles` table that populate_events reads,
so an archived article's events stopped being produced and were
deleted; fuse_events then deleted their canonical rows. Measured
(docs/MODEL_CLUSTER_INSUFFICIENCY_AUDIT_2026_11_01.md): `events`
846 -> 153, `canonical_events` 581 -> 129, against 1,295
research_observations that are never deleted.

THE RULE UNDER TEST
-------------------
Absence from a rebuild is not evidence of invalidity. A row is deleted
only on positive evidence, and the hardest case has its own class
below: an article that IS live and WAS read, which produced no such
report. That is a disagreement between the extractor and the stored
row, and it is NOT a disproof of the row -- so the row is kept and
reported as unresolved.

The three legitimate cleanups still have to work, or this change would
just be "stop deleting", which would let the uuid4-migration remnants
the original sweep was written for accumulate forever.
"""

import gzip
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scripts.populate_events import main as populate_main, report_id_for
from scripts.fuse_events import canonical_id_for, main as fuse_main
from src.data_access.article_archive import ArticleArchive
from src.data_access.event_repository import initialize_event_schema
from src.data_access.fusion_schema import initialize_fusion_schema
from src.data_access.news_schema import initialize_news_schema
from src.data_access.research_schema import initialize_research_schema
from src.events.retention import (
    DELETE_ARTICLE_GONE, DELETE_DANGLING_REPORTS, DELETE_ID_SCHEME_REMNANT,
    DELETE_NO_PROVENANCE, DELETE_RECOMPUTED, DELETE_VERDICTS,
    KEEP_ARCHIVED_SOURCE, KEEP_OUT_OF_SCAN_SCOPE, KEEP_TERMINAL_HISTORY,
    KEEP_UNRESOLVED, TERMINAL_STATUSES,
    delete_canonical_events, delete_event_reports,
    plan_canonical_retention, plan_event_report_retention,
)
from src.news_database import NewsDatabase

ACQUISITION_TITLE = "NVIDIA agrees to acquire Company Y for $2 billion"
ACQUISITION_SUMMARY = "The deal is expected to close next quarter."


def insert_article(conn, article_id, published_at, title=ACQUISITION_TITLE,
                   source="Reuters", entity="nvidia"):
    conn.execute("""
        INSERT INTO articles (article_id, url, title, summary, source, published_at, stored_at)
        VALUES (?,?,?,?,?,?, 'now')
    """, (article_id, f"http://x/{article_id}", title, ACQUISITION_SUMMARY,
          source, published_at))
    if entity:
        conn.execute("INSERT INTO article_entities (article_id, entity_type, entity_id) "
                     "VALUES (?, 'company', ?)", (article_id, entity))


def insert_event(conn, event_id, event_type="acquisition", *, article_id=None,
                 status="detected", supersedes=None, with_link=True,
                 with_evidence=True):
    """One `events` row, plus the provenance rows the policy reads."""
    conn.execute("""
        INSERT INTO events (event_id, event_type, category, title, publication_time,
                            confidence_json, confidence_score, status, extraction_tier,
                            attributes_json, version, supersedes_event_id)
        VALUES (?,?,'corporate_action','T','2026-07-01T10:00:00+00:00',
                '{}', 0.7, ?, 'deterministic', '{}', 1, ?)
    """, (event_id, event_type, status, supersedes))
    if article_id and with_link:
        conn.execute("INSERT INTO article_event_links (article_id, event_id, relation, created_at) "
                     "VALUES (?,?,'creates','now')", (article_id, event_id))
    if article_id and with_evidence:
        conn.execute("INSERT INTO event_evidence (event_id, article_id, source_name, "
                     "published_at, is_syndicated) VALUES (?,?,'Reuters',"
                     "'2026-07-01T10:00:00+00:00',0)", (event_id, article_id))


def insert_canonical(conn, canonical_id, report_ids):
    conn.execute("""
        INSERT INTO canonical_events (canonical_event_id, event_type, category,
            lifecycle_state, corroboration_state, first_reported_at,
            independent_source_count, total_report_count, has_contradictions,
            quality_confidence)
        VALUES (?,'acquisition','corporate_action','reported','single_source',
                '2026-07-01T10:00:00+00:00',0,?,0,0.7)
    """, (canonical_id, len(report_ids)))
    for report_id in report_ids:
        conn.execute("INSERT INTO canonical_event_reports (canonical_event_id, report_id) "
                     "VALUES (?,?)", (canonical_id, report_id))


def write_archive(directory, month, records):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"articles_{month}.jsonl.gz")
    with gzip.open(path, "at", encoding="utf-8") as handle:
        for rec in records:
            handle.write(json.dumps(rec) + "\n")
    return path


def archived_record(article_id, published_at="2026-05-01T10:00:00+00:00", score=0.3):
    return {
        "article_id": article_id, "url": f"http://x/{article_id}",
        "title": ACQUISITION_TITLE, "summary": ACQUISITION_SUMMARY,
        "source": "Reuters", "published_at": published_at,
        "collected_at": published_at, "stored_at": published_at,
        "sentiment": json.dumps({"score": score, "label": "positive"}),
    }


class RetentionFixture(unittest.TestCase):
    """A real database file with the news, event and fusion schemas."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.db_path = os.path.join(self.root, "marketlens.db")
        self.archive_dir = os.path.join(self.root, "archives")
        NewsDatabase(self.db_path).close()
        self.conn = sqlite3.connect(self.db_path)
        initialize_news_schema(self.conn)
        initialize_event_schema(self.conn)
        initialize_fusion_schema(self.conn)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def archive(self):
        return ArticleArchive(self.archive_dir)

    def verdicts(self, plan):
        return {d.row_id: d.verdict for d in plan.decisions}

    def plan(self, *, produced=(), scanned=None, archive=None, with_scheme=True):
        return plan_event_report_retention(
            self.conn,
            produced_ids=set(produced),
            scanned_article_ids=None if scanned is None else set(scanned),
            archive_contains=(archive or self.archive()).contains,
            report_id_for=report_id_for if with_scheme else None,
        )


class TestArchivedSourceIsPreserved(RetentionFixture):
    """The repair itself."""

    def test_event_whose_article_is_archived_is_kept(self):
        article_id = "old-1"
        event_id = report_id_for(article_id, "acquisition")
        insert_event(self.conn, event_id, article_id=article_id)
        self.conn.commit()
        write_archive(self.archive_dir, "2026-05", [archived_record(article_id)])

        plan = self.plan(produced=(), scanned=())
        self.assertEqual(self.verdicts(plan), {event_id: KEEP_ARCHIVED_SOURCE})
        self.assertEqual(plan.to_delete, [])

    def test_the_same_event_would_be_deleted_without_the_archive(self):
        # The negative control: with no archive file the article is in
        # neither place, which IS positive evidence, and the row goes.
        article_id = "old-1"
        event_id = report_id_for(article_id, "acquisition")
        insert_event(self.conn, event_id, article_id=article_id)
        self.conn.commit()

        plan = self.plan(produced=(), scanned=())
        self.assertEqual(self.verdicts(plan), {event_id: DELETE_ARTICLE_GONE})
        self.assertEqual(plan.to_delete, [event_id])

    def test_a_damaged_archive_does_not_resurrect_a_false_positive(self):
        article_id = "old-1"
        event_id = report_id_for(article_id, "acquisition")
        insert_event(self.conn, event_id, article_id=article_id)
        self.conn.commit()
        os.makedirs(self.archive_dir, exist_ok=True)
        with open(os.path.join(self.archive_dir, "articles_2026-05.jsonl.gz"), "wb") as handle:
            handle.write(b"not gzip")

        plan = self.plan(produced=(), scanned=())  # must not raise
        self.assertEqual(self.verdicts(plan), {event_id: DELETE_ARTICLE_GONE})

    def test_many_archived_events_are_all_kept(self):
        expected = {}
        records = []
        for i in range(25):
            article_id = f"old-{i}"
            event_id = report_id_for(article_id, "acquisition")
            insert_event(self.conn, event_id, article_id=article_id)
            expected[event_id] = KEEP_ARCHIVED_SOURCE
            records.append(archived_record(article_id))
        self.conn.commit()
        write_archive(self.archive_dir, "2026-05", records)

        plan = self.plan(produced=(), scanned=())
        self.assertEqual(self.verdicts(plan), expected)
        self.assertEqual(len(plan.to_keep), 25)


class TestNotRegeneratedIsNotContradicted(RetentionFixture):
    """
    The safety rule, stated as tests.

    An article that is live and WAS read, yet produced no such report,
    is a disagreement between the current extractor and a stored row.
    It is not proof the stored row was ever false, so the row survives
    and is reported as unresolved.
    """

    def test_live_and_scanned_but_not_regenerated_is_kept_not_deleted(self):
        article_id = "live-1"
        event_id = report_id_for(article_id, "acquisition")
        insert_article(self.conn, article_id, "2026-10-01T10:00:00+00:00")
        insert_event(self.conn, event_id, article_id=article_id)
        self.conn.commit()

        plan = self.plan(produced=(), scanned=[article_id])
        self.assertEqual(self.verdicts(plan), {event_id: KEEP_UNRESOLVED})
        self.assertEqual(plan.to_delete, [])

    def test_it_is_reported_as_unresolved_rather_than_silently_kept(self):
        article_id = "live-1"
        event_id = report_id_for(article_id, "acquisition")
        insert_article(self.conn, article_id, "2026-10-01T10:00:00+00:00")
        insert_event(self.conn, event_id, article_id=article_id)
        self.conn.commit()

        plan = self.plan(produced=(), scanned=[article_id])
        self.assertEqual([d.row_id for d in plan.unresolved], [event_id])
        self.assertIn(article_id, plan.unresolved[0].detail)

    def test_a_regenerated_event_is_not_classified_at_all(self):
        article_id = "live-1"
        event_id = report_id_for(article_id, "acquisition")
        insert_article(self.conn, article_id, "2026-10-01T10:00:00+00:00")
        insert_event(self.conn, event_id, article_id=article_id)
        self.conn.commit()

        plan = self.plan(produced=[event_id], scanned=[article_id])
        self.assertEqual(plan.decisions, [])

    def test_unresolved_and_archived_are_distinguished_in_one_run(self):
        live_article, old_article = "live-1", "old-1"
        live_event = report_id_for(live_article, "acquisition")
        old_event = report_id_for(old_article, "acquisition")
        insert_article(self.conn, live_article, "2026-10-01T10:00:00+00:00")
        insert_event(self.conn, live_event, article_id=live_article)
        insert_event(self.conn, old_event, article_id=old_article)
        self.conn.commit()
        write_archive(self.archive_dir, "2026-05", [archived_record(old_article)])

        plan = self.plan(produced=(), scanned=[live_article])
        self.assertEqual(self.verdicts(plan), {
            live_event: KEEP_UNRESOLVED,
            old_event: KEEP_ARCHIVED_SOURCE,
        })
        self.assertEqual(plan.to_delete, [])


class TestRestrictedScans(RetentionFixture):
    """`--limit` and `--since` restrict what a run can see."""

    def test_live_article_outside_the_scan_is_out_of_scope_not_stale(self):
        article_id = "live-1"
        event_id = report_id_for(article_id, "acquisition")
        insert_article(self.conn, article_id, "2026-10-01T10:00:00+00:00")
        insert_event(self.conn, event_id, article_id=article_id)
        self.conn.commit()

        plan = self.plan(produced=(), scanned=[])  # scanned nothing
        self.assertEqual(self.verdicts(plan), {event_id: KEEP_OUT_OF_SCAN_SCOPE})
        self.assertEqual(plan.to_delete, [])

    def test_unknown_scan_scope_is_treated_as_having_scanned_nothing(self):
        # scanned_article_ids=None must be the conservative reading, not
        # "everything", or a caller that forgets to pass it deletes.
        article_id = "live-1"
        event_id = report_id_for(article_id, "acquisition")
        insert_article(self.conn, article_id, "2026-10-01T10:00:00+00:00")
        insert_event(self.conn, event_id, article_id=article_id)
        self.conn.commit()

        plan = self.plan(produced=(), scanned=None)
        self.assertEqual(self.verdicts(plan), {event_id: KEEP_OUT_OF_SCAN_SCOPE})

    def test_scanned_and_unscanned_are_distinguished_within_one_run(self):
        scanned_article, skipped_article = "live-scanned", "live-skipped"
        scanned_event = report_id_for(scanned_article, "acquisition")
        skipped_event = report_id_for(skipped_article, "acquisition")
        insert_article(self.conn, scanned_article, "2026-10-01T10:00:00+00:00")
        insert_article(self.conn, skipped_article, "2026-09-01T10:00:00+00:00")
        insert_event(self.conn, scanned_event, article_id=scanned_article)
        insert_event(self.conn, skipped_event, article_id=skipped_article)
        self.conn.commit()

        plan = self.plan(produced=(), scanned=[scanned_article])
        self.assertEqual(self.verdicts(plan), {
            scanned_event: KEEP_UNRESOLVED,
            skipped_event: KEEP_OUT_OF_SCAN_SCOPE,
        })


class TestLegitimateCleanupStillWorks(RetentionFixture):
    """
    If this change only stopped deleting, the uuid4 remnants the sweep
    was written for would accumulate forever.
    """

    def test_event_with_no_provenance_at_all_is_deleted(self):
        insert_event(self.conn, "evt-orphan", article_id=None)
        self.conn.commit()

        plan = self.plan(produced=(), scanned=())
        self.assertEqual(self.verdicts(plan), {"evt-orphan": DELETE_NO_PROVENANCE})

    def test_uuid_scheme_remnant_is_deleted_even_when_article_is_archived(self):
        # The strongest case for the id check: the article IS archived,
        # so the archive rule alone would preserve a row the current
        # writer can never mint or refresh.
        article_id = "old-1"
        insert_event(self.conn, "9f2c1d44-1111-2222-3333-444455556666",
                     article_id=article_id)
        self.conn.commit()
        write_archive(self.archive_dir, "2026-05", [archived_record(article_id)])

        plan = self.plan(produced=(), scanned=())
        self.assertEqual(
            self.verdicts(plan),
            {"9f2c1d44-1111-2222-3333-444455556666": DELETE_ID_SCHEME_REMNANT})

    def test_uuid_remnant_is_deleted_even_when_article_is_live_and_scanned(self):
        article_id = "live-1"
        insert_article(self.conn, article_id, "2026-10-01T10:00:00+00:00")
        insert_event(self.conn, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                     article_id=article_id)
        self.conn.commit()

        plan = self.plan(produced=(), scanned=[article_id])
        self.assertEqual(
            self.verdicts(plan),
            {"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee": DELETE_ID_SCHEME_REMNANT})

    def test_id_check_is_skipped_when_no_scheme_is_supplied(self):
        # Honest degradation: with no id function the remnant check
        # cannot be made, so it is not guessed at.
        article_id = "old-1"
        insert_event(self.conn, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                     article_id=article_id)
        self.conn.commit()
        write_archive(self.archive_dir, "2026-05", [archived_record(article_id)])

        plan = self.plan(produced=(), scanned=(), with_scheme=False)
        self.assertEqual(
            self.verdicts(plan),
            {"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee": KEEP_ARCHIVED_SOURCE})

    def test_a_live_status_does_not_delete(self):
        for index, status in enumerate(("detected", "confirmed", "updated")):
            article_id = f"old-{index}"
            event_id = report_id_for(article_id, "acquisition")
            insert_event(self.conn, event_id, article_id=article_id, status=status)
            write_archive(self.archive_dir, "2026-05", [archived_record(article_id)])
        self.conn.commit()

        plan = self.plan(produced=(), scanned=())
        self.assertEqual(plan.to_delete, [])

    def test_provenance_from_event_evidence_alone_is_enough(self):
        # A row whose article_event_links was lost but whose evidence
        # survives is NOT provenance-less.
        article_id = "old-1"
        event_id = report_id_for(article_id, "acquisition")
        insert_event(self.conn, event_id, article_id=article_id, with_link=False)
        self.conn.commit()
        write_archive(self.archive_dir, "2026-05", [archived_record(article_id)])

        plan = self.plan(produced=(), scanned=())
        self.assertEqual(self.verdicts(plan), {event_id: KEEP_ARCHIVED_SOURCE})


class TestTerminalStatusIsHistoryNotGarbage(RetentionFixture):
    """
    The corrective patch, stated as tests.

    The first version of the policy deleted rows whose `events.status`
    was terminal, and rows named by another row's
    `supersedes_event_id`. docs/HISTORICAL_DATA_RETENTION_REVIEW_2026_11_01.md
    confirmed that as a defect on two grounds:

      * `EventRepository.supersede` says outright that "the old one is
        marked SUPERSEDED rather than deleted -- historical states are
        never destroyed", and the repository exposes
        `include_superseded` on `list` and `count` so callers can
        filter rather than delete;
      * deleting such a row changes the canonical id of the group it
        belongs to, because `canonical_id_for` hashes the report set,
        which dangles `research_observations.event_id` --
        demonstrated, and reproduced here in
        `test_a_historical_canonical_reference_still_resolves`.
    """

    def test_retracted_event_is_retained(self):
        article_id = "old-1"
        event_id = report_id_for(article_id, "acquisition")
        insert_event(self.conn, event_id, article_id=article_id, status="retracted")
        self.conn.commit()
        write_archive(self.archive_dir, "2026-05", [archived_record(article_id)])

        plan = self.plan(produced=(), scanned=())
        self.assertEqual(self.verdicts(plan), {event_id: KEEP_TERMINAL_HISTORY})
        self.assertEqual(plan.to_delete, [])

    def test_superseded_and_cancelled_events_are_retained(self):
        expected = {}
        for index, status in enumerate(("superseded", "cancelled")):
            article_id = f"old-{index}"
            event_id = report_id_for(article_id, "acquisition")
            insert_event(self.conn, event_id, article_id=article_id, status=status)
            expected[event_id] = KEEP_TERMINAL_HISTORY
        self.conn.commit()

        plan = self.plan(produced=(), scanned=())
        self.assertEqual(self.verdicts(plan), expected)
        self.assertEqual(plan.to_delete, [])

    def test_every_terminal_status_is_retained(self):
        # Pinned against the constant rather than a hand-written list,
        # so a new terminal status cannot quietly become deletable.
        expected = {}
        for index, status in enumerate(sorted(TERMINAL_STATUSES)):
            article_id = f"old-{index}"
            event_id = report_id_for(article_id, "acquisition")
            insert_event(self.conn, event_id, article_id=article_id, status=status)
            expected[event_id] = KEEP_TERMINAL_HISTORY
        self.conn.commit()

        plan = self.plan(produced=(), scanned=())
        self.assertEqual(self.verdicts(plan), expected)

    def test_a_terminal_event_is_retained_even_with_no_archive(self):
        # Without the terminal rule this row would be DELETE_ARTICLE_GONE,
        # so this is the case where the two rules genuinely disagree.
        article_id = "old-1"
        event_id = report_id_for(article_id, "acquisition")
        insert_event(self.conn, event_id, article_id=article_id, status="retracted")
        self.conn.commit()

        plan = self.plan(produced=(), scanned=())
        self.assertEqual(self.verdicts(plan), {event_id: KEEP_TERMINAL_HISTORY})

    def test_a_successors_pointer_does_not_delete_its_predecessor(self):
        old_article, new_article = "old-1", "old-2"
        old_event = report_id_for(old_article, "acquisition")
        new_event = report_id_for(new_article, "acquisition")
        insert_event(self.conn, old_event, article_id=old_article)
        insert_event(self.conn, new_event, article_id=new_article,
                     supersedes=old_event)
        self.conn.commit()
        write_archive(self.archive_dir, "2026-05", [
            archived_record(old_article), archived_record(new_article)])

        plan = self.plan(produced=[new_event], scanned=())
        self.assertEqual(self.verdicts(plan), {old_event: KEEP_TERMINAL_HISTORY})
        self.assertEqual(plan.to_delete, [])
        detail = {d.row_id: d.detail for d in plan.decisions}[old_event]
        self.assertIn("superseded by", detail)
        self.assertIn(new_event, detail)

    def test_the_pointer_itself_survives_the_rebuild(self):
        # The point of keeping the predecessor: the successor's
        # supersedes_event_id must still resolve to a real row.
        old_article, new_article = "old-1", "old-2"
        old_event = report_id_for(old_article, "acquisition")
        new_event = report_id_for(new_article, "acquisition")
        insert_event(self.conn, old_event, article_id=old_article,
                     status="superseded")
        insert_event(self.conn, new_event, article_id=new_article,
                     supersedes=old_event)
        self.conn.commit()

        plan = self.plan(produced=[new_event], scanned=())
        delete_event_reports(self.conn, plan.to_delete)
        self.conn.commit()

        resolved = self.conn.execute(
            "SELECT p.event_id FROM events e "
            "JOIN events p ON p.event_id = e.supersedes_event_id "
            "WHERE e.event_id = ?", (new_event,)).fetchone()
        self.assertIsNotNone(resolved, "the supersedes pointer now dangles")
        self.assertEqual(resolved[0], old_event)

    def test_terminal_status_does_not_immortalise_an_orphan(self):
        # Ordering matters: provenance is checked BEFORE status, so a
        # terminal row with no article link is still cleaned up.
        insert_event(self.conn, "evt-orphan", article_id=None, status="retracted")
        self.conn.commit()

        plan = self.plan(produced=(), scanned=())
        self.assertEqual(self.verdicts(plan), {"evt-orphan": DELETE_NO_PROVENANCE})

    def test_terminal_status_does_not_immortalise_a_uuid_remnant(self):
        article_id = "old-1"
        insert_event(self.conn, "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
                     article_id=article_id, status="retracted")
        self.conn.commit()
        write_archive(self.archive_dir, "2026-05", [archived_record(article_id)])

        plan = self.plan(produced=(), scanned=())
        self.assertEqual(
            self.verdicts(plan),
            {"bbbbbbbb-cccc-dddd-eeee-ffffffffffff": DELETE_ID_SCHEME_REMNANT})

    def test_keep_terminal_history_is_not_a_delete_verdict(self):
        self.assertNotIn(KEEP_TERMINAL_HISTORY, DELETE_VERDICTS)

    def test_a_historical_canonical_reference_still_resolves(self):
        """
        The regression this patch exists to prevent, end to end.

        Two reports in one canonical group, one of them retracted, and
        a research observation whose event_id is that group's canonical
        id. Under the old policy the retracted report was deleted, the
        group's id changed (`canonical_id_for` hashes the report set),
        the stale canonical row was pruned as recomputed, and the
        observation's LEFT JOIN resolved to NULL -- the same NULL-feature
        failure the retention work exists to stop.
        """
        initialize_research_schema(self.conn)
        live_article, retracted_article = "a-live", "a-retracted"
        live_report = report_id_for(live_article, "acquisition")
        retracted_report = report_id_for(retracted_article, "acquisition")
        insert_article(self.conn, live_article, "2026-10-01T10:00:00+00:00")
        insert_event(self.conn, live_report, article_id=live_article)
        insert_event(self.conn, retracted_report, article_id=retracted_article,
                     status="retracted")

        canonical_id = canonical_id_for([live_report, retracted_report])
        insert_canonical(self.conn, canonical_id, [live_report, retracted_report])
        self.conn.execute(
            "INSERT INTO research_observations (observation_id, event_id, "
            "instrument_id, observation_created_at, information_cutoff, "
            "event_cluster_id, quality_level, dataset_version, event_type) "
            "VALUES ('obs-1',?,'inst-nvda','2026-10-02T00:00:00+00:00',"
            "'2026-10-02T00:00:00+00:00','inst-nvda','high','v1','acquisition')",
            (canonical_id,))
        self.conn.commit()
        write_archive(self.archive_dir, "2026-05",
                      [archived_record(retracted_article)])

        # A rebuild that re-produces only the live report.
        plan = self.plan(produced=[live_report], scanned=[live_article])
        delete_event_reports(self.conn, plan.to_delete)
        self.conn.commit()

        self.assertEqual(plan.to_delete, [],
                         "the retracted report was deleted again")
        surviving = {r[0] for r in self.conn.execute("SELECT event_id FROM events")}
        self.assertEqual(surviving, {live_report, retracted_report})

        # The group is unchanged, so the id a fusion run would mint is
        # the same one the observation already points at.
        self.assertEqual(canonical_id_for(sorted(surviving)), canonical_id)

        cplan = plan_canonical_retention(
            self.conn, produced_ids={canonical_id},
            loaded_report_ids=set(surviving))
        delete_canonical_events(self.conn, cplan.to_delete)
        self.conn.commit()

        joined = self.conn.execute(
            "SELECT ce.canonical_event_id FROM research_observations ro "
            "LEFT JOIN canonical_events ce "
            "ON ce.canonical_event_id = ro.event_id").fetchone()[0]
        self.assertEqual(joined, canonical_id,
                         "the observation's event reference dangles again")


class TestDeletionAndIdempotency(RetentionFixture):
    def test_delete_event_reports_removes_the_row_and_its_children(self):
        article_id = "old-1"
        insert_event(self.conn, "evt-gone", article_id=article_id)
        self.conn.execute("INSERT INTO event_participants (event_id, entity_id, role) "
                          "VALUES ('evt-gone','nvidia','primary')")
        self.conn.commit()

        delete_event_reports(self.conn, ["evt-gone"])
        self.conn.commit()
        for table in ("events", "event_evidence", "event_participants",
                      "article_event_links"):
            count = self.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE event_id = 'evt-gone'").fetchone()[0]
            self.assertEqual(count, 0, table)

    def test_deletion_does_not_touch_articles_or_article_entities(self):
        article_id = "live-1"
        insert_article(self.conn, article_id, "2026-10-01T10:00:00+00:00")
        insert_event(self.conn, "evt-gone", article_id=article_id)
        self.conn.commit()

        delete_event_reports(self.conn, ["evt-gone"])
        self.conn.commit()
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM articles").fetchone()[0], 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM article_entities").fetchone()[0], 1)

    def test_planning_twice_gives_the_same_answer(self):
        article_id = "old-1"
        event_id = report_id_for(article_id, "acquisition")
        insert_event(self.conn, event_id, article_id=article_id)
        insert_event(self.conn, "evt-orphan", article_id=None)
        self.conn.commit()
        write_archive(self.archive_dir, "2026-05", [archived_record(article_id)])

        first = self.verdicts(self.plan(produced=(), scanned=()))
        second = self.verdicts(self.plan(produced=(), scanned=()))
        self.assertEqual(first, second)

    def test_applying_the_plan_twice_is_stable(self):
        article_id = "old-1"
        event_id = report_id_for(article_id, "acquisition")
        insert_event(self.conn, event_id, article_id=article_id)
        insert_event(self.conn, "evt-orphan", article_id=None)
        self.conn.commit()
        write_archive(self.archive_dir, "2026-05", [archived_record(article_id)])

        delete_event_reports(self.conn, self.plan(produced=(), scanned=()).to_delete)
        self.conn.commit()
        after_first = {r[0] for r in self.conn.execute("SELECT event_id FROM events")}

        delete_event_reports(self.conn, self.plan(produced=(), scanned=()).to_delete)
        self.conn.commit()
        after_second = {r[0] for r in self.conn.execute("SELECT event_id FROM events")}

        self.assertEqual(after_first, {event_id})
        self.assertEqual(after_first, after_second)

    def test_decisions_are_emitted_in_a_deterministic_order(self):
        for i in range(8):
            insert_event(self.conn, f"evt-{i:02d}", article_id=None)
        self.conn.commit()
        first = [d.row_id for d in self.plan(produced=(), scanned=()).decisions]
        second = [d.row_id for d in self.plan(produced=(), scanned=()).decisions]
        self.assertEqual(first, second)
        self.assertEqual(first, sorted(first))


class TestCanonicalRetention(RetentionFixture):
    def test_regrouped_canonical_row_is_deleted_as_recomputed(self):
        article_id = "old-1"
        report = report_id_for(article_id, "acquisition")
        insert_event(self.conn, report, article_id=article_id)
        insert_canonical(self.conn, "ce-old", [report])
        self.conn.commit()

        plan = plan_canonical_retention(
            self.conn, produced_ids={"ce-new"}, loaded_report_ids={report})
        self.assertEqual(self.verdicts(plan), {"ce-old": DELETE_RECOMPUTED})

    def test_canonical_row_whose_reports_were_not_loaded_is_kept(self):
        article_id = "old-1"
        report = report_id_for(article_id, "acquisition")
        insert_event(self.conn, report, article_id=article_id)
        insert_canonical(self.conn, "ce-old", [report])
        self.conn.commit()

        plan = plan_canonical_retention(
            self.conn, produced_ids=set(), loaded_report_ids=set())
        self.assertEqual(self.verdicts(plan), {"ce-old": KEEP_OUT_OF_SCAN_SCOPE})
        self.assertEqual(plan.to_delete, [])

    def test_partially_loaded_group_is_kept(self):
        loaded = report_id_for("a-loaded", "acquisition")
        skipped = report_id_for("a-skipped", "acquisition")
        insert_event(self.conn, loaded, article_id="a-loaded")
        insert_event(self.conn, skipped, article_id="a-skipped")
        insert_canonical(self.conn, "ce-old", [loaded, skipped])
        self.conn.commit()

        plan = plan_canonical_retention(
            self.conn, produced_ids=set(), loaded_report_ids={loaded})
        self.assertEqual(self.verdicts(plan), {"ce-old": KEEP_OUT_OF_SCAN_SCOPE})

    def test_canonical_row_pointing_only_at_vanished_reports_is_deleted(self):
        insert_canonical(self.conn, "ce-dangling", ["evt-long-gone"])
        self.conn.commit()

        plan = plan_canonical_retention(
            self.conn, produced_ids=set(), loaded_report_ids=set())
        self.assertEqual(self.verdicts(plan), {"ce-dangling": DELETE_DANGLING_REPORTS})

    def test_canonical_row_with_no_report_links_is_deleted(self):
        insert_canonical(self.conn, "ce-bare", [])
        self.conn.commit()

        plan = plan_canonical_retention(
            self.conn, produced_ids=set(), loaded_report_ids=set())
        self.assertEqual(self.verdicts(plan), {"ce-bare": DELETE_NO_PROVENANCE})

    def test_loaded_report_ids_none_means_everything_was_loaded(self):
        # Backwards compatibility: the unrestricted run must behave
        # exactly as the original sweep did.
        article_id = "old-1"
        report = report_id_for(article_id, "acquisition")
        insert_event(self.conn, report, article_id=article_id)
        insert_canonical(self.conn, "ce-old", [report])
        self.conn.commit()

        plan = plan_canonical_retention(
            self.conn, produced_ids={"ce-new"}, loaded_report_ids=None)
        self.assertEqual(self.verdicts(plan), {"ce-old": DELETE_RECOMPUTED})

    def test_produced_canonical_rows_are_not_classified(self):
        report = report_id_for("old-1", "acquisition")
        insert_event(self.conn, report, article_id="old-1")
        insert_canonical(self.conn, "ce-keep", [report])
        self.conn.commit()

        plan = plan_canonical_retention(
            self.conn, produced_ids={"ce-keep"}, loaded_report_ids={report})
        self.assertEqual(plan.decisions, [])

    def test_delete_canonical_events_removes_children_but_not_events(self):
        report = report_id_for("old-1", "acquisition")
        insert_event(self.conn, report, article_id="old-1")
        insert_canonical(self.conn, "ce-old", [report])
        self.conn.commit()

        delete_canonical_events(self.conn, ["ce-old"])
        self.conn.commit()
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM canonical_events").fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM canonical_event_reports").fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM events").fetchone()[0], 1,
            "fuse_events must never delete a report")


class TestPopulateEventsEndToEnd(unittest.TestCase):
    """
    The real CLI, against a disposable database, with the archive in
    the sibling directory the script derives from the database path.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.db_path = os.path.join(self.root, "marketlens.db")
        self.archive_dir = os.path.join(self.root, "archives")
        NewsDatabase(self.db_path).close()
        conn = sqlite3.connect(self.db_path)
        initialize_news_schema(conn)
        initialize_event_schema(conn)
        initialize_fusion_schema(conn)
        insert_article(conn, "a-live", "2026-10-01T00:00:00+00:00")
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_populate(self, extra=()):
        argv = sys.argv
        sys.argv = ["populate_events.py", "--db", self.db_path,
                    "--max-db-mb", "500"] + list(extra)
        try:
            return populate_main()
        finally:
            sys.argv = argv

    def events(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return {r[0] for r in conn.execute("SELECT event_id FROM events")}
        finally:
            conn.close()

    def seed_archived_event(self, article_id="a-archived"):
        """An event whose source article exists only in the archive."""
        event_id = report_id_for(article_id, "acquisition")
        conn = sqlite3.connect(self.db_path)
        insert_event(conn, event_id, article_id=article_id)
        conn.commit()
        conn.close()
        write_archive(self.archive_dir, "2026-05", [archived_record(article_id)])
        return event_id

    def test_archived_event_survives_a_full_rebuild(self):
        archived_event = self.seed_archived_event()
        self.assertEqual(self.run_populate(["--apply"]), 0)
        after = self.events()
        self.assertIn(archived_event, after,
                      "the archived-source event was deleted by the rebuild")
        self.assertIn(report_id_for("a-live", "acquisition"), after)

    def test_orphan_is_still_cleaned_up_by_a_full_rebuild(self):
        conn = sqlite3.connect(self.db_path)
        insert_event(conn, "evt-no-provenance", article_id=None)
        conn.commit()
        conn.close()

        self.assertEqual(self.run_populate(["--apply"]), 0)
        self.assertNotIn("evt-no-provenance", self.events())

    def test_uuid_remnant_is_still_cleaned_up_by_a_full_rebuild(self):
        conn = sqlite3.connect(self.db_path)
        insert_event(conn, "11112222-3333-4444-5555-666677778888",
                     article_id="a-live")
        conn.commit()
        conn.close()

        self.assertEqual(self.run_populate(["--apply"]), 0)
        self.assertNotIn("11112222-3333-4444-5555-666677778888", self.events())

    def test_since_does_not_delete_events_from_outside_the_window(self):
        conn = sqlite3.connect(self.db_path)
        insert_article(conn, "a-older", "2026-02-01T00:00:00+00:00")
        insert_event(conn, report_id_for("a-older", "acquisition"),
                     article_id="a-older")
        conn.commit()
        conn.close()

        self.assertEqual(self.run_populate(["--since", "2026-09-01", "--apply"]), 0)
        self.assertIn(report_id_for("a-older", "acquisition"), self.events(),
                      "--since deleted an event it never looked at")

    def test_limit_does_not_delete_events_from_outside_the_window(self):
        conn = sqlite3.connect(self.db_path)
        for i in range(4):
            article_id = f"a-extra-{i}"
            insert_article(conn, article_id, f"2026-0{i + 1}-01T00:00:00+00:00")
            insert_event(conn, report_id_for(article_id, "acquisition"),
                         article_id=article_id)
        conn.commit()
        conn.close()
        before = self.events()

        self.assertEqual(self.run_populate(["--limit", "1", "--apply"]), 0)
        self.assertTrue(before.issubset(self.events()),
                        "--limit deleted events outside the scanned window")

    def test_repeated_full_runs_are_stable(self):
        archived_event = self.seed_archived_event()
        self.assertEqual(self.run_populate(["--apply"]), 0)
        first = self.events()
        self.assertEqual(self.run_populate(["--apply"]), 0)
        second = self.events()
        self.assertEqual(self.run_populate(["--apply"]), 0)
        third = self.events()
        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self.assertIn(archived_event, third)

    def test_dry_run_deletes_nothing(self):
        conn = sqlite3.connect(self.db_path)
        insert_event(conn, "evt-no-provenance", article_id=None)
        conn.commit()
        conn.close()

        self.run_populate([])
        self.assertIn("evt-no-provenance", self.events())

    def test_a_missing_archive_directory_is_not_fatal(self):
        self.assertFalse(os.path.isdir(self.archive_dir))
        self.assertEqual(self.run_populate(["--apply"]), 0)
        self.assertIn(report_id_for("a-live", "acquisition"), self.events())


class TestFuseEventsEndToEnd(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.db_path = os.path.join(self.root, "marketlens.db")
        NewsDatabase(self.db_path).close()
        conn = sqlite3.connect(self.db_path)
        initialize_news_schema(conn)
        initialize_event_schema(conn)
        initialize_fusion_schema(conn)
        insert_article(conn, "a-live", "2026-10-01T00:00:00+00:00")
        conn.commit()
        conn.close()
        argv = sys.argv
        sys.argv = ["populate_events.py", "--db", self.db_path,
                    "--max-db-mb", "500", "--apply"]
        try:
            populate_main()
        finally:
            sys.argv = argv

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_fuse(self, extra=()):
        argv = sys.argv
        sys.argv = ["fuse_events.py", "--db", self.db_path,
                    "--max-db-mb", "500"] + list(extra)
        try:
            return fuse_main()
        finally:
            sys.argv = argv

    def canonical(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return {r[0] for r in conn.execute(
                "SELECT canonical_event_id FROM canonical_events")}
        finally:
            conn.close()

    def test_fusion_is_idempotent(self):
        self.assertEqual(self.run_fuse(["--apply"]), 0)
        first = self.canonical()
        self.assertEqual(self.run_fuse(["--apply"]), 0)
        self.assertEqual(first, self.canonical())
        self.assertTrue(first)

    def test_fusion_never_deletes_an_event_report(self):
        conn = sqlite3.connect(self.db_path)
        before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.close()
        self.assertEqual(self.run_fuse(["--apply"]), 0)
        conn = sqlite3.connect(self.db_path)
        after = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.close()
        self.assertEqual(before, after)

    def test_limited_fusion_preserves_canonical_rows_it_did_not_load(self):
        self.assertEqual(self.run_fuse(["--apply"]), 0)
        baseline = self.canonical()
        self.assertTrue(baseline)

        # A second report the limited run will not reach, so the
        # earlier canonical rows are outside its scope.
        conn = sqlite3.connect(self.db_path)
        insert_article(conn, "a-second", "2026-10-02T00:00:00+00:00")
        conn.commit()
        conn.close()
        argv = sys.argv
        sys.argv = ["populate_events.py", "--db", self.db_path,
                    "--max-db-mb", "500", "--apply"]
        try:
            populate_main()
        finally:
            sys.argv = argv

        self.assertEqual(self.run_fuse(["--limit", "1", "--apply"]), 0)
        self.assertTrue(baseline.issubset(self.canonical()),
                        "--limit pruned canonical rows it never examined")


if __name__ == "__main__":
    unittest.main()
