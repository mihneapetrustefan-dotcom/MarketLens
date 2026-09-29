# Provider evidence — IBKR market-data billing (ticket #561227)

**Status:** provider policy recorded. **Operational verification: PENDING.**

| | |
|---|---|
| Date received | 2026-09-29 |
| Ticket | #561227 |
| Provider | Interactive Brokers Client Services |
| Channel | Client Services reply to a support ticket |
| Evidence class | provider statement, not a statement of account |

Sanitized deliberately: no account identifier, username, email, credential
or token appears in this file, and none may be added to it.

---

## 1. What the provider stated

1. API calls to the snapshot endpoint are charged **USD 0.01 per request**.
2. Once snapshot requests for a given exchange reach **150 in a calendar
   month**, the snapshot fee for that exchange is **capped at USD 1.50**
   for that month.
3. IBKR states that **real-time streaming quotes for that exchange are then
   provided for the remainder of that month at no additional charge**.
4. The snapshot usage counter **includes API requests**.
5. The usage counter is **updated at the close of each business day**, not
   continuously during the trading session.
6. The US Equity and Options Add-On Streaming Bundle is described as
   **replacing** snapshot delivery under the Value Bundle for the covered
   exchanges — an alternative, not a prerequisite.
7. On a monthly statement the charges appear under **"GLOBAL SNAPSHOT"**.

## 2. What this SUPERSEDES

`src/adaptive/cost_governor.py` (Phase 25.9I-A) and
`docs/PHASE_25_9I_A_ARCHITECTURE.md` recorded an open question in which the
variable component might be:

> ~USD 2,500/month IF each API snapshot bills at USD 0.01, at ~250,000
> requests a month

**That assumption is superseded.** It modelled an *uncapped* per-request
charge. Item 2 above establishes a per-exchange monthly ceiling, so the
variable component cannot grow without bound while the cap holds.

The earlier text is **not deleted** — it records why the question was open,
and the 2026-09-24 episode in which IBKR's own assistant answered the same
question two contradictory ways within an hour is exactly why a provider
statement is not treated here as a verified account behaviour.

## 3. What this does NOT establish

This is the distinction the whole phase is built on:

| | |
|---|---|
| **POLICY_KNOWN** | YES — items 1–7, from the provider, on 2026-09-29 |
| **ACCOUNT_BEHAVIOR_VERIFIED** | **NO** — this account has never been observed reaching a cap, and no realtime marker has ever been observed on it |

Specifically **not** established by this ticket:

- that the cap applies to **this** account, in **this** (paper) context;
- that realtime actually becomes available after the cap, on this account;
- **what counts as one "request"** — see §4, this is material;
- the billing timezone in which "calendar month" is evaluated;
- whether the benchmark (SPY, ARCA) bills like any other exchange;
- the current price of either bundle (not quoted in the reply, so it stays
  UNKNOWN rather than being carried over from an unverified earlier note).

## 4. The open question the reply did not answer: the counting basis

The repository's transport batches the whole universe into **one** HTTP
call (`transport.market_snapshot(conids, fields)` —
`src/marketdata/quotes.py`, one request per cycle regardless of universe
size). The provider said "per request" without defining whether a request
is:

| Basis | One 60 s cycle of the 31-instrument universe costs | Full 390-minute session, **if the cap did not hold** |
|---|---|---|
| `PER_HTTP_CALL` | 1 request, attributed across 3 exchanges | ~USD 3.90 |
| `PER_INSTRUMENT` | 31 requests (NASDAQ 13, NYSE 17, ARCA 1) | ~USD 120.90 |

A 31× difference in worst-case exposure. The counting basis is therefore
modelled as **UNKNOWN** and the conservative basis (`PER_INSTRUMENT`) is
used for any exposure ceiling, per the rule that an unknown cost is never
the cheap answer.

## 5. How it is encoded

- `src/adaptive/billing.py` — `IBKR_SNAPSHOT_POLICY_561227`, carrying the
  three numbers above plus this file as its evidence source and
  2026-09-29 as its evidence date.
- Confidence is recorded as `PROVIDER_STATED`, never `ACCOUNT_VERIFIED`.
  Only an observation may raise it, and only `scripts/validate_market_data_billing.py`
  produces such an observation.

## 6. Verification still outstanding

Run the controlled bounded test described in
`docs/PHASE_25_9I_C_ARCHITECTURE.md` §"Controlled October validation", then
reconcile the local request ledger against the portal counter on the
following business day. Until that reconciliation exists, every consumer
must read this policy as expected, not confirmed.
