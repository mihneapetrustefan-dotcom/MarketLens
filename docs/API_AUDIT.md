# API audit — Phase 17

Spec §32, §34, §62.

---

## 1. There is no HTTP API

Verified: no Flask, FastAPI, Django, aiohttp, tornado, uvicorn, and no
`@app.route` anywhere in `src/` or `scripts/`.

This is a deliberate architectural choice, not an omission. The system
is a set of scheduled scripts writing SQLite and publishing a static
page. Spec §32's checklist — pagination, rate limits, versioning, CORS
— is assessed against the interfaces that actually exist.

## 2. The interfaces that do exist

| Interface | Shape | Consumers |
|---|---|---|
| `ExecutionService` | typed Python facade with `Caller` permissions | 3 CLIs |
| `PortfolioService` | typed facade | paper session, risk CLI |
| Repository classes (14) | SQL behind typed methods | engines, scripts |
| CLI scripts (22) | argparse | operators, GitHub Actions |
| `DashboardGenerator` | DB → static HTML | GitHub Pages |

## 3. ExecutionService — the closest thing to an API

The only interface with an authorization model:

```python
service.submit(caller, request)   # requires ExecutionPermission.SUBMIT
service.dry_run(caller, request)  # read-only
```

`Caller` defaults to read-only. Permission is checked before the
orchestrator is reached. This is the correct shape for a facade that
can reach a broker.

**Assessment: KEEP.** Putting HTTP in front of this would introduce
authentication, session handling and a network surface the system
currently has none of, in exchange for no capability it lacks.

## 4. CLI surface

| CLI | Purpose | Safety gates |
|---|---|---|
| `run_execution.py` | Phase 14 broker-neutral | `--allow-paper-orders`, `--assume-risk-approved` |
| `run_ibkr.py` | Phase 15 IBKR operator | same, plus the ordering gate |
| `run_operations.py` | Phase 16 governance | `--actor` required on every mutation |
| `audit_live_safety.py` | 16-question safety audit | exits non-zero on failure |
| 18 pipeline scripts | one stage each | `--dry-run` on the destructive ones |

**Consistency finding (LOW):** `--db`, `--actor`, `--dry-run` and
`--mock` are named consistently across the three execution CLIs. The 18
pipeline scripts vary — some have `--dry-run`, some do not. Not worth
normalising for its own sake; worth doing when a script is next
touched.

## 5. Business logic in the wrong layer (spec §41)

**Finding (MEDIUM, = TD-06):** two scripts contain business logic that
also exists as a library module:

- `scripts/build_research_observations.py` (355 lines) reimplements
  `src/research/builder.py` (398 lines)
- `scripts/backfill_article_entities.py` writes its own SQL instead of
  using `src/data_access/entity_repository.py`

Both library modules are reached only by their tests. A script should
be a thin CLI over a service; here the script *is* the service, and the
service is dead weight beside it.

Recommended: make the scripts delegate. Do not delete either side until
the script is proven to produce identical output.

## 6. No duplicated endpoints

There are no endpoints to duplicate. Repository methods were checked
for overlapping responsibility: 14 repositories, one per domain, no two
writing the same table.

## 7. Compatibility (spec §62)

No external clients exist. The only consumers of any interface live
inside this repository, so nothing was at risk of breaking and no
deprecation cycle was needed for the Phase 17 change.

`src/execution/intake.py` is purely additive — it adds a producer for
`IntentRequest`, changes no existing signature, and leaves both CLI
paths working exactly as before.
