"""
src/events/retention.py
-----------------------------------------------
Which derived event rows a rebuild may delete, and which it must keep.

THE FAILURE THIS REPLACES
-------------------------
`scripts/populate_events.py` and `scripts/fuse_events.py` rebuild
derived tables and then swept away everything the current run did not
re-produce:

    stale = existing_ids - current_ids      # delete all of these

That set difference conflates four completely different situations.
`scripts/archive_old_articles.py --keep-days 60` moves old articles
out of the live `articles` table; `populate_events.py` reads
`FROM articles`, so it stops producing those articles' events; the
sweep then deleted them. `fuse_events.py` rebuilds `canonical_events`
from `events`, so the canonical rows went too. Measured outcome
(docs/MODEL_CLUSTER_INSUFFICIENCY_AUDIT_2026_11_01.md): `events`
846 -> 153, `canonical_events` 581 -> 129, against 1,295
`research_observations` that are never deleted — so ~90% of the
research corpus could no longer resolve its own event, and seven
article- and event-derived features fell from ~100% coverage to
8.6-10.3%.

THE RULE
--------
    ABSENCE FROM A REBUILD IS NOT EVIDENCE OF INVALIDITY.

A row is deleted only on POSITIVE evidence that it is invalid,
superseded, or provenance-less. "This run did not regenerate it" is
not such evidence — the run may not have looked, may have looked at a
restricted window, or may have been unable to see the source article
at all. Where the data cannot establish invalidity, the row is kept
and reported as UNRESOLVED, so a real extractor regression stays
visible instead of being silently absorbed either way.

This is deliberately asymmetric. Keeping a row that should have gone
costs some rows in a derived table, which the next rebuild can still
remove once real evidence exists. Deleting a row that should have
stayed destroys the only link between a research observation and the
evidence it was built from, and no later run can put it back.

WHAT STILL GETS CLEANED UP
--------------------------
Both legitimate cases survive, each on its own positive signal:

  * no resolvable source article anywhere -> the provenance is gone,
    so the row can never be re-derived or checked;
  * an id that the current deterministic scheme cannot produce for
    its own (article, event_type) -> a remnant of the pre-uuid4
    migration, which is exactly what the original sweep was written
    for, and is proved by recomputation rather than inferred.

A third case was tried and withdrawn: deleting rows with a terminal
`events.status`, or rows named by another row's
`supersedes_event_id`. Those are HISTORICAL RECORDS and are now kept
as `KEEP_TERMINAL_HISTORY`. The reasoning is at that branch; the short
version is that `EventRepository.supersede` states the invariant
("historical states are never destroyed") and that deleting such a row
demonstrably dangles a research observation's event reference.

UNRESOLVED ARCHITECTURAL RISK: THE CANONICAL ID IS MUTABLE
--------------------------------------------------------------
Not introduced here, not fixed here, and the reason the policy above
is as conservative as it is.

`scripts/fuse_events.canonical_id_for` derives a canonical event's id
by hashing the SET OF REPORT IDS it fuses. So the id changes whenever
that set changes -- when a report is deleted, and equally when a new
report JOINS the group, which `fuse_events`' own docstring states:
"if a later run attaches an additional report to a group, the group's
id changes."

Three durable things key off that id and are never rewritten:

    canonical_events.canonical_event_id
    research_observations.event_id      (and observation_id_for(...))
    event_studies.event_id              (and study_id_for(...))

So any change to a group's membership leaves those references pointing
at an id that no longer exists. `compute_features.py:232` LEFT JOINs
`research_observations` to `canonical_events` on it, and a dangling
reference becomes NULL event metadata -- the exact failure
docs/MODEL_CLUSTER_INSUFFICIENCY_AUDIT_2026_11_01.md measured.

CONSEQUENCE FOR ANYONE PLANNING EVENT RE-DERIVATION. Re-deriving the
reports whose articles were archived would ADD them back to existing
groups, churning the ids of every group they join. That must be
solved -- a stable canonical key, or a persisted old-id -> new-id
mapping the dependent tables can be read through -- BEFORE
re-derivation, or re-derivation will dangle more references than the
original deletion ever did. Nothing in this module attempts it.

WITH NO FLAGS, NOTHING CHANGES
------------------------------
When `populate_events.py` scans every eligible article and
`fuse_events.py` loads every report — which is how `pipeline.yml`
invokes them — the only behavioural difference is that archived-source
rows and unresolved rows are now kept. `--limit` and `--since`
restrict what a run can see, and rows outside that window are no
longer treated as deleted evidence.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set

#: `events.status` values that assert the row is no longer a live
#: claim. Taken from EventStatus (spec §13); DETECTED, CONFIRMED and
#: UPDATED are live states and are NOT in this set.
#:
#: THIS IS A FILTERING CONSTANT, NOT A DELETION TRIGGER. The first
#: version of this module deleted on it, and
#: docs/HISTORICAL_DATA_RETENTION_REVIEW_2026_11_01.md showed why that
#: was wrong -- see `KEEP_TERMINAL_HISTORY` below. It is kept because
#: callers that want to EXCLUDE these rows from a view legitimately
#: need the set, exactly as `EventRepository.list(include_superseded=
#: False)` and `EventRepository.count(include_superseded=False)` do.
TERMINAL_STATUSES = frozenset({"superseded", "cancelled", "retracted"})

# ---------------------------------------------------------------- verdicts

#: Deleted — positive evidence of invalidity.
DELETE_NO_PROVENANCE = "delete_no_provenance"
DELETE_ARTICLE_GONE = "delete_article_gone"
DELETE_ID_SCHEME_REMNANT = "delete_id_scheme_remnant"
DELETE_RECOMPUTED = "delete_recomputed"
DELETE_DANGLING_REPORTS = "delete_dangling_reports"

#: Kept — no such evidence.
KEEP_ARCHIVED_SOURCE = "keep_archived_source"
KEEP_OUT_OF_SCAN_SCOPE = "keep_out_of_scan_scope"
KEEP_UNRESOLVED = "keep_unresolved"
KEEP_TERMINAL_HISTORY = "keep_terminal_history"

DELETE_VERDICTS = frozenset({
    DELETE_NO_PROVENANCE, DELETE_ARTICLE_GONE, DELETE_ID_SCHEME_REMNANT,
    DELETE_RECOMPUTED, DELETE_DANGLING_REPORTS,
})

#: Human-readable, for the scripts' own output.
VERDICT_LABELS = {
    DELETE_NO_PROVENANCE: "fara provenienta (nicio legatura la articol)",
    DELETE_ARTICLE_GONE: "articolul sursa lipseste si din live si din arhiva",
    DELETE_ID_SCHEME_REMNANT: "id din schema veche (rest de migrare uuid4)",
    DELETE_RECOMPUTED: "vedere recalculata din aceleasi rapoarte",
    DELETE_DANGLING_REPORTS: "trimite la rapoarte care nu mai exista",
    KEEP_TERMINAL_HISTORY: "PASTRAT: stare istorica (inlocuit/anulat/retras)",
    KEEP_ARCHIVED_SOURCE: "PASTRAT: articolul sursa e arhivat",
    KEEP_OUT_OF_SCAN_SCOPE: "PASTRAT: in afara domeniului scanat",
    KEEP_UNRESOLVED: "PASTRAT NEREZOLVAT: articol viu, scanat, dar neregenerat",
}


@dataclass
class RetentionDecision:
    """One row's fate, with the reason and the evidence behind it."""
    row_id: str
    verdict: str
    detail: str = ""

    @property
    def deletes(self) -> bool:
        return self.verdict in DELETE_VERDICTS


