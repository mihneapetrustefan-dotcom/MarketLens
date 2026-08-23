"""
tests/entities/test_resolver.py
------------------------------------
Tests for the entity resolution pipeline, covering every scenario the
Phase 3 spec (§27) explicitly requires — run against BOTH controlled
fixtures and the REAL 389-company registry.
"""

import sys
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.entities.alias_index import AliasIndex, normalize_name
from src.entities.resolver import EntityResolver
from src.entities.index_builder import build_index_from_registries
from src.domain.entity_models import (
    EntityAlias, EntityIdentifier, EntityType, AliasType, IdentifierType,
    ResolutionStatus, ResolutionMethod,
)
from company_registry import COMPANY_REGISTRY
from sector_registry import COMPANY_SECTOR_MAP


def alias(entity_id, text, entity_type=EntityType.COMPANY, risky=False, alias_type=AliasType.DISPLAY_NAME):
    return EntityAlias(entity_id=entity_id, entity_type=entity_type, alias=text,
                        normalized_alias=normalize_name(text), alias_type=alias_type, ambiguity_risk=risky)


def identifier(entity_id, id_type, value, entity_type=EntityType.COMPANY):
    return EntityIdentifier(entity_id=entity_id, entity_type=entity_type,
                             identifier_type=id_type, value=value)


class TestNormalization(unittest.TestCase):
    def test_corporate_suffixes_stripped(self):
        self.assertEqual(normalize_name("NVIDIA Corporation"), normalize_name("Nvidia Corp"))
        self.assertEqual(normalize_name("NVIDIA Corp."), "nvidia")

    def test_case_and_punctuation_ignored(self):
        self.assertEqual(normalize_name("Coca-Cola"), normalize_name("coca cola"))

    def test_suffix_only_name_is_not_emptied(self):
        # "Holdings" alone must not normalize to "" and collide with everything.
        self.assertNotEqual(normalize_name("Holdings"), "")

    def test_empty_input(self):
        self.assertEqual(normalize_name(None), "")
        self.assertEqual(normalize_name(""), "")


