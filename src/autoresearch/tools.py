"""
src/autoresearch/tools.py
-----------------------------------
Phase 23 §49, §50, §51, §69, §75 — the controlled surface a research
agent is allowed to touch.

WHY THIS EXISTS WHEN THERE IS NO AGENT
------------------------------------------
No LLM is wired into this system (§47 permits one; it does not require
one). This module is still the right thing to build, for two reasons.

First, it is the permission boundary. Whether the caller is an LLM, a
script, or a person at a REPL, the tools here are the only sanctioned
way into the research layer, and each one requires a named permission.
Building the boundary before the agent means the agent arrives into a
constrained space rather than having constraints retrofitted around it.

Second, it makes the absent capabilities explicit. There is no
`modify_production()`, no `submit_order()`, no `set_risk()`, no
`promote()` that succeeds. §51 lists what an agent must never reach;
the enforcement is that no such function exists, and
`tests/autoresearch/test_boundary_and_safety.py` parses this package
to prove it.

WHAT A TOOL MAY NOT DO
--------------------------
- run arbitrary code (§29, §49) — every evaluator is a registered name
- reach the filesystem or the shell (§69, §75)
- read credentials (§75) — nothing here touches config or environment
- write a production table (§33, §34)
- delete anything (§42) — negative results are permanent

THE PERMISSION MODEL IS DELIBERATELY BORING
-----------------------------------------------
A `Grant` is a frozen set of permission names. A tool declares what it
needs; calling it without that permission raises. There is no
hierarchy, no inheritance, no wildcard, and no way to widen a grant
from inside a tool — all of which are places where permission systems
usually leak.

`PROMOTE_CANDIDATE` is defined and cannot be granted by
`Grant.researcher()` or `Grant.read_only()`. §50 says a future phase
may need it; this phase must not have it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence

from src.domain.autoresearch_models import (
    Actor, ResearchBudget, ResearchHypothesis,
)
from src.autoresearch import (
    audit, candidates as candidate_registry, cycle as cycle_layer, governance,
    hypotheses as hypothesis_layer, observations as observation_layer,
    prioritization, questions as question_layer, queue as queue_layer,
)


# ======================================================================
# Permissions
# ======================================================================

READ_RESEARCH = "READ_RESEARCH"
CREATE_HYPOTHESIS = "CREATE_HYPOTHESIS"
CREATE_EXPERIMENT = "CREATE_EXPERIMENT"
RUN_EXPERIMENT = "RUN_EXPERIMENT"
READ_RESULTS = "READ_RESULTS"

#: Defined, never granted in this phase (§50, §55). Phase 24 may make
#: it grantable; until then the only holder of this permission is a
#: human running `scripts/promote_model.py`, which is outside this
#: package entirely.
PROMOTE_CANDIDATE = "PROMOTE_CANDIDATE"

ALL_PERMISSIONS = frozenset({
    READ_RESEARCH, CREATE_HYPOTHESIS, CREATE_EXPERIMENT, RUN_EXPERIMENT,
    READ_RESULTS,
})


class PermissionDenied(Exception):
    """A tool was called without the permission it declares."""


@dataclass(frozen=True)
class Grant:
    """What a caller is allowed to do, and who they are."""
    permissions: FrozenSet[str]
    actor: Actor = Actor.SYSTEM
    label: str = ""

    @classmethod
    def read_only(cls, actor: Actor = Actor.SYSTEM, label: str = "") -> "Grant":
        return cls(frozenset({READ_RESEARCH, READ_RESULTS}), actor, label)

    @classmethod
    def researcher(cls, actor: Actor = Actor.SYSTEM, label: str = "") -> "Grant":
        """
        The widest grant this phase can issue.

        Note what it does NOT contain: `PROMOTE_CANDIDATE`. A researcher
        may propose, test and conclude. Turning a conclusion into a
        production change is a different act by a different party.
        """
        return cls(ALL_PERMISSIONS, actor, label)

    def allows(self, permission: str) -> bool:
        return permission in self.permissions

    def require(self, permission: str, tool: str) -> None:
        if permission == PROMOTE_CANDIDATE:
            raise PermissionDenied(
                "%s requires %s, which Phase 23 cannot grant to anyone. "
                "Promotion is a human decision through the Phase 18 gate."
                % (tool, permission))
        if not self.allows(permission):
            raise PermissionDenied(
                "%s requires %s; this grant holds {%s}."
                % (tool, permission, ", ".join(sorted(self.permissions)) or "nothing"))


# ======================================================================
# The toolbox
# ======================================================================

@dataclass
class ToolSpec:
    name: str
    permission: str
    description: str
    writes: bool = False


_TOOLS: Dict[str, ToolSpec] = {}


def _tool(spec: ToolSpec):
    _TOOLS[spec.name] = spec
    def decorator(function):
        function.spec = spec
        return function
    return decorator


def registered() -> List[ToolSpec]:
    return [_TOOLS[name] for name in sorted(_TOOLS)]


# -- reading -------------------------------------------------------

@_tool(ToolSpec("search_memory", READ_RESEARCH,
                "Memory patterns above or below the base rate."))
def search_memory(conn: sqlite3.Connection, grant: Grant, *,
                  limit: int = 10) -> List[Dict[str, Any]]:
    grant.require(READ_RESEARCH, "search_memory")
    try:
        found = observation_layer.recurring_success(conn, limit=limit)
    except observation_layer.DetectorUnavailable as exc:
        return [{"unavailable": str(exc)}]
    return [o.as_dict() for o in found]


@_tool(ToolSpec("search_errors", READ_RESEARCH,
                "Error types the record attributes repeatedly."))
def search_errors(conn: sqlite3.Connection, grant: Grant, *,
                  limit: int = 10) -> List[Dict[str, Any]]:
    grant.require(READ_RESEARCH, "search_errors")
    try:
        found = observation_layer.recurring_error(conn, limit=limit)
    except observation_layer.DetectorUnavailable as exc:
        return [{"unavailable": str(exc)}]
    return [o.as_dict() for o in found]


@_tool(ToolSpec("search_experiments", READ_RESULTS,
                "Phase 22 experiments and how they ended."))
def search_experiments(conn: sqlite3.Connection, grant: Grant, *,
                       limit: int = 25) -> List[Dict[str, Any]]:
    grant.require(READ_RESULTS, "search_experiments")
    from src.experiments import api as experiment_api
    return experiment_api.list_experiments(conn, limit=limit)


@_tool(ToolSpec("search_conclusions", READ_RESULTS,
                "Research conclusions, including the negative ones."))
def search_conclusions(conn: sqlite3.Connection, grant: Grant, *,
                       limit: int = 50) -> List[Dict[str, Any]]:
    grant.require(READ_RESULTS, "search_conclusions")
    from src.autoresearch import api as research_api
    return research_api.conclusions(conn, limit=limit)


@_tool(ToolSpec("research_context", READ_RESEARCH,
                "What is already known about a claim (§15)."))
def research_context(conn: sqlite3.Connection, grant: Grant, *,
                     hypothesis: ResearchHypothesis) -> Dict[str, Any]:
    grant.require(READ_RESEARCH, "research_context")
    return hypothesis_layer.research_context(conn, hypothesis)


# -- creating ------------------------------------------------------

@_tool(ToolSpec("create_hypothesis", CREATE_HYPOTHESIS,
                "Form a falsifiable claim from a testable question.",
                writes=True))
def create_hypothesis(conn: sqlite3.Connection, grant: Grant, *,
                      question, observation) -> Dict[str, Any]:
    """
    Form and store a hypothesis.

    The quality gate and the leakage check both run here, so a caller
    cannot bypass them by using the tool interface instead of the
    module. That is the point of having a tool interface at all.
    """
    grant.require(CREATE_HYPOTHESIS, "create_hypothesis")
    hypothesis = hypothesis_layer.from_question(question, observation)
    duplicate = hypothesis_layer.find_duplicate(conn, hypothesis)
    if duplicate:
        return {"refused": "this claim has already been tested",
                "existing": duplicate}
    hypothesis_layer.save(conn, [hypothesis])
    audit.record(conn, actor=grant.actor, action="create_hypothesis",
                 hypothesis_id=hypothesis.hypothesis_id,
                 decision="created", reason=hypothesis.statement[:400])
    return hypothesis.as_dict()


@_tool(ToolSpec("run_research_cycle", RUN_EXPERIMENT,
                "One bounded pass of the research loop.", writes=True))
def run_research_cycle(conn: sqlite3.Connection, grant: Grant, *,
                       budget: Optional[ResearchBudget] = None,
                       apply: bool = False) -> Dict[str, Any]:
    """
    Run a cycle under a budget.

    `RUN_EXPERIMENT` is required even for a dry run, because a dry run
    still reads the whole record and a caller with only read
    permissions should not be able to trigger that work.
    """
    grant.require(RUN_EXPERIMENT, "run_research_cycle")
    return cycle_layer.run_cycle(conn, budget=budget, apply=apply,
                                 trigger="tool", actor=grant.actor)


@_tool(ToolSpec("compare_results", READ_RESULTS,
                "Two conclusions side by side, without ranking them."))
def compare_results(conn: sqlite3.Connection, grant: Grant, *,
                    conclusion_ids: Sequence[str]) -> Dict[str, Any]:
    """
    Lay conclusions beside each other. Deliberately does NOT rank.

    Ranking by effect size is the selection bias the phase exists to
    expose: the top of a list of forty experiments is where the noise
    collects. The comparison lists; the reader decides.
    """
    grant.require(READ_RESULTS, "compare_results")
    from src.autoresearch import api as research_api
    rows = [research_api.conclusion_detail(conn, cid) for cid in conclusion_ids]
    return {
        "conclusions": [row for row in rows if row],
        "note": ("listed in the order requested, not ranked. Selecting the "
                 "best of several results is how a research record "
                 "manufactures a finding."),
    }


# -- the refusals --------------------------------------------------

@_tool(ToolSpec("promote_candidate", PROMOTE_CANDIDATE,
                "Always refuses. Present so the refusal is findable."))
def promote_candidate(conn: sqlite3.Connection, grant: Grant, *,
                      candidate_id: str) -> Dict[str, Any]:
    """
    Never succeeds.

    It exists because somebody looking for how a candidate reaches
    production will search for this name, and finding an explicit
    refusal is far better than finding nothing and assuming the
    mechanism is elsewhere.
    """
    grant.require(PROMOTE_CANDIDATE, "promote_candidate")
    raise candidate_registry.PromotionRefused("unreachable by construction")


def describe() -> List[Dict[str, Any]]:
    """The toolbox, for a caller that needs to know what it may do."""
    return [{"name": spec.name, "permission": spec.permission,
             "description": spec.description, "writes": spec.writes,
             "grantable": spec.permission in ALL_PERMISSIONS}
            for spec in registered()]
