# Security audit — Phase 17

Spec §35, §64. Assessed against what the system is: a set of scheduled
scripts writing a SQLite file and a static HTML page. Nothing is
network-reachable, so most of the conventional web checklist does not
apply — recorded below rather than silently skipped.

---

## 1. Attack surface

| Surface | Present? | Notes |
|---|---|---|
| HTTP server / API | **No** | no Flask, FastAPI, Django, aiohttp, uvicorn anywhere |
| Authentication / sessions | **No** | nothing to authenticate to |
| CORS / CSRF | **N/A** | no browser-submitted requests |
| File uploads | **No** | |
| Admin functionality | **No** | |
| Outbound: news APIs | Yes | Finnhub, AlphaVantage, FRED, Polygon — read-only, keyed |
| Outbound: SMTP | Yes | alert email |
| Outbound: IBKR | Yes | localhost gateway only, TLS enforced for non-local hosts |
| Published artefact | Yes | `docs/index.html` on GitHub Pages — **public** |

The real surface is the published dashboard and the secrets used to
build it.

## 2. Secrets

**Nine environment variables read; zero credentials in source.**

```
ALPHA_VANTAGE_API_KEY  FINNHUB_API_KEY  FRED_API_KEY  POLYGON_API_KEY
SMTP_HOST  SMTP_PORT  SMTP_USERNAME  SMTP_PASSWORD  ALERT_EMAIL_TO
```

Verified mechanically:

- Regex sweep for `(password|secret|api_key|token|private_key) = "…"`
  across `src/` and `scripts/`: **no matches** outside `os.environ`.
- `IBKRConfig` has **no credential field at all** — not empty, absent.
  The Client Portal Gateway holds the session; this application never
  sees a username or password.
- `.env` and `.env.*` are gitignored; `!.env.example` is excepted and
  contains only placeholders.
- `git ls-files .env` → empty. No `.env` has ever been tracked.

Both facts are re-checked on every run of `scripts/audit_live_safety.py`
(Q12–Q15), so this is a standing check rather than a one-time finding.

## 3. Credential scrubbing

`src/execution/adapters/ibkr/errors.py` provides `scrub()`, applied to
IBKR error payloads before logging, removing cookies and session
tokens. IBKR error text can echo request headers.

## 4. Published-artefact exposure

`docs/index.html` is public. It is generated from the database by
`DashboardGenerator`. Reviewed for what it embeds:

- Account identifiers appear (`DU1234567` — an IBKR **paper** account).
- No API keys, no tokens, no SMTP settings.
- No position or P&L data, because none exists.

**Finding (LOW):** if this system ever holds a real account id, the
dashboard would publish it. Paper account ids are not sensitive; a live
one is mildly so. Worth a redaction rule before any live account is
configured — recorded, not urgent, since no live path exists.

## 5. Authorization

`ExecutionService` requires a `Caller` carrying explicit
`ExecutionPermission`s. `Caller` defaults to **read-only**, so a caller
constructed without thought cannot execute. This is the correct default
and is the only authorization model in the system — appropriate, given
there are no users.

## 6. Insecure defaults

Swept for the usual shapes. Findings:

- `IBKRConfig` **refuses** TLS-off for any non-local host. Local-only
  TLS-off is correct: the Client Portal Gateway is `localhost:5000` with
  a self-signed certificate.
- No capital limit ships with a default value — every real-money cap is
  `None` until a human sets one (spec §25).
- `Caller` defaults to read-only.
- `TradingState`, kill switch, and ordering gate all default to the
  restrictive setting.

**No insecure default found.**

## 7. Dependencies

Three: `feedparser`, `pandas`, `yfinance`. All **pinned** as of
commit `f1c0868` (TD-12, fixed) — `6.0.14`, `3.0.5`, `1.7.0`. No
known-vulnerable version is installed. Pinning closes both the
supply-chain surface and the reproducibility hazard: a research result
that cannot be recomputed on the same library versions is not a
result.

## 8. Verdict

**No critical or high security issue found.**

The system's security posture is largely a consequence of its shape:
there is no server, no user, no session and no inbound request. The
things that could go wrong — a leaked API key, a published account id —
are both checked mechanically and both currently clean.

The one standing recommendation is pinning dependencies. The one
forward-looking one is a redaction rule for account identifiers before
any live account exists.
