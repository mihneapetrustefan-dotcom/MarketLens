"""
src/events/taxonomy.py
---------------------------
Machine-readable event taxonomy (Phase 4, spec §4, §5).

STRUCTURE: Category -> Type -> Subtype, e.g.
    CORPORATE -> PARTNERSHIP -> TECHNOLOGY_PARTNERSHIP
    CORPORATE -> EARNINGS    -> EARNINGS_BEAT

WHY THIS IS DATA, NOT CODE (spec §5: "avoid hardcoding event logic
throughout the application"): the whole taxonomy is a declarative
structure. Adding an event type is a data edit here, never an `if
event_type == ...` added somewhere else. Extraction rules live
alongside their type (see EVENT_TYPE_RULES) for the same reason.

BACKWARD COMPATIBILITY: the existing pipeline's event_detector.py
already emits flat event-type strings (EARNINGS, ACQUISITION,
CEO_CHANGE, LAYOFFS, LAWSUIT, BANKRUPTCY, ...). Those strings are kept
as canonical EventType values here, so anything the old detector
produced maps straight into the new taxonomy without translation, and
event_fusion.py keeps working untouched.
"""

from enum import Enum
from typing import Dict, List, Optional, NamedTuple


class EventCategory(str, Enum):
    CORPORATE = "corporate"
    MARKET = "market"
    MACRO = "macro"
    GEOPOLITICAL = "geopolitical"
    SUPPLY_CHAIN = "supply_chain"
    TECHNOLOGY = "technology"


class EventType(str, Enum):
    # --- CORPORATE ---
    EARNINGS = "earnings"
    REVENUE = "revenue"
    GUIDANCE = "guidance"
    ACQUISITION = "acquisition"
    MERGER = "merger"
    DIVESTITURE = "divestiture"
    PARTNERSHIP = "partnership"
    CONTRACT = "contract"
    PRODUCT_LAUNCH = "product_launch"
    PRODUCT_RECALL = "product_recall"
    MANAGEMENT_CHANGE = "management_change"
    LAYOFFS = "layoffs"
    RESTRUCTURING = "restructuring"
    CAPITAL_RAISE = "capital_raise"
    DEBT_ISSUANCE = "debt_issuance"
    BUYBACK = "buyback"
    DIVIDEND = "dividend"
    BANKRUPTCY = "bankruptcy"
    LITIGATION = "litigation"
    REGULATORY_ACTION = "regulatory_action"
    REGULATORY_APPROVAL = "regulatory_approval"
    REGULATORY_RESTRICTION = "regulatory_restriction"

    # --- MARKET ---
    LARGE_PRICE_MOVE = "large_price_move"
    VOLATILITY_EVENT = "volatility_event"
    VOLUME_ANOMALY = "volume_anomaly"
    TRADING_HALT = "trading_halt"
    LISTING_CHANGE = "listing_change"

    # --- MACRO ---
    INTEREST_RATE_DECISION = "interest_rate_decision"
    INFLATION = "inflation"
    EMPLOYMENT = "employment"
    GDP = "gdp"
    CENTRAL_BANK_DECISION = "central_bank_decision"
    CURRENCY_EVENT = "currency_event"
    COMMODITY_SHOCK = "commodity_shock"

    # --- GEOPOLITICAL ---
    SANCTIONS = "sanctions"
    TARIFFS = "tariffs"
    EXPORT_RESTRICTIONS = "export_restrictions"
    TRADE_RESTRICTIONS = "trade_restrictions"
    CONFLICT = "conflict"
    POLITICAL_DECISION = "political_decision"

    # --- SUPPLY CHAIN ---
    SUPPLY_DISRUPTION = "supply_disruption"
    FACTORY_SHUTDOWN = "factory_shutdown"
    PRODUCTION_INCREASE = "production_increase"
    PRODUCTION_CUT = "production_cut"
    LOGISTICS_DISRUPTION = "logistics_disruption"
    RAW_MATERIAL_CONSTRAINT = "raw_material_constraint"

    # --- TECHNOLOGY ---
    MAJOR_TECHNOLOGY_RELEASE = "major_technology_release"
    AI_MODEL_RELEASE = "ai_model_release"
    SEMICONDUCTOR_DEVELOPMENT = "semiconductor_development"
    PATENT_EVENT = "patent_event"
    CYBERSECURITY_INCIDENT = "cybersecurity_incident"


