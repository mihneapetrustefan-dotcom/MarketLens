# Trading Memory — reference

Phase 21 · memory version `v1` · context schema `ctx-v1` · 2026-09-05

A structured record of what the system has been through. Not a vector
dump, not generated lessons, not chat history.

---

## 1. What an experience is

```
EXPERIENCE = CONTEXT + DECISION + EXPECTATION + OUTCOME
           + ERROR ATTRIBUTION + EVIDENCE
```

A join, not a paraphrase. Every field either references a canonical
record from Phase 19 or Phase 20, or snapshots a value that was true at
decision time. Nothing is invented and no sentence is stored that
cannot be traced back to a row.

---

## 2. `available_at` — the column the phase turns on

An experience becomes **knowable** when its outcome window closes, not
when we compute it.

```
available_at = outcome_measurements.window_end
```

Without that distinction, point-in-time memory is decorative. Dated by
`created_at`, every experience would appear at the same instant — today
— and `memory_as_of('2026-08-20')` would cheerfully return outcomes
that had not yet happened. The leakage would be total and invisible:
every historical study would consult its own future and report
excellent results.

With it, measured on production:

| `as_of` | experiences visible |
|---|---:|
| 2026-08-10 | **38** |
| 2026-08-20 | **1,114** |
| 2026-09-01 | 3,825 |
| now | 4,311 |

An experience with no `window_end` — pending, or never measurable —
gets no `available_at`, is marked `INCOMPLETE`, and **never enters a
point-in-time result**. It is kept, because knowing something could not
be measured is worth remembering.

---

## 3. Experience quality

| Quality | Meaning |
|---|---|
| `VALIDATED` | complete provenance, measured outcome, attributed cause, promoted model |
| `EXPERIMENTAL` | the same, but from a model no human promoted |
| `INCOMPLETE` | missing an outcome, an attribution, or a timestamp |
| `SUPERSEDED` | contradicted by a later correction |

Experimental experience is **kept and never pooled**. Today every
experience is experimental, because no model has been promoted — so
every pattern carries `experimental_count` saying how much of it is
research rather than production.

---

## 4. Classification — driven by attribution, not profit

| Class | When |
|---|---|
| `EXPECTED_WIN` | no error, a gain inside the usual range |
| `UNEXPECTED_WIN` | no error, a gain outside it |
| `EXPECTED_LOSS` | an ordinary adverse move, or no error and a loss |
| `UNEXPECTED_LOSS` | an error, and a loss outside the usual range |
| `MIXED` | **an error was attributed and it still made money** |
| `UNSUCCESSFUL` | an error, and a loss |
| `NO_CLEAR_RESULT` | unknown or missing attribution |

`MIXED` is the important one. §13 is explicit that a profitable result
can still be a weak or lucky decision, and calling it successful would
teach the system that being right by accident is being right. On
production it is the second-largest class at **1,540**.

`unexpected` comes from Phase 19's cohort percentile band, and is
`None` when the cohort was too small to say — in which case nothing is
called unexpected.

---

## 5. Patterns

A pattern says: *these conditions co-occurred with these outcomes, this
many times, over this window.* It does not say one caused the other and
it does not say the next occurrence will match.

Eleven stated families — not a cross product. §51 warns against
precomputing every combination, and at this history depth the cross
product would be tens of thousands of cohorts of three.

```
signal_direction_horizon   event_direction        event_horizon
model_horizon              instrument_direction   asset_class_direction
regime_direction           regime_horizon         strategy_horizon
error_horizon              sector_direction
```

A cohort keyed on a missing dimension is **skipped**, not bucketed as
"unknown" — an unknown cohort would be the largest pattern in the
database and would mean nothing.

### Quality states

| State | Meaning |
|---|---|
| `CONFIRMED` | ≥30 experiences, stable across sub-periods |
| `WEAK` | under 30 — quotes no rate at all |
| `CONFLICTING` | sub-populations disagree; no single rate describes it |
| `UNSTABLE` | hit rate varies >15% across sub-periods |
| `STALE` | newest evidence older than 90 days |
| `REQUIRES_REVIEW` | enough observations, too little history |

