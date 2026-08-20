"""
test_event_fusion.py
------------------------
Unit tests for Event Fusion v1 (fuse_events()).
"""

import unittest
from datetime import datetime, timedelta, timezone

from event_fusion import EventFusion


BASE_TIME = datetime(2026, 8, 1, 9, 0, 0, tzinfo=timezone.utc)


def make_article(
    title="Article", source="Reuters", url="http://a/1",
    companies=("Nvidia",), event_types=("EARNINGS",),
    impact=0.6, published_at=None,
):
    return {
        "title": title, "source": source, "url": url,
        "companies_mentioned": list(companies),
        "events": [{"event_type": et, "matched_phrase": "x"} for et in event_types],
        "impact": {"score": impact},
        "published_at": (published_at or BASE_TIME).isoformat(),
    }


class TestBasicGrouping(unittest.TestCase):
    def setUp(self):
        self.fusion = EventFusion(time_window_hours=72)

    def test_single_article_produces_one_event(self):
        events = self.fusion.fuse_events([make_article()])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["article_count"], 1)
        self.assertFalse(events[0]["confirmed_by_multiple_sources"])

    def test_two_sources_same_entity_and_event_type_fuse_into_one_event(self):
        a1 = make_article(source="Reuters", published_at=BASE_TIME)
        a2 = make_article(source="CNBC", published_at=BASE_TIME + timedelta(hours=2))
        events = self.fusion.fuse_events([a1, a2])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["article_count"], 2)
        self.assertEqual(events[0]["source_count"], 2)
        self.assertTrue(events[0]["confirmed_by_multiple_sources"])

    def test_articles_with_no_detected_event_are_excluded_entirely(self):
        no_event = {"title": "Routine update", "source": "A", "url": "http://a",
                     "companies_mentioned": ["Nvidia"], "events": [], "impact": {"score": 0.2},
                     "published_at": BASE_TIME.isoformat()}
        events = self.fusion.fuse_events([no_event])
        self.assertEqual(events, [])

    def test_empty_batch_returns_empty_list(self):
        self.assertEqual(self.fusion.fuse_events([]), [])

    def test_different_companies_produce_separate_events(self):
        nvda = make_article(companies=("Nvidia",))
        tsla = make_article(companies=("Tesla",))
        events = self.fusion.fuse_events([nvda, tsla])
        self.assertEqual(len(events), 2)
        entities = {e["entity"] for e in events}
        self.assertEqual(entities, {"Nvidia", "Tesla"})

    def test_different_event_types_same_company_produce_separate_events(self):
        earnings = make_article(companies=("Nvidia",), event_types=("EARNINGS",))
        layoffs = make_article(companies=("Nvidia",), event_types=("LAYOFFS",))
        events = self.fusion.fuse_events([earnings, layoffs])
        self.assertEqual(len(events), 2)
        event_types = {e["event_type"] for e in events}
        self.assertEqual(event_types, {"EARNINGS", "LAYOFFS"})


class TestTimeWindowClustering(unittest.TestCase):
    def setUp(self):
        self.fusion = EventFusion(time_window_hours=72)

    def test_within_window_fuses(self):
        a1 = make_article(published_at=BASE_TIME)
        a2 = make_article(published_at=BASE_TIME + timedelta(hours=72))
        events = self.fusion.fuse_events([a1, a2])
        self.assertEqual(len(events), 1)

    def test_outside_window_splits_into_separate_events(self):
        a1 = make_article(published_at=BASE_TIME)
        a2 = make_article(published_at=BASE_TIME + timedelta(hours=100))
        events = self.fusion.fuse_events([a1, a2])
        self.assertEqual(len(events), 2)

    def test_chain_linking_captures_slowly_evolving_story(self):
        # Each gap is within the window, but the TOTAL span exceeds it
        # — chain-linking should still treat this as ONE ongoing event,
        # unlike a fixed window measured strictly from the first article.
        a1 = make_article(published_at=BASE_TIME)
        a2 = make_article(published_at=BASE_TIME + timedelta(hours=60))
        a3 = make_article(published_at=BASE_TIME + timedelta(hours=120))
        events = self.fusion.fuse_events([a1, a2, a3])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["article_count"], 3)

    def test_unparseable_timestamp_stays_in_current_cluster(self):
        a1 = make_article(published_at=BASE_TIME)
        a2 = make_article()
        a2["published_at"] = "not-a-date"
        a2["collected_at"] = None
        events = self.fusion.fuse_events([a1, a2])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["article_count"], 2)