#: Event type -> its category. Every type belongs to exactly one.
TYPE_TO_CATEGORY: Dict[EventType, EventCategory] = {
    **{t: EventCategory.CORPORATE for t in [
        EventType.EARNINGS, EventType.REVENUE, EventType.GUIDANCE, EventType.ACQUISITION,
        EventType.MERGER, EventType.DIVESTITURE, EventType.PARTNERSHIP, EventType.CONTRACT,
        EventType.PRODUCT_LAUNCH, EventType.PRODUCT_RECALL, EventType.MANAGEMENT_CHANGE,
        EventType.LAYOFFS, EventType.RESTRUCTURING, EventType.CAPITAL_RAISE,
        EventType.DEBT_ISSUANCE, EventType.BUYBACK, EventType.DIVIDEND, EventType.BANKRUPTCY,
        EventType.LITIGATION, EventType.REGULATORY_ACTION, EventType.REGULATORY_APPROVAL,
        EventType.REGULATORY_RESTRICTION,
    ]},
    **{t: EventCategory.MARKET for t in [
        EventType.LARGE_PRICE_MOVE, EventType.VOLATILITY_EVENT, EventType.VOLUME_ANOMALY,
        EventType.TRADING_HALT, EventType.LISTING_CHANGE,
    ]},
    **{t: EventCategory.MACRO for t in [
        EventType.INTEREST_RATE_DECISION, EventType.INFLATION, EventType.EMPLOYMENT,
        EventType.GDP, EventType.CENTRAL_BANK_DECISION, EventType.CURRENCY_EVENT,
        EventType.COMMODITY_SHOCK,
    ]},
    **{t: EventCategory.GEOPOLITICAL for t in [
        EventType.SANCTIONS, EventType.TARIFFS, EventType.EXPORT_RESTRICTIONS,
        EventType.TRADE_RESTRICTIONS, EventType.CONFLICT, EventType.POLITICAL_DECISION,
    ]},
    **{t: EventCategory.SUPPLY_CHAIN for t in [
        EventType.SUPPLY_DISRUPTION, EventType.FACTORY_SHUTDOWN, EventType.PRODUCTION_INCREASE,
        EventType.PRODUCTION_CUT, EventType.LOGISTICS_DISRUPTION, EventType.RAW_MATERIAL_CONSTRAINT,
    ]},
    **{t: EventCategory.TECHNOLOGY for t in [
        EventType.MAJOR_TECHNOLOGY_RELEASE, EventType.AI_MODEL_RELEASE,
        EventType.SEMICONDUCTOR_DEVELOPMENT, EventType.PATENT_EVENT,
        EventType.CYBERSECURITY_INCIDENT,
    ]},
}

#: Optional third level. Deliberately sparse — subtypes exist only
#: where they carry real analytical meaning, not for symmetry's sake.
TYPE_TO_SUBTYPES: Dict[EventType, List[str]] = {
    EventType.EARNINGS: ["earnings_beat", "earnings_miss", "earnings_inline"],
    EventType.GUIDANCE: ["guidance_raised", "guidance_cut", "guidance_maintained"],
    EventType.PARTNERSHIP: ["technology_partnership", "distribution_partnership", "research_partnership"],
    EventType.MANAGEMENT_CHANGE: ["ceo_change", "cfo_change", "board_change"],
    EventType.REGULATORY_ACTION: ["investigation", "fine", "consent_order"],
    EventType.LARGE_PRICE_MOVE: ["price_surge", "price_drop"],
}


class ExtractionRule(NamedTuple):
    """
    A deterministic keyword rule for one event type (extraction Tier 2
    — see extractor.py). `phrases` are matched case-insensitively as
    whole phrases; `negations` veto a match when present, which is how
    obvious false positives are suppressed without any model call.
    """
    event_type: EventType
    phrases: List[str]
    negations: List[str] = []


