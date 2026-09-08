"""
src/challengers/registry.py
-------------------------------------
Phase 24 §3-§10 — creating a challenger from a candidate, versioning it,
and refusing to let it move once it is being evaluated.

THE THREE RULES THIS MODULE ENFORCES
----------------------------------------
1. **Not every candidate becomes a challenger** (§3). `from_candidate`
   runs `validate_candidate` first and refuses with the reasons. A
   challenger costs walk-forward folds, robustness slices and a
   parameter sweep; spending that on evidence that already cannot
   support it fills the record with work nobody can act on.

2. **The baseline is versioned and pinned** (§6). Resolved once, at
   creation, and written into the fingerprint. A baseline that moves
   during evaluation turns a comparison into an anecdote.

3. **A started challenger is frozen** (§9, §10). Changing the
   baseline, the change definition, the plan or the dataset cutoff
   produces a NEW VERSION — never a silent mutation of the running
   one. `save` refuses the mutation; `new_version` is the supported
   path.

WHY THE DATASET CUTOFF IS IN THE FINGERPRINT
------------------------------------------------
Phase 23.5 found a stale cached result being returned as current
research: an experiment's dataset identity was purely definitional, so
a record that GREW hashed identically. The same trap is available here
and is closed the same way — `current_data_cutoff` is stamped at
creation, so a challenger evaluated on a longer record is a different
challenger rather than the same one with different numbers.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional, Sequence

from src.data_access.challenger_schema import initialize_challenger_schema
from src.domain.challenger_models import (
    CHALLENGER_METHOD_VERSION, Actor, BaselineKind, BaselineSpec,
    ChallengerChanged, ChallengerLimits, ChallengerStatus, ChangeDefinition,
    Challenger, EvaluationPlan, LimitExceeded, VariantType, _digest, utcnow,
    validate_candidate,
)


class ChallengerRefused(Exception):
    """The challenger cannot be created, and the reason is always given."""


def _code_version() -> str:
    import subprocess
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


# ======================================================================
# Baselines
# ======================================================================

def resolve_baseline(conn: sqlite3.Connection, candidate: Dict[str, Any]
                     ) -> BaselineSpec:
    """
    The versioned thing this challenger must beat (§6).

    The candidate carries `base_version` as `baseline:<name>` — a
    registered Phase 22 baseline. It is resolved through Phase 22's own
    registry rather than reconstructed here, because a baseline
    invented for one comparison is a baseline chosen to flatter it.

    THE VERSION IS THE HONEST PART. A registered baseline is code, so
    its version is the commit that defines it. That is not decorative:
    if the baseline's behaviour changes in a later commit, a comparison
    made against the old one is no longer comparable, and the recorded
    version is what makes that visible.
    """
    from src.experiments import evaluators

    base = str(candidate.get("base_version") or "")
    name = base.split(":", 1)[1] if ":" in base else base
    if not name:
        raise ChallengerRefused(
            "the candidate names no baseline, so there is nothing to compare "
            "a challenger against (§6)")
    try:
        arm = evaluators.baseline(name)
    except evaluators.EvaluatorError as exc:
        raise ChallengerRefused(
            "the candidate's baseline %r is not registered: %s" % (name, exc))

    return BaselineSpec(
        kind=BaselineKind.CURRENT_SIGNAL_RULE,
        name=arm.name,
        version="signal-rule:%s@%s" % (name, _code_version() or "unknown"),
        evaluator=arm.evaluator,
        parameters=dict(arm.parameters),
        complexity=max(arm.complexity, 1))


def active_model_baseline(conn: sqlite3.Connection) -> Optional[BaselineSpec]:
    """
    The promoted model, if one exists (§6, §44).

    Uses Phase 18's own selection rather than re-deriving deployability.
    Returns None when no model has been promoted — which is the case
    today, and is why `MODEL` challengers are declared rather than
    runnable. Returning None instead of substituting an experimental
    model is deliberate: comparing against an unvalidated model and
    calling it "the baseline" would make every result meaningless in a
    way nobody could see from the number.
    """
    from src.modeling.selection import (
        NoUsableModel, SelectionPolicy, select,
    )
    try:
        # ACTIVE_ONLY is the point. Phase 18 raises rather than falling
        # back to an unvalidated model, and that refusal is exactly the
        # answer this function needs -- "no promoted model exists" is a
        # fact about governance, not an error to be swallowed.
        chosen = select(conn, policy=SelectionPolicy.ACTIVE_ONLY)
    except NoUsableModel:
        return None
    except sqlite3.OperationalError as exc:
        # A database with no `trained_models` table has no promoted
        # model, which is the same answer. Narrowed to the missing
        # table on purpose: absorbing every OperationalError would
        # hide a genuine query bug behind "experimental basis", which
        # is exactly the silent-degradation trap Phase 23.5 closed in
        # the dashboard helpers.
        if "no such table" not in str(exc).lower():
            raise
        return None
    identifier = getattr(chosen, "model_qualified_id", None)         or getattr(chosen, "trained_model_id", None)
    if not identifier:
        return None
    return BaselineSpec(kind=BaselineKind.ACTIVE_MODEL,
                        name=str(identifier), version=str(identifier))


# ======================================================================
# Creation
# ======================================================================

def _challenger_id(candidate_id: str, change: ChangeDefinition,
                   baseline: BaselineSpec) -> str:
    return "chl-" + _digest({"candidate": candidate_id,
                             "change": change.identity(),
                             "baseline": baseline.identity()})[:20]


def from_candidate(conn: sqlite3.Connection, candidate_id: str, *,
                   plan: Optional[EvaluationPlan] = None,
                   created_by: str = "", force: bool = False
                   ) -> Challenger:
    """
    Turn a validated candidate into a challenger definition (§3, §7).

    Refuses candidates whose evidence cannot support the work, unless
    `force` is passed — which exists so a human can override with their
    eyes open, and is recorded in the notes rather than hidden.
    """
    from src.autoresearch import api as research_api
    from src.experiments import engine as experiment_engine

    initialize_challenger_schema(conn)
    candidates = {row["candidate_id"]: row
                  for row in research_api.candidates(conn, limit=500)}
    candidate = candidates.get(candidate_id)
    if candidate is None:
        raise ChallengerRefused("no candidate %r exists" % candidate_id)

    problems = validate_candidate(candidate)
    if problems and not force:
        raise ChallengerRefused(
            "this candidate does not qualify for a challenger: %s. "
            "Building one costs walk-forward folds, robustness slices and a "
            "sweep; spending that on evidence that already cannot support it "
            "is how a research record fills with work nobody can act on."
            % "; ".join(problems))

    changes = candidate.get("changes") or {}
    evaluator = changes.get("evaluator") or ""
    parameters = dict(changes.get("parameters") or {})
    condition = dict(changes.get("condition") or {})

    change = ChangeDefinition(
        kind="signal_filter_added",
        summary=("restrict to the cohort %s"
                 % ", ".join("%s=%s" % (k, condition[k])
                             for k in sorted(condition))
                 or "apply %s" % evaluator),
        evaluator=evaluator,
        parameters=parameters,
        added=sorted(condition),
        complexity=1 + len(parameters))

    baseline = resolve_baseline(conn, candidate)
    cutoff = experiment_engine.current_data_cutoff(conn)

    # A challenger built on a model that has never been promoted is
    # research on research. It is allowed and it must say so (§75).
    experimental = active_model_baseline(conn) is None

    hypothesis = _hypothesis_of(conn, candidate.get("hypothesis_id") or "")

    challenger = Challenger(
        challenger_id=_challenger_id(candidate_id, change, baseline),
        version=1,
        variant_type=VariantType.SIGNAL,
        name=candidate.get("name") or "challenger",
        baseline=baseline,
        change=change,
        plan=plan or EvaluationPlan(
            sensitivity_values=_sensitivity_values(parameters)),
        candidate_id=candidate_id,
        hypothesis_id=candidate.get("hypothesis_id") or "",
        experiment_id=candidate.get("experiment_id") or "",
        conclusion_id=candidate.get("conclusion_id") or "",
        family_id=(hypothesis or {}).get("family_id") or "",
        dataset_cutoff=cutoff,
        code_version=_code_version(),
        experimental_basis=experimental,
        created_by=created_by or "system")

    if problems and force:
        challenger.notes.append(
            "created over the following objections, deliberately: "
            + "; ".join(problems))
    if experimental:
        challenger.notes.append(
            "EXPERIMENTAL BASIS: no model has been promoted, so this "
            "challenger competes against a signal rule rather than a "
            "validated production model (§75).")

    challenger.validate()
    return challenger


def _hypothesis_of(conn: sqlite3.Connection, hypothesis_id: str
                   ) -> Optional[Dict[str, Any]]:
    if not hypothesis_id:
        return None
    try:
        from src.autoresearch import hypotheses as hypothesis_layer
        return hypothesis_layer.load(conn, hypothesis_id)
    except Exception:
        return None


def _sensitivity_values(parameters: Dict[str, Any]) -> List[Any]:
    """
    Neighbouring values for the sweep (§18).

    Only numeric parameters have neighbours. A categorical cohort key
    like `event_type=acquisition` has none, so no sweep is invented for
    it — a fabricated neighbour would produce a plateau that means
    nothing.
    """
    for key, value in sorted(parameters.items()):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            step = max(abs(float(value)) * 0.2, 0.05)
            return [round(float(value) + step * offset, 4)
                    for offset in (-2, -1, 0, 1, 2)]
    return []


# ======================================================================
# Persistence, versioning and immutability
# ======================================================================

def save(conn: sqlite3.Connection, challenger: Challenger) -> None:
    """
    Persist a definition.

    Refuses to overwrite a STARTED challenger whose fingerprint has
    changed (§9, §10). The supported path for a change is
    `new_version`, which keeps the old definition and its results
    intact — a challenger whose baseline can move after the numbers
    exist is not a comparison.
    """
    initialize_challenger_schema(conn)
    challenger.validate()

    stored = conn.execute("""
        SELECT fingerprint, status FROM challengers
        WHERE challenger_id = ? AND version = ?
    """, (challenger.challenger_id, challenger.version)).fetchone()
    if stored and stored[0] != challenger.fingerprint:
        if ChallengerStatus(stored[1]).is_started:
            raise ChallengerChanged(
                "challenger %s v%d is %s and its definition has changed "
                "(%s -> %s). Create a new version instead of editing one "
                "whose evaluation has begun; the old definition and its "
                "results stay."
                % (challenger.challenger_id, challenger.version, stored[1],
                   stored[0][:12], challenger.fingerprint[:12]))

    baseline = challenger.baseline
    change = challenger.change
    conn.execute("""
        INSERT OR REPLACE INTO challengers (
            challenger_id, version, method_version, variant_type, name,
            status, candidate_id, hypothesis_id, experiment_id,
            conclusion_id, family_id, baseline_kind, baseline_name,
            baseline_version, baseline_json, change_kind, change_summary,
            change_json, plan_json, limits_json, dataset_cutoff,
            dataset_version, feature_version, label_version, model_version,
            strategy_version, code_version, experimental_basis, fingerprint,
            notes_json, created_by, created_at, started_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                  ?,?,(SELECT started_at FROM challengers
                       WHERE challenger_id=? AND version=?))
    """, (challenger.challenger_id, challenger.version,
          challenger.method_version, challenger.variant_type.value,
          challenger.name, challenger.status.value, challenger.candidate_id,
          challenger.hypothesis_id, challenger.experiment_id,
          challenger.conclusion_id, challenger.family_id,
          baseline.kind.value, baseline.name, baseline.version,
          json.dumps(baseline.as_dict(), sort_keys=True, default=str),
          change.kind, change.summary,
          json.dumps(change.as_dict(), sort_keys=True, default=str),
          json.dumps(challenger.plan.as_dict(), sort_keys=True, default=str),
          json.dumps(ChallengerLimits().as_dict()),
          challenger.dataset_cutoff, challenger.dataset_version,
          challenger.feature_version, challenger.label_version,
          challenger.model_version, challenger.strategy_version,
          challenger.code_version, 1 if challenger.experimental_basis else 0,
          challenger.fingerprint, json.dumps(challenger.notes),
          challenger.created_by, challenger.created_at,
          challenger.challenger_id, challenger.version))
    conn.commit()


def new_version(conn: sqlite3.Connection, challenger: Challenger, *,
                reason: str, **changes: Any) -> Challenger:
    """
    A changed definition, as a NEW version (§9).

    The previous version keeps its results and its history. This is the
    only supported way to alter a challenger that has begun evaluation,
    and the reason is stored on the new version rather than lost.
    """
    if not reason.strip():
        raise ValueError("a new version must say why it exists")
    initialize_challenger_schema(conn)
    highest = conn.execute("""
        SELECT COALESCE(MAX(version), 0) FROM challengers
        WHERE challenger_id = ?
    """, (challenger.challenger_id,)).fetchone()[0]

    import copy
    successor = copy.deepcopy(challenger)
    successor.version = int(highest) + 1
    successor.status = ChallengerStatus.PROPOSED
    for key, value in changes.items():
        if not hasattr(successor, key):
            raise ValueError("challengers have no field %r" % key)
        setattr(successor, key, value)
    successor.notes = list(challenger.notes) + [
        "v%d supersedes v%d: %s" % (successor.version, challenger.version,
                                    reason)]
    successor.created_at = utcnow()
    successor.validate()
    save(conn, successor)
    return successor


def set_status(conn: sqlite3.Connection, challenger_id: str, version: int,
               status: ChallengerStatus, *, reason: str = "") -> None:
    initialize_challenger_schema(conn)
    if status is ChallengerStatus.VALIDATING:
        conn.execute("""
            UPDATE challengers SET status = ?, started_at = COALESCE(started_at, ?)
            WHERE challenger_id = ? AND version = ?
        """, (status.value, utcnow(), challenger_id, version))
    else:
        conn.execute("""
            UPDATE challengers SET status = ?
            WHERE challenger_id = ? AND version = ?
        """, (status.value, challenger_id, version))
    conn.commit()


_COLUMNS = (
    "challenger_id", "version", "method_version", "variant_type", "name",
    "status", "candidate_id", "hypothesis_id", "experiment_id",
    "conclusion_id", "family_id", "baseline_kind", "baseline_name",
    "baseline_version", "baseline_json", "change_kind", "change_summary",
    "change_json", "plan_json", "limits_json", "dataset_cutoff",
    "dataset_version", "feature_version", "label_version", "model_version",
    "strategy_version", "code_version", "experimental_basis", "fingerprint",
    "notes_json", "created_by", "created_at", "started_at",
)


def load(conn: sqlite3.Connection, challenger_id: str,
         version: Optional[int] = None) -> Optional[Challenger]:
    """Rebuild a stored definition. Latest version unless one is named."""
    initialize_challenger_schema(conn)
    if version is None:
        row = conn.execute("""
            SELECT %s FROM challengers WHERE challenger_id = ?
            ORDER BY version DESC LIMIT 1
        """ % ", ".join(_COLUMNS), (challenger_id,)).fetchone()
    else:
        row = conn.execute("""
            SELECT %s FROM challengers WHERE challenger_id = ? AND version = ?
        """ % ", ".join(_COLUMNS), (challenger_id, version)).fetchone()
    if row is None:
        return None
    record = dict(zip(_COLUMNS, row))

    def load_json(key, default):
        try:
            return json.loads(record.get(key) or "null") or default
        except (TypeError, ValueError):
            return default

    baseline_data = load_json("baseline_json", {})
    change_data = load_json("change_json", {})
    plan_data = load_json("plan_json", {})

    baseline = BaselineSpec(
        kind=BaselineKind(baseline_data.get("kind")
                          or record["baseline_kind"]
                          or "current_signal_rule"),
        name=baseline_data.get("name") or record["baseline_name"],
        version=baseline_data.get("version") or record["baseline_version"],
        evaluator=baseline_data.get("evaluator", ""),
        parameters=baseline_data.get("parameters", {}),
        complexity=baseline_data.get("complexity", 1))

    change = ChangeDefinition(
        kind=change_data.get("kind") or record["change_kind"],
        summary=change_data.get("summary") or record["change_summary"],
        evaluator=change_data.get("evaluator", ""),
        parameters=change_data.get("parameters", {}),
        added=change_data.get("added", []),
        removed=change_data.get("removed", []),
        complexity=change_data.get("complexity", 1))

    plan = EvaluationPlan(**{k: v for k, v in plan_data.items()
                            if k in EvaluationPlan.__dataclass_fields__})

    return Challenger(
        challenger_id=record["challenger_id"], version=record["version"],
        variant_type=VariantType(record["variant_type"]),
        name=record["name"], baseline=baseline, change=change, plan=plan,
        candidate_id=record["candidate_id"],
        hypothesis_id=record["hypothesis_id"],
        experiment_id=record["experiment_id"],
        conclusion_id=record["conclusion_id"],
        family_id=record["family_id"],
        dataset_cutoff=record["dataset_cutoff"],
        dataset_version=record["dataset_version"],
        feature_version=record["feature_version"],
        label_version=record["label_version"],
        model_version=record["model_version"],
        strategy_version=record["strategy_version"],
        code_version=record["code_version"],
        method_version=record["method_version"],
        status=ChallengerStatus(record["status"]),
        experimental_basis=bool(record["experimental_basis"]),
        notes=load_json("notes_json", []),
        created_by=record["created_by"], created_at=record["created_at"])


def listing(conn: sqlite3.Connection, *, status: Optional[str] = None,
            limit: int = 100) -> List[Dict[str, Any]]:
    """Every challenger's latest version, rejected ones included (§42)."""
    initialize_challenger_schema(conn)
    sql = """
        SELECT c.challenger_id, c.version, c.name, c.variant_type, c.status,
               c.baseline_name, c.baseline_version, c.change_summary,
               c.candidate_id, c.family_id, c.experimental_basis,
               c.dataset_cutoff, c.created_at,
               r.decision, r.effect, r.effect_low, r.effect_high
        FROM challengers c
        LEFT JOIN challenger_results r
               ON r.challenger_id = c.challenger_id
              AND r.challenger_version = c.version
        WHERE c.version = (SELECT MAX(v.version) FROM challengers v
                           WHERE v.challenger_id = c.challenger_id)
    """
    params: tuple = ()
    if status:
        sql += " AND c.status = ?"
        params = (status,)
    sql += " ORDER BY c.created_at DESC LIMIT ?"
    keys = ("challenger_id", "version", "name", "variant_type", "status",
            "baseline_name", "baseline_version", "change_summary",
            "candidate_id", "family_id", "experimental_basis",
            "dataset_cutoff", "created_at", "decision", "effect",
            "effect_low", "effect_high")
    return [dict(zip(keys, row))
            for row in conn.execute(sql, params + (limit,))]


def family_challenger_count(conn: sqlite3.Connection, family_id: str) -> int:
    """
    How many challengers this idea has already produced (§15, §41).

    Travels with every verdict, because the twentieth variant of one
    idea is not the first, and a reader who cannot see the count cannot
    discount the result.
    """
    if not family_id:
        return 1
    initialize_challenger_schema(conn)
    return max(1, conn.execute("""
        SELECT COUNT(DISTINCT challenger_id) FROM challengers
        WHERE family_id = ?
    """, (family_id,)).fetchone()[0])


def assert_family_budget(conn: sqlite3.Connection, family_id: str, *,
                         limits: Optional[ChallengerLimits] = None) -> None:
    """§50: prevent challenger explosion within one idea."""
    limits = limits or ChallengerLimits()
    count = family_challenger_count(conn, family_id)
    if family_id and count >= limits.max_challengers_per_family:
        raise LimitExceeded(
            "%d challengers already exist in this family, at the limit of "
            "%d. Testing the same idea repeatedly is how a comparison record "
            "manufactures a winner."
            % (count, limits.max_challengers_per_family))