@dataclass
class RetentionPlan:
    """
    Every decision for one rebuild, grouped so the caller can act on
    it and report it without recomputing anything.
    """
    decisions: List[RetentionDecision] = field(default_factory=list)

    @property
    def to_delete(self) -> List[str]:
        return [d.row_id for d in self.decisions if d.deletes]

    @property
    def to_keep(self) -> List[str]:
        return [d.row_id for d in self.decisions if not d.deletes]

    @property
    def unresolved(self) -> List[RetentionDecision]:
        """
        Rows kept because invalidity could not be established. These
        are the ones worth a human look: a genuine extractor
        regression lands here rather than being deleted.
        """
        return [d for d in self.decisions if d.verdict == KEEP_UNRESOLVED]

    def counts(self) -> Dict[str, int]:
        return dict(Counter(d.verdict for d in self.decisions))

    def summary_lines(self) -> List[str]:
        out = []
        for verdict, count in sorted(self.counts().items(), key=lambda kv: (-kv[1], kv[0])):
            out.append(f"  {count:>6,}  {VERDICT_LABELS.get(verdict, verdict)}")
        return out


# ------------------------------------------------------- event reports

def _source_article_for(conn: sqlite3.Connection, event_id: str) -> Optional[str]:
    """
    The article an event report was extracted from.

    `article_event_links` with relation 'creates' is the authoritative
    link, written by `populate_events.link_article`. `event_evidence`
    is consulted as a fallback because it carries the same article id
    and survives independently. Ordered by article_id so a row with
    several links resolves the same way on every run.
    """
    row = conn.execute(
        "SELECT article_id FROM article_event_links "
        "WHERE event_id = ? AND relation = 'creates' "
        "ORDER BY article_id LIMIT 1", (event_id,)).fetchone()
    if row and row[0]:
        return row[0]
    row = conn.execute(
        "SELECT article_id FROM article_event_links WHERE event_id = ? "
        "ORDER BY article_id LIMIT 1", (event_id,)).fetchone()
    if row and row[0]:
        return row[0]
    row = conn.execute(
        "SELECT article_id FROM event_evidence WHERE event_id = ? "
        "ORDER BY article_id LIMIT 1", (event_id,)).fetchone()
    return row[0] if row and row[0] else None