**Measured on production: 415 WEAK, 110 REQUIRES_REVIEW, 44 CONFIRMED,
41 UNSTABLE.** 68% weak is the correct result for 27 days of data, and
reporting it is the point.

---

## 6. Confidence — three different things

| Kind | What it means |
|---|---|
| Model confidence | how sure the model was about a prediction |
| Signal confidence | a heuristic trust score (Phase 18: near-constant at 0.30) |
| **Memory confidence** | whether a historical regularity is real |

§27 requires them kept apart, and they are. Memory confidence:

```
conflicting evidence      -> CONFLICTING_EVIDENCE   (always)
sample < 30               -> INSUFFICIENT_EVIDENCE  (always)
unstable across periods   -> at most LOW
enough n, too little time -> at most MEDIUM
high dispersion           -> at most MEDIUM
otherwise                 -> HIGH
```

Ordinal, never a probability. Nothing has checked how often a pattern
holds on data it was not built from.

---

## 7. Stability, regime dependence, contradiction

**Stability** (§29) compares weekly sub-periods. Two periods must each
clear 30 experiences before stability can be assessed at all —
otherwise the verdict is `insufficient_history`, which is what a
27-day record gets. A pattern whose sub-periods diverge by more than
15% is `UNSTABLE`, not "good with noise": an average over a good period
and a bad one describes neither.

**Regime dependence** (§30) is stored beside the all-regime numbers,
never collapsed into them.

**Contradiction** (§31, §32) has two forms:

- *within* a pattern — two regimes on opposite sides of a coin flip
  make it `CONFLICTING`, and `describe()` refuses to quote a rate;
- *between* patterns — `find_contradictions()` surfaces pairs that
  share conditions and disagree.

Both are **kept visible, not resolved.** Which pattern generalises is a
research question, not a tie-break. Production surfaces **262**
between-pattern contradictions.

---

## 8. Retrieval

`memory_as_of(T)` **rebuilds patterns** from the visible experience
rather than reading the stored table. A stored pattern was aggregated
over the whole record and carries the future inside its averages;
recomputing over the as-of subset is the only honest answer.

`similar_experiences(context)` is structured filtering, not vector
search. §40 asks to start there, and there is a stronger reason: an
embedding match cannot say *which* dimensions matched, so a response
could not honestly report what it relaxed.

Relaxation is progressive and recorded. The query starts fully specific
and drops dimensions from the least-specific end until it has enough
neighbours; the response names every dimension it dropped, because "20
similar experiences" means something very different when similarity was
reduced to "any short signal".

Every response carries a summary, the supporting experience ids, the
sample size, the evidence state, the time range, and **limitations** —
which is never empty in practice.

---

## 9. What memory refuses to say

- No causal claim. `describe()` never contains "causes", "because of"
  or "will be", and a test asserts it.
- No promise about the future. A large pattern's description ends
  *"this describes what happened, not what will happen"*.
- No rate below 30 experiences.
- No verdict on a model — `model_memory()` states it is a historical
  record and that promotion remains a human decision.
- No causal claim about events — `event_memory()` states co-occurrence
  only.
- No LLM anywhere in the package; a test asserts the absence.

---

## 10. Versioning

Four version stamps travel with every experience and every export:

```
memory_version              v1        the meaning of an experience or pattern
context_schema_version      ctx-v1    what decision-time context is captured
outcome_method_version      v1        Phase 19
attribution_method_version  v1        Phase 20
```

Memory and context version **separately**, because context definitions
and aggregation rules change for different reasons and at different
times. One number covering both would make "why did this memory change"
unanswerable.

A version bump writes **new rows beside the old ones**. Nothing
historical is rewritten.

---

## 11. Database

| Table | Holds |
|---|---|
| `trading_experiences` | one row per subject × horizon × memory version |
| `memory_patterns` | deterministic aggregates |
| `memory_pattern_evidence` | which experiences formed which pattern |
| `memory_snapshots` | what the system knew at a moment |