#: Deterministic rules, kept beside the taxonomy they classify.
#: DELIBERATELY CONSERVATIVE: high-precision phrases only. A missed
#: event costs a later tier one more look; a wrongly-typed event
#: pollutes the factual record, which is worse.
EVENT_TYPE_RULES: List[ExtractionRule] = [
    ExtractionRule(EventType.EARNINGS, ["reports quarterly", "quarterly results", "quarterly earnings",
                                         "posts quarterly", "q1 results", "q2 results", "q3 results", "q4 results",
                                         "full year results", "earnings report"]),
    ExtractionRule(EventType.GUIDANCE, ["raises guidance", "cuts guidance", "lowers guidance",
                                         "raises full year", "guidance for the year", "updates guidance"]),
    ExtractionRule(EventType.ACQUISITION, ["to acquire", "acquires", "acquisition of", "agrees to buy",
                                            "agreed to purchase", "takeover bid"]),
    ExtractionRule(EventType.MERGER, ["merger with", "to merge with", "merger agreement"]),
    ExtractionRule(EventType.DIVESTITURE, ["divests", "sells its stake", "spin off", "spins off", "divestiture"]),
    ExtractionRule(EventType.PARTNERSHIP, ["partnership with", "partners with", "joint venture",
                                            "strategic alliance", "expanded partnership", "teams up with"]),
    ExtractionRule(EventType.CONTRACT, ["wins contract", "awarded a contract", "signs contract",
                                         "secures order", "wins order"]),
    ExtractionRule(EventType.PRODUCT_LAUNCH, ["launches", "unveils", "introduces new", "announces the launch"],
                    negations=["launches investigation", "launches lawsuit"]),
    ExtractionRule(EventType.PRODUCT_RECALL, ["recalls", "product recall", "issues recall"]),
    ExtractionRule(EventType.MANAGEMENT_CHANGE, ["steps down", "appoints new", "names new chief",
                                                  "resigns as", "new ceo", "chief executive resigns"]),
    ExtractionRule(EventType.LAYOFFS, ["lay off", "layoffs", "job cuts", "cuts jobs", "workforce reduction"]),
    ExtractionRule(EventType.RESTRUCTURING, ["restructuring plan", "restructures", "reorganization plan"]),
    ExtractionRule(EventType.CAPITAL_RAISE, ["raises capital", "share offering", "equity offering",
                                              "capital increase", "ipo pricing"]),
    ExtractionRule(EventType.DEBT_ISSUANCE, ["bond offering", "issues bonds", "debt offering", "notes offering"]),
    ExtractionRule(EventType.BUYBACK, ["share buyback", "buyback program", "repurchase program",
                                        "share repurchase"]),
    ExtractionRule(EventType.DIVIDEND, ["declares dividend", "dividend increase", "raises dividend",
                                         "cuts dividend", "special dividend"]),
    ExtractionRule(EventType.BANKRUPTCY, ["files for bankruptcy", "chapter 11", "insolvency proceedings"]),
    ExtractionRule(EventType.LITIGATION, ["lawsuit", "sues", "legal action against", "class action"]),
    ExtractionRule(EventType.REGULATORY_ACTION, ["regulatory investigation", "probe into", "antitrust investigation",
                                                  "fined by", "regulator fines"]),
    ExtractionRule(EventType.REGULATORY_APPROVAL, ["regulatory approval", "approved by the fda",
                                                    "wins approval", "cleared by regulators"]),
    ExtractionRule(EventType.REGULATORY_RESTRICTION, ["banned by", "restriction imposed", "license revoked"]),

    ExtractionRule(EventType.TRADING_HALT, ["trading halted", "halts trading", "trading suspension"]),
    ExtractionRule(EventType.LISTING_CHANGE, ["delisted", "to be delisted", "lists on", "begins trading on"]),

    ExtractionRule(EventType.INTEREST_RATE_DECISION, ["raises interest rates", "cuts interest rates",
                                                       "holds interest rates", "rate decision", "basis point hike",
                                                       "basis point cut"]),
    ExtractionRule(EventType.INFLATION, ["inflation rose", "inflation fell", "consumer price index",
                                          "cpi reading", "inflation data"]),
    ExtractionRule(EventType.EMPLOYMENT, ["unemployment rate", "nonfarm payrolls", "jobs report",
                                           "employment data"]),
    ExtractionRule(EventType.GDP, ["gross domestic product", "gdp growth", "gdp contracted", "gdp data"]),
    ExtractionRule(EventType.CENTRAL_BANK_DECISION, ["federal reserve", "european central bank",
                                                      "central bank decision", "fomc"]),
    ExtractionRule(EventType.COMMODITY_SHOCK, ["oil prices surge", "oil prices plunge", "commodity shock",
                                                "supply glut"]),

    ExtractionRule(EventType.SANCTIONS, ["imposes sanctions", "sanctions on", "sanctioned by"]),
    ExtractionRule(EventType.TARIFFS, ["imposes tariffs", "tariffs on", "tariff increase"]),
    ExtractionRule(EventType.EXPORT_RESTRICTIONS, ["export restrictions", "export controls", "export ban"]),
    ExtractionRule(EventType.TRADE_RESTRICTIONS, ["trade restrictions", "trade barriers", "import ban"]),

    ExtractionRule(EventType.SUPPLY_DISRUPTION, ["supply disruption", "supply chain disruption", "shortage of"]),
    ExtractionRule(EventType.FACTORY_SHUTDOWN, ["factory shutdown", "halts production at", "plant closure",
                                                 "suspends production"]),
    ExtractionRule(EventType.PRODUCTION_INCREASE, ["increase production", "boosts output", "raises output",
                                                    "expand production"]),
    ExtractionRule(EventType.PRODUCTION_CUT, ["cuts production", "reduces output", "production cut",
                                               "output quota cut"]),
    ExtractionRule(EventType.LOGISTICS_DISRUPTION, ["shipping delays", "port congestion", "logistics disruption"]),

    ExtractionRule(EventType.AI_MODEL_RELEASE, ["releases ai model", "launches ai model", "new ai model",
                                                 "unveils ai model"]),
    ExtractionRule(EventType.SEMICONDUCTOR_DEVELOPMENT, ["new chip", "chip production", "semiconductor plant",
                                                          "advanced node", "fab expansion"]),
    ExtractionRule(EventType.PATENT_EVENT, ["patent granted", "patent infringement", "files patent"]),
    ExtractionRule(EventType.CYBERSECURITY_INCIDENT, ["data breach", "cyberattack", "hacked", "ransomware attack",
                                                       "security breach"]),
]