def plan_event_report_retention(
    conn: sqlite3.Connection,
    *,
    produced_ids: Set[str],
    scanned_article_ids: Optional[Set[str]],
    live_article_ids: Optional[Set[str]] = None,
    archive_contains: Callable[[str], bool] = lambda _article_id: False,
    report_id_for: Optional[Callable[[str, str], str]] = None,
) -> RetentionPlan:
    """
    Decide what to do with `events` rows this run did not re-produce.

    `produced_ids`        ids the current run generated.
    `scanned_article_ids` the articles the run actually read, or None
                          when that is unknown — None means "assume
                          nothing was scanned", the conservative
                          reading, so no row is judged by absence.
    `live_article_ids`    ids still in `articles`; queried if omitted.
    `archive_contains`    callable answering "was this archived?".
    `report_id_for`       the current deterministic id function. When
                          given, a row whose id it cannot reproduce is
                          a pre-migration remnant. When omitted, that
                          check is skipped rather than guessed at.

    Rows in `produced_ids` are not examined: a row the run produced is
    being rewritten, not retired.
    """
    plan = RetentionPlan()

    existing = conn.execute("SELECT event_id, event_type, status, supersedes_event_id "
                            "FROM events ORDER BY event_id").fetchall()
    if live_article_ids is None:
        live_article_ids = {r[0] for r in conn.execute("SELECT article_id FROM articles")}
    scanned = scanned_article_ids if scanned_article_ids is not None else set()

    # Which rows another row claims to replace. Read for REPORTING
    # only: a successor's `supersedes_event_id` is a pointer INTO
    # history, so following it to delete the predecessor would destroy
    # the thing the pointer exists to reach.
    superseded_by = {}
    for row in existing:
        if row[3]:
            superseded_by[row[3]] = row[0]

    for event_id, event_type, status, _supersedes in existing:
        if event_id in produced_ids:
            continue

        article_id = _source_article_for(conn, event_id)
        if not article_id:
            plan.decisions.append(RetentionDecision(
                event_id, DELETE_NO_PROVENANCE, "no article_event_links or event_evidence row"))
            continue

        if report_id_for is not None and event_type:
            expected = report_id_for(article_id, event_type)
            if expected != event_id:
                plan.decisions.append(RetentionDecision(
                    event_id, DELETE_ID_SCHEME_REMNANT,
                    f"current scheme would mint {expected} for ({article_id}, {event_type})"))
                continue

        # A TERMINAL STATUS IS A HISTORICAL RECORD, NOT A DELETE ORDER.
        #
        # This module's first version deleted on `status` in
        # TERMINAL_STATUSES and on being named by another row's
        # `supersedes_event_id`. Both were wrong, and the review
        # (docs/HISTORICAL_DATA_RETENTION_REVIEW_2026_11_01.md) gave
        # two independent reasons:
        #
        #   1. `EventRepository.supersede` states the invariant
        #      outright -- "the old one is marked SUPERSEDED rather
        #      than deleted -- historical states are never destroyed"
        #      -- and the repository offers `include_superseded` on
        #      `list` and `count` precisely so callers can FILTER
        #      these rows out of a view while the rows remain.
        #      Terminal status means "no longer current", not
        #      "safe to destroy".
        #
        #   2. Deleting one measurably breaks a historical
        #      relationship. `canonical_id_for` hashes the set of
        #      reports in a group, so removing a report changes the
        #      group's canonical id; `research_observations.event_id`
        #      and `event_studies.event_id` both reference that id and
        #      are never rewritten, so they dangle -- and
        #      compute_features.py:232 LEFT JOINs on exactly that
        #      reference, which is the NULL-feature failure this whole
        #      module exists to prevent.
        #
        # Checked AFTER provenance and the id scheme on purpose. A row
        # with no resolvable article, or an id the current writer can
        # never mint, cannot be re-derived or verified whatever its
        # status says -- so the two legitimate cleanups keep
        # precedence, and a terminal status does not become a way to
        # immortalise an orphan.
        if status and str(status).lower() in TERMINAL_STATUSES:
            detail = f"status={status}"
            if event_id in superseded_by:
                detail += f", superseded by {superseded_by[event_id]}"
            plan.decisions.append(RetentionDecision(
                event_id, KEEP_TERMINAL_HISTORY, detail))
            continue
        if event_id in superseded_by:
            # Named as the predecessor by a successor row, without a
            # terminal status of its own. Still history, still kept.
            plan.decisions.append(RetentionDecision(
                event_id, KEEP_TERMINAL_HISTORY,
                f"superseded by {superseded_by[event_id]}"))
            continue

        if article_id in live_article_ids:
            if article_id in scanned:
                # The run read this article and did not extract this
                # event. That is a DISAGREEMENT, not a disproof: the
                # extractor's thresholds, its classification or the
                # article's own text may have changed, and none of
                # those makes the recorded report false. Kept, and
                # surfaced for a human.
                plan.decisions.append(RetentionDecision(
                    event_id, KEEP_UNRESOLVED,
                    f"article {article_id} was scanned but produced no such report"))
            else:
                plan.decisions.append(RetentionDecision(
                    event_id, KEEP_OUT_OF_SCAN_SCOPE,
                    f"article {article_id} is live but outside this run's scan"))
            continue

        if archive_contains(article_id):
            plan.decisions.append(RetentionDecision(
                event_id, KEEP_ARCHIVED_SOURCE, f"article {article_id} is archived"))
            continue

        plan.decisions.append(RetentionDecision(
            event_id, DELETE_ARTICLE_GONE,
            f"article {article_id} is in neither the live table nor the archive"))

    return plan