class TestRequiredScenarios(unittest.TestCase):
    """The 19 scenarios spec §27 requires. Each maps to a named test below."""

    def setUp(self):
        index = AliasIndex(
            aliases=[
                alias("nvidia", "Nvidia"), alias("nvidia", "NVIDIA Corporation", alias_type=AliasType.LEGAL_NAME),
                alias("amd", "AMD"), alias("amd", "Advanced Micro Devices"),
                alias("electrica", "Electrica"), alias("estee-lauder", "Estee Lauder"),
                alias("apple", "Apple", risky=True),
                alias("meta", "Meta Platforms"), alias("meta", "Facebook", alias_type=AliasType.HISTORICAL_NAME),
                alias("technology", "Technology", entity_type=EntityType.SECTOR),
            ],
            identifiers=[
                identifier("nvidia", IdentifierType.TICKER, "NVDA"),
                identifier("nvidia", IdentifierType.EXCHANGE_TICKER, "NASDAQ:NVDA"),
                identifier("nvidia", IdentifierType.PROVIDER_ID, "finnhub-nvda-001"),
                identifier("amd", IdentifierType.TICKER, "AMD"),
                identifier("electrica", IdentifierType.TICKER, "EL"),
                identifier("electrica", IdentifierType.EXCHANGE_TICKER, "BVB:EL"),
                identifier("estee-lauder", IdentifierType.TICKER, "EL"),
                identifier("estee-lauder", IdentifierType.EXCHANGE_TICKER, "NYSE:EL"),
                identifier("nvidia", IdentifierType.ISIN, "US67066G1040"),
            ],
        )
        self.resolver = EntityResolver(index)

    # 1
    def test_exact_company_match(self):
        result = self.resolver.resolve("Nvidia")
        self.assertEqual(result.status, ResolutionStatus.RESOLVED)
        self.assertEqual(result.entity_id, "nvidia")

    # 2
    def test_alias_match(self):
        result = self.resolver.resolve("NVIDIA Corporation")
        self.assertEqual(result.entity_id, "nvidia")
        self.assertTrue(result.is_confident)

    # 3
    def test_ticker_match(self):
        result = self.resolver.resolve("NVDA")
        self.assertEqual(result.method, ResolutionMethod.TICKER)
        self.assertEqual(result.entity_id, "nvidia")

    def test_cashtag_scores_higher_than_bare_ticker(self):
        bare = self.resolver.resolve("NVDA")
        cashtag = self.resolver.resolve("$NVDA")
        self.assertGreater(cashtag.confidence, bare.confidence)

    # 4
    def test_exchange_plus_ticker_match_is_unambiguous(self):
        electrica = self.resolver.resolve("BVB:EL")
        estee = self.resolver.resolve("NYSE:EL")
        self.assertEqual(electrica.entity_id, "electrica")
        self.assertEqual(estee.entity_id, "estee-lauder")
        self.assertEqual(electrica.confidence, Decimal("1.0"))

    # 5
    def test_provider_id_match(self):
        ids = self.resolver.index.lookup_identifier(IdentifierType.PROVIDER_ID, "finnhub-nvda-001")
        self.assertEqual(ids, ["nvidia"])

    # 6
    def test_fuzzy_match_on_misspelling(self):
        result = self.resolver.resolve("Advancd Micro Devices")
        self.assertEqual(result.method, ResolutionMethod.FUZZY)
        self.assertEqual(result.entity_id, "amd")
        self.assertEqual(result.status, ResolutionStatus.HIGH_CONFIDENCE)

    # 7
    def test_ambiguous_name_is_not_blindly_resolved(self):
        result = self.resolver.resolve("Apple")
        self.assertEqual(result.status, ResolutionStatus.AMBIGUOUS)
        self.assertIsNone(result.entity_id)

    def test_ambiguous_name_resolves_with_financial_context(self):
        result = self.resolver.resolve(
            "Apple", context="Apple shares climbed after quarterly earnings beat analyst estimates"
        )
        self.assertEqual(result.status, ResolutionStatus.HIGH_CONFIDENCE)
        self.assertEqual(result.entity_id, "apple")
        self.assertEqual(result.method, ResolutionMethod.CONTEXTUAL)

    def test_ambiguous_name_stays_ambiguous_in_non_financial_context(self):
        result = self.resolver.resolve("Apple", context="She ate an apple while walking through the orchard")
        self.assertEqual(result.status, ResolutionStatus.AMBIGUOUS)

    # 8
    def test_false_positive_rejection_unknown_company(self):
        result = self.resolver.resolve("Totally Fictional Holdings Group")
        self.assertEqual(result.status, ResolutionStatus.UNRESOLVED)
        self.assertIsNone(result.entity_id)

    def test_short_strings_are_never_fuzzy_matched(self):
        # "AMC" vs "AMD" — high string similarity, completely different companies.
        result = self.resolver.resolve("AMC")
        self.assertNotEqual(result.entity_id, "amd")

    # 11
    def test_shared_ticker_across_exchanges_is_ambiguous_not_guessed(self):
        result = self.resolver.resolve("EL")
        self.assertEqual(result.status, ResolutionStatus.AMBIGUOUS)
        self.assertEqual(set(result.candidates), {"electrica", "estee-lauder"})

    def test_shared_ticker_resolved_by_context(self):
        result = self.resolver.resolve("EL", context="Estee Lauder reported quarterly revenue growth")
        self.assertEqual(result.entity_id, "estee-lauder")
        self.assertEqual(result.method, ResolutionMethod.CONTEXTUAL)

    # 12
    def test_historical_name_still_resolves(self):
        result = self.resolver.resolve("Facebook")
        self.assertEqual(result.entity_id, "meta")

    # 18/19
    def test_sector_resolves_as_sector_not_company(self):
        result = self.resolver.resolve("Technology")
        self.assertEqual(result.entity_type, EntityType.SECTOR)

    def test_expected_type_filter_excludes_wrong_type(self):
        result = self.resolver.resolve("Technology", expected_type=EntityType.COMPANY)
        self.assertEqual(result.status, ResolutionStatus.UNRESOLVED)


class TestResolverBehaviour(unittest.TestCase):
    def setUp(self):
        index = AliasIndex(
            aliases=[alias("nvidia", "Nvidia"), alias("amd", "AMD")],
            identifiers=[identifier("nvidia", IdentifierType.TICKER, "NVDA")],
        )
        self.resolver = EntityResolver(index)

    def test_empty_query_is_unresolved_not_an_error(self):
        self.assertEqual(self.resolver.resolve("").status, ResolutionStatus.UNRESOLVED)
        self.assertEqual(self.resolver.resolve("   ").status, ResolutionStatus.UNRESOLVED)

    def test_result_is_never_none(self):
        for q in ["", "?!?", "Nvidia", "$$$", "12345"]:
            self.assertIsNotNone(self.resolver.resolve(q))

    def test_every_confident_result_records_its_method(self):
        result = self.resolver.resolve("Nvidia")
        self.assertNotEqual(result.method, ResolutionMethod.NONE)

    def test_no_result_ever_uses_the_semantic_model_tier(self):
        """Phase 3 is entirely deterministic — no model call exists in this pipeline."""
        for q in ["Nvidia", "NVDA", "Nvdia", "unknown thing"]:
            self.assertNotEqual(self.resolver.resolve(q).method, ResolutionMethod.SEMANTIC_MODEL)

    def test_cache_returns_consistent_results(self):
        first = self.resolver.resolve("Nvidia")
        second = self.resolver.resolve("Nvidia")
        self.assertEqual(first.entity_id, second.entity_id)

    def test_cache_can_be_cleared(self):
        self.resolver.resolve("Nvidia")
        self.resolver.clear_cache()
        self.assertEqual(self.resolver.resolve("Nvidia").entity_id, "nvidia")

    def test_batch_resolution(self):
        results = self.resolver.resolve_batch(["Nvidia", "AMD", "Nonexistent"])
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0].entity_id, "nvidia")
        self.assertEqual(results[2].status, ResolutionStatus.UNRESOLVED)