def category_for(event_type: EventType) -> Optional[EventCategory]:
    """Return the category an event type belongs to, or None if unmapped (never raises)."""
    return TYPE_TO_CATEGORY.get(event_type)


def subtypes_for(event_type: EventType) -> List[str]:
    """Return the valid subtypes for an event type — empty list if it has none."""
    return list(TYPE_TO_SUBTYPES.get(event_type, []))


def is_valid_subtype(event_type: EventType, subtype: Optional[str]) -> bool:
    """Whether `subtype` is declared for `event_type`. None is always valid (subtype is optional)."""
    if subtype is None:
        return True
    return subtype in TYPE_TO_SUBTYPES.get(event_type, [])


def types_in_category(category: EventCategory) -> List[EventType]:
    """Every event type belonging to a category."""
    return [t for t, c in TYPE_TO_CATEGORY.items() if c == category]


def from_legacy_string(legacy: str) -> Optional[EventType]:
    """
    Map an event-type string produced by the EXISTING event_detector.py
    (e.g. "EARNINGS", "CEO_CHANGE") onto a canonical EventType.

    Returns None for anything unrecognized — deliberately, rather than
    guessing, so an unmapped legacy type is visible instead of silently
    mis-typed. CEO_CHANGE is special-cased onto MANAGEMENT_CHANGE,
    where it belongs in the new hierarchy (as its ceo_change subtype).
    """
    if not legacy:
        return None
    key = legacy.strip().lower()
    aliases = {
        "ceo_change": EventType.MANAGEMENT_CHANGE,
        "cfo_change": EventType.MANAGEMENT_CHANGE,
        "guidance_up": EventType.GUIDANCE,
        "guidance_down": EventType.GUIDANCE,
        "cyberattack": EventType.CYBERSECURITY_INCIDENT,
        "lawsuit": EventType.LITIGATION,
    }
    if key in aliases:
        return aliases[key]
    try:
        return EventType(key)
    except ValueError:
        return None