# ---------------------------------------------------- canonical events

def plan_canonical_retention(
    conn: sqlite3.Connection,
    *,
    produced_ids: Set[str],
    loaded_report_ids: Optional[Set[str]] = None,
) -> RetentionPlan:
    """
    Decide what to do with `canonical_events` rows this run did not
    re-produce.

    Canonical events are a DERIVED VIEW over `events`, keyed on the set
    of reports they fuse (`fuse_events.canonical_id_for`). So when a
    report joins or leaves a group the id legitimately changes, and
    pruning the old row is correct — the positive evidence being that
    the same inputs were fully re-fused into a different grouping.

    `loaded_report_ids` is the set of reports this run actually fused,
    or None for "all of them". The distinction matters under
    `--limit`: a canonical row whose reports still exist in `events`
    but were not loaded was not re-examined, so its absence from the
    output says nothing, and it is kept.
    """
    plan = RetentionPlan()

    existing = [r[0] for r in conn.execute(
        "SELECT canonical_event_id FROM canonical_events ORDER BY canonical_event_id")]
    if not existing:
        return plan

    reports_by_canonical: Dict[str, List[str]] = {}
    for canonical_id, report_id in conn.execute(
            "SELECT canonical_event_id, report_id FROM canonical_event_reports "
            "ORDER BY canonical_event_id, report_id"):
        reports_by_canonical.setdefault(canonical_id, []).append(report_id)

    surviving_reports = {r[0] for r in conn.execute("SELECT event_id FROM events")}

    for canonical_id in existing:
        if canonical_id in produced_ids:
            continue
        reports = reports_by_canonical.get(canonical_id, [])
        if not reports:
            plan.decisions.append(RetentionDecision(
                canonical_id, DELETE_NO_PROVENANCE, "no canonical_event_reports rows"))
            continue

        still_present = [r for r in reports if r in surviving_reports]
        if not still_present:
            plan.decisions.append(RetentionDecision(
                canonical_id, DELETE_DANGLING_REPORTS,
                f"all {len(reports)} report(s) have left `events`"))
            continue

        unexamined = [r for r in still_present
                      if loaded_report_ids is not None and r not in loaded_report_ids]
        if unexamined:
            plan.decisions.append(RetentionDecision(
                canonical_id, KEEP_OUT_OF_SCAN_SCOPE,
                f"{len(unexamined)} of {len(reports)} report(s) were not loaded this run"))
            continue

        plan.decisions.append(RetentionDecision(
            canonical_id, DELETE_RECOMPUTED,
            f"{len(still_present)} report(s) re-fused into a different grouping"))

    return plan


def delete_event_reports(conn: sqlite3.Connection, event_ids: Sequence[str]) -> int:
    """
    Remove event report rows and their children.

    Same tables as the original sweep, in the same order. `articles`
    and `article_entities` are not touched.
    """
    removed = 0
    for event_id in event_ids:
        for table in ("events", "event_participants", "event_evidence",
                      "event_instruments", "event_sectors"):
            conn.execute(f"DELETE FROM {table} WHERE event_id = ?", (event_id,))
        conn.execute("DELETE FROM article_event_links WHERE event_id = ?", (event_id,))
        removed += 1
    return removed


def delete_canonical_events(conn: sqlite3.Connection, canonical_ids: Iterable[str]) -> int:
    """Remove canonical rows and their children. `events` is untouched."""
    removed = 0
    for canonical_id in canonical_ids:
        for table in ("canonical_events", "canonical_event_participants",
                      "canonical_event_reports", "fusion_contradictions",
                      "fusion_timeline", "fusion_review_cases"):
            conn.execute(f"DELETE FROM {table} WHERE canonical_event_id = ?", (canonical_id,))
        removed += 1
    return removed