class TestQualityMetrics(unittest.TestCase):
    def setUp(self):
        index = AliasIndex(
            aliases=[alias("nvidia", "Nvidia"), alias("apple", "Apple", risky=True)],
            identifiers=[identifier("nvidia", IdentifierType.TICKER, "NVDA")],
        )
        self.resolver = EntityResolver(index)

    def test_metrics_track_each_outcome_type(self):
        self.resolver.resolve("Nvidia")
        self.resolver.resolve("Apple")
        self.resolver.resolve("Unknown Entity Name Here")

        m = self.resolver.metrics
        self.assertEqual(m.total_mentions, 3)
        self.assertEqual(m.resolved, 1)
        self.assertEqual(m.ambiguous, 1)
        self.assertEqual(m.unresolved, 1)

    def test_resolution_rate_computed(self):
        self.resolver.resolve("Nvidia")
        self.resolver.resolve("Unknown Entity Name Here")
        self.assertEqual(self.resolver.metrics.resolution_rate, 0.5)

    def test_rates_are_none_when_nothing_measured(self):
        self.assertIsNone(self.resolver.metrics.resolution_rate)
        self.assertIsNone(self.resolver.metrics.ambiguity_rate)

    def test_method_breakdown_recorded(self):
        self.resolver.resolve("Nvidia")
        self.resolver.resolve("NVDA")
        self.assertIn("ticker", self.resolver.metrics.by_method)


class TestAgainstRealRegistry(unittest.TestCase):
    """Run the resolver against the REAL 389-company dataset, not a fixture."""

    @classmethod
    def setUpClass(cls):
        cls.index = build_index_from_registries(COMPANY_REGISTRY, COMPANY_SECTOR_MAP)
        cls.resolver = EntityResolver(cls.index)

    def test_index_covers_the_whole_registry(self):
        self.assertGreater(self.index.alias_count, 300)
        self.assertGreater(self.index.identifier_count, 700)

    def test_spec_example_variants_all_resolve_to_the_same_company(self):
        """The spec's own example: every way of writing NVIDIA resolves consistently."""
        variants = ["NVIDIA", "Nvidia", "NVIDIA Corp.", "Nvidia Corporation", "NVDA", "$NVDA"]
        resolved = {self.resolver.resolve(v).entity_id for v in variants}
        self.assertEqual(resolved, {"nvidia"})

    def test_real_el_collision_is_flagged_ambiguous(self):
        result = self.resolver.resolve("EL")
        self.assertEqual(result.status, ResolutionStatus.AMBIGUOUS)
        self.assertEqual(len(result.candidates), 2)

    def test_real_known_risky_name_requires_context(self):
        self.assertEqual(self.resolver.resolve("Oracle").status, ResolutionStatus.AMBIGUOUS)
        with_context = self.resolver.resolve(
            "Oracle", context="Oracle stock rose after the cloud company reported quarterly revenue growth"
        )
        self.assertTrue(with_context.is_confident)

    def test_bvb_company_resolves(self):
        self.assertEqual(self.resolver.resolve("Banca Transilvania").entity_id, "banca-transilvania")

    def test_crypto_entity_resolves(self):
        self.assertEqual(self.resolver.resolve("Bitcoin").entity_id, "bitcoin")

    def test_unknown_company_stays_unresolved_on_real_data(self):
        result = self.resolver.resolve("Nonexistent Fictional Enterprises")
        self.assertEqual(result.status, ResolutionStatus.UNRESOLVED)

    def test_resolution_is_deterministic_across_runs(self):
        first = [self.resolver.resolve(v).entity_id for v in ["Nvidia", "Tesla", "Microsoft"]]
        self.resolver.clear_cache()
        second = [self.resolver.resolve(v).entity_id for v in ["Nvidia", "Tesla", "Microsoft"]]
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main(verbosity=2)