class TestEventShape(unittest.TestCase):
    def setUp(self):
        self.fusion = EventFusion(time_window_hours=72)

    def test_timeline_sorted_chronologically(self):
        a1 = make_article(title="Later", published_at=BASE_TIME + timedelta(hours=5))
        a2 = make_article(title="Earlier", published_at=BASE_TIME)
        events = self.fusion.fuse_events([a1, a2])
        titles = [t["title"] for t in events[0]["timeline"]]
        self.assertEqual(titles, ["Earlier", "Later"])

    def test_first_and_last_reported_at_match_cluster_bounds(self):
        a1 = make_article(published_at=BASE_TIME)
        a2 = make_article(published_at=BASE_TIME + timedelta(hours=10))
        events = self.fusion.fuse_events([a1, a2])
        self.assertEqual(events[0]["first_reported_at"], BASE_TIME.isoformat())
        self.assertEqual(events[0]["last_reported_at"], (BASE_TIME + timedelta(hours=10)).isoformat())

    def test_representative_article_is_highest_impact(self):
        low = make_article(title="Low impact", impact=0.2, published_at=BASE_TIME)
        high = make_article(title="High impact", impact=0.9, published_at=BASE_TIME + timedelta(hours=1))
        events = self.fusion.fuse_events([low, high])
        self.assertEqual(events[0]["representative_title"], "High impact")

    def test_sources_deduplicated_and_sorted(self):
        a1 = make_article(source="Reuters", published_at=BASE_TIME)
        a2 = make_article(source="Reuters", published_at=BASE_TIME + timedelta(hours=1))
        a3 = make_article(source="CNBC", published_at=BASE_TIME + timedelta(hours=2))
        events = self.fusion.fuse_events([a1, a2, a3])
        self.assertEqual(events[0]["sources"], ["CNBC", "Reuters"])
        self.assertEqual(events[0]["source_count"], 2)

    def test_multiple_event_types_on_one_article_produce_multiple_events(self):
        # An article tagged with 2 event types for the same company
        # contributes to TWO separate (entity, event_type) groups.
        dual = make_article(companies=("Nvidia",), event_types=("CEO_CHANGE", "LAWSUIT"))
        events = self.fusion.fuse_events([dual])
        self.assertEqual(len(events), 2)
        event_types = {e["event_type"] for e in events}
        self.assertEqual(event_types, {"CEO_CHANGE", "LAWSUIT"})


class TestCompanySchemaVariants(unittest.TestCase):
    """Confirm companies_mentioned works whether it's plain strings or dicts with canonical_name."""

    def setUp(self):
        self.fusion = EventFusion(time_window_hours=72)

    def test_string_list_schema(self):
        a1 = make_article(published_at=BASE_TIME)
        a1["companies_mentioned"] = ["Nvidia"]
        events = self.fusion.fuse_events([a1])
        self.assertEqual(events[0]["entity"], "Nvidia")

    def test_dict_list_schema_with_canonical_name_key(self):
        a1 = make_article(published_at=BASE_TIME)
        a1["companies_mentioned"] = [{"canonical_name": "Nvidia", "ticker": "NVDA"}]
        events = self.fusion.fuse_events([a1])
        self.assertEqual(events[0]["entity"], "Nvidia")


if __name__ == "__main__":
    unittest.main(verbosity=2)