`memory_pattern_evidence` is what stops knowledge floating free of the
record. **36,311 links** on production, and `patterns_without_evidence`
must be zero.

Nothing Phase 19 or Phase 20 stores is stored again. Only the values a
memory query filters or aggregates on are mirrored — joining three
tables on every point-in-time retrieval would make retrieval unusable.

### One index worth naming

`experience_id` is the join key from pattern evidence and it is **not**
the primary key (that is the natural key). Without a unique index on
it, the integrity check joined 36,311 evidence rows against 6,510
experiences by full scan each time and took **over five minutes**; with
it, **0.52s**. Found by measuring a query that hung, not by adding
indexes speculatively (§50).

---

## 12. API

Following the convention Phases 19 and 20 established — typed functions
over a connection, no HTTP layer.

| Route | Function |
|---|---|
| `GET /memory/experiences` | `list_experiences()` |
| `GET /memory/patterns` | `list_patterns()` |
| `GET /memory/models/{id}` | `retrieval.model_memory()` |
| `GET /memory/signals/{id}` | `experience_detail()` |
| `GET /memory/regimes/{id}` | `retrieval.regime_memory()` |
| `GET /memory/events/{id}` | `retrieval.event_memory()` |
| `GET /memory/instruments/{id}` | `retrieval.instrument_memory()` |
| `GET /memory/search` | `retrieval.similar_experiences()` |

**Every listing accepts `as_of`.** Not decoration: an endpoint that
could only answer "now" would push callers to filter afterwards, which
is exactly where the leak would appear.

`pattern_detail()` always attaches evidence. `experience_detail()`
shows which patterns an experience supports — the reverse link is how a
reader spots a generalisation resting on one instrument.

Export: `experiences.csv`, `patterns.csv`, `pattern_evidence.csv`,
`memory.json`. CSV rather than Parquet — pyarrow is not a dependency.

---

## 13. What is defined and empty

| Memory | Status |
|---|---|
| Execution (§20) | **defined, empty** — no order has ever been placed |
| Portfolio (§22) | **defined, empty** — no portfolio or position exists |
| Regime (§17) | **defined, empty** — `market_regime` is NULL throughout |
| Risk (§21) | signal suppression only; no `risk_decisions` table |

Each says which tables are missing. The shape exists so the first real
execution, position or regime label has somewhere to go — and so the
absence is visible rather than looking like a clean record.

Risk memory reports withheld-but-right alongside wrong-calls-not-
withheld, and **labels neither a mistake**. A suppression that avoided
a loss is the rule working; one that withheld a correct call has a
cost. Both are recorded so the trade-off can be studied.

---

## 14. Running it

```bash
python scripts/build_memory.py --apply
python scripts/build_memory.py --apply --since 2026-09-01   # incremental
python scripts/build_memory.py --as-of 2026-08-20           # read-only
python scripts/build_memory.py --snapshot 2026-08-20 --apply
python scripts/build_memory.py --export data/exports/memory
```

Pipeline **stage 13 of 14**, after error attribution and before the
dashboard rebuild.

Measured on production: experience 1.2s, patterns 3.5s, integrity 0.5s.

---

## 15. What Phase 22 can ask

| Question | Answered by |
|---|---|
| What happened last time under similar conditions? | `similar_experiences()` |
| How often did this signal fail? | `signal_memory()` |
| What errors recur? | `error_counts` on every pattern |
| What regimes hurt this model? | `regime_breakdown` per pattern |
| What execution issues repeat? | `execution_memory()` — defined, empty |
| **What did the system know at time T?** | `memory_as_of(T)` |
| Which experiences support this claim? | `memory_pattern_evidence` |
| Is this regularity stable? | `stability`, `periods` |
| Does the evidence conflict? | `contradictions`, `CONFLICTING` |

The last four are what make an Experiment Engine possible rather than
merely plausible: a hypothesis needs evidence it can name, a stability
claim it can check, and a memory view that cannot see its own future.

Build memory from evidence. Build learning from memory.
