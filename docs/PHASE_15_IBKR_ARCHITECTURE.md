# Phase 15 — Interactive Brokers integration

IBKR as an **adapter behind the Phase 14 boundary**. Nothing above that
boundary knows IBKR exists.

**No real-money execution.** `IBKR_ENVIRONMENT` accepts only `paper`,
the configuration refuses anything else at construction, the gateway
refuses to be built for a real-money environment, and Phase 14's safety
layer refuses `LIVE` before any of it runs. §11 below is the audit.

---

## 1. Where IBKR sits

```
        SIGNALS → PORTFOLIO → RISK → ORDER INTENT      (phases 10-11)
                                          |
============ the Phase 14 broker-neutral boundary ============
                                          |
                          EXECUTION ORCHESTRATOR       (phase 14)
                                          |
                          BROKER GATEWAY (abstract)    (phase 14)
                                          |
              +---------------------------+-------------------+
              |                           |                   |
         PAPER ADAPTER              IBKR ADAPTER
          (phase 13/14)               (phase 15)         (phase 16)
                                          |
                                   IBKR TRANSPORT       ← second seam
                                          |
                        +-----------------+----------------+
                        |                                  |
              Client Portal Web API              TWS API (not built)
                        |
              Client Portal Gateway (java, localhost)
                        |
                  IBKR PAPER ACCOUNT
```

Two seams, not one. The first is Phase 14's: the core does not know
which broker. The second is this phase's: the **adapter does not know
which IBKR interface**. That is what lets a TWS transport be added
later without touching the mapper, the resolver or the gateway.

## 2. Transport choice, and why

IBKR offers two interfaces. Both require a locally running Java program
that a human authenticates — that is not a property of either choice,
it is a property of IBKR for retail accounts.

| | TWS API | **Client Portal Web API** (chosen) |
|---|---|---|
| Shape | async socket, callback message bus | REST over localhost |
| Needs running | TWS or IB Gateway | Client Portal Gateway |
| Auth | gateway holds the session | gateway holds the session |
| Python dependency | `ibapi`, shipped in IBKR's download | none — `requests` already present |
| Runs headless on Linux | poorly | yes |
| Contract identity | `reqContractDetails` callback | `conid`, a first-class REST concept |

**Chosen: Client Portal Web API.** Four reasons specific to this
repository:

1. **It matches the shape Phase 14 already has.** `BrokerGateway` is
   request/response with a `poll_events` drain. The TWS API is an
   asynchronous callback bus that needs a persistent event loop and a
   reader thread — and this project has no persistent runtime at all.
   Wrapping it would mean building that runtime first.
2. **Zero new dependencies.** `requests` arrives already through an
   existing dependency. `ibapi` is not on the standard install path and
   would have to be vendored from IBKR's download — a step no other
   part of this project requires.
3. **It runs where this project runs.** Linux and cloud, which is where
   every other phase executes.
4. **Contract resolution is first-class.** `/iserver/secdef/search` and
   the `conid` map directly onto Phase 14's `BrokerInstrumentMapping`
   and its `broker_payload` extension point.

And the decisive one for security: **the gateway holds the credential,
so this application never sees one.** There is no username or password
field anywhere in Phase 15 — see §10.

**The honest limitation.** Retail Client Portal access still requires a
human to log into the gateway in a browser. Session automation via
OAuth is available to institutional clients, not to this account type.
So the integration runs on a developer machine with the gateway up; it
**cannot run unattended under GitHub Actions cron** without a hosted,
authenticated gateway. That is stated rather than worked around.

## 3. Modules

| File | Responsibility |
|---|---|
| `config.py` | Environment configuration; paper-only; two safety gates |
| `errors.py` | IBKR failure → canonical category + reject code; scrubbing |
| `transport.py` | `IBKRTransport` seam + `ClientPortalTransport` |
| `mock_transport.py` | Deterministic double that fails on command |
| `mapper.py` | IBKR ↔ canonical: statuses, orders, executions, accounts |
| `contracts.py` | Symbol → `conid`, with ambiguity refused not resolved |
| `gateway.py` | `BrokerGateway` implementation |

Nothing outside `src/execution/adapters/ibkr/` imports any of them, and
a test asserts that IBKR vocabulary appears in no core module.

## 4. Contract resolution

The assumption this refuses: **`ticker == instrument`**. It is false at
IBKR more than anywhere else — the same symbol lists on several venues,
in several currencies, as several security types.

```
symbol + sec_type + currency [+ exchange]
        ↓ /iserver/secdef/search
   candidates
        ↓ filter by every discriminator supplied
   exactly one?  ──no──▶  ContractResolution(ambiguous=True, candidates=[...])
        │                 nothing is registered, nothing will trade
       yes
        ↓ /iserver/contract/{conid}/info   (tick size, lot size, minimum)
   IBKRContract → BrokerInstrumentMapping   (conid in broker_payload)
```

Choosing the first candidate would work almost always. The times it did
not would be a trade in the wrong security, on the wrong exchange, in
the wrong currency — silently. An unresolved instrument that refuses to
trade is a phone call; a wrongly-resolved one is a position nobody
meant to hold.

Resolved mappings persist in Phase 14's `broker_instrument_mapping`
table. **No new table was added** — the `broker_payload_json` column
already existed for exactly this, and it now round-trips on load.

## 5. Order status mapping

Written out explicitly, never by lowercasing. The entry that matters:

| IBKR | Canonical | Why |
|---|---|---|
| `PreSubmitted`, `Submitted` | `ACKNOWLEDGED` | IBKR's "Submitted" means the venue is **working it**. Canonical `SUBMITTED` means we sent it and heard nothing. Mapping by spelling would be wrong on every successful order. |
| `Filled` | `FILLED` | |
| `PendingCancel` | `CANCEL_REQUESTED` | |
| `Cancelled`, `ApiCancelled` | `CANCELLED` | |
| `Rejected` | `REJECTED` | |
| `Inactive`, `WarnState` | `RECONCILIATION_REQUIRED` | IBKR's catch-all for an order it holds but is not working. **Not terminal.** Reading it as cancelled would leave a live order the system believes is closed. |
| anything unrecognised | `RECONCILIATION_REQUIRED` | IBKR adding a status is a real possibility; assuming it resembles something familiar is how a live order gets treated as closed. |

## 6. The timeout

```
place_order → network timeout → IBKR may or may not hold the order
                    ↓
        SubmissionAck(timed_out=True)
                    ↓
        Phase 14 → UNKNOWN  (never FAILED, never retried)
                    ↓
        resolve_unknown_orders → GET order status
                    ↓
   knows it → adopt   |   never saw it → FAILED   |   unreachable → still UNKNOWN
```

`TIMEOUT` is deliberately excluded from `is_retryable`. Retrying a
timed-out submission is precisely the action that turns one intended
order into two.

When no IBKR order id came back, the **client order id** is the handle
— derived from Phase 14's idempotency key, so a retry computes the same
one, and IBKR's own duplicate detection sees it as the same order.

## 7. Executions and fills

Deduplicated on **IBKR's execution id**, never on the visible fields. A
venue filling 100 as two 50s at one price produces two executions
identical in instrument, side, size, price and second — collapsing them
would discard a real fill.

Commission frequently arrives **later**, in a separate report, so a
zero means "not yet reported" rather than "free". The raw IBKR payload
is kept beside every normalized fill.

Fills flow into the **existing** Phase 11/12 accounting. No second
portfolio system was created (§64).

## 8. Events

Polled, not pushed — this repository has no persistent runtime to hold
a websocket. `poll_events` diffs the venue against what we last saw and
emits only **changes**; re-emitting the same state every tick would
look like a duplicate downstream and be discarded, making real changes
invisible too.

A websocket transport would fill the same buffer, and nothing above
would change.

## 9. Recovery

After a restart the adapter restores two things Phase 14 cannot:

- **the broker-id map**, or an IBKR execution could not be attributed
  to our order, and every fill would look like it belonged to an order
  we never placed;
- **the seen-execution set**, because reconnecting is exactly what
  makes IBKR replay its recent executions.

Phase 14 restores the rest: the order book, the state history, the
fills, and the idempotency index rebuilt from the orders themselves.
In-flight orders become `UNKNOWN`.

## 10. Security

**No credential exists anywhere in this phase.**

- `IBKRConfig` has no username, password, token or key field. Not
  "should not" — there is none, and `describe()` reports
  `holds_credentials: False`.
- `BrokerGateway.connect()` takes no arguments; the transport
  inherits the gateway's session.
- No database column can hold a secret (a Phase 14 test scans every
  column name).
- `errors.scrub()` removes cookie/authorization/session/token-shaped
  keys **and** long opaque values before any payload is stored or
  logged. The gateway holds the password, but a session cookie in a log
  is still a credential in a log.
- `.env` is git-ignored; `.env.example` carries placeholders only and
  deliberately contains **no** `IBKR_USERNAME` or `IBKR_PASSWORD`,
  with a note explaining why adding them would be a mistake.

## 11. Live-execution audit

| Layer | Refusal |
|---|---|
| `IBKRConfig.__post_init__` | `IBKR_ENVIRONMENT=live` raises |
| `IBKRGateway.__init__` | a real-money environment raises |
| `IBKRConfig.can_submit_orders` | requires enabled **and** ordering **and** paper |
| `IBKRGateway.submit_order` | refuses unless all three hold |
| Phase 14 `ExecutionSafety.check` | refuses `LIVE` before anything else |
| Phase 14 `allow_real_orders` | property with no setter, permanently False |
| Phase 14 domain types | `Broker`/`BrokerAccount` refuse `LIVE` |

Seven layers. `IBKR_LIVE_TRADING_ENABLED` is **not** a variable this
project reads, because a flag implies a thing it could turn on.

## 12. Configuration

See `.env.example`. The two that matter:

```
IBKR_ENVIRONMENT=paper              # only value accepted
IBKR_PAPER_ORDERING_ENABLED=false   # the second gate
```

Connecting is **not** permission to trade. An IBKR session existing is
never a reason for an order to exist, so ordering is a separate flag,
off by default, and the CLI reports which one is missing.

## 13. One broker

**Interactive Brokers is the only broker of this project.** Phase 16
settled that: there is no MetaTrader 5 adapter, no MT5 compatibility
layer, no multi-broker routing, and no placeholder for a second venue.
`planned_gateways()` returns nothing, and no module in `src`, `tests`
or `scripts` mentions MT5.

The abstraction that remains is not there to support a second broker.
It is there because a boundary is what keeps IBKR-shaped detail —
conids, its own order ids, its status vocabulary — out of strategy,
signal, portfolio and risk code. That is worth having with exactly one
broker, and it is why IBKR logic still lives entirely in
`src/execution/adapters/ibkr/` and the core contains no
`if broker_id == "ibkr"` anywhere.

The conformance suite (§59) runs the same assertions against the paper
adapter, the IBKR adapter and the disabled adapter — three
implementations of one interface, all of them ours.

## 14. Future autonomous-trading compatibility

Full lineage is preserved per order, so a later learning phase can
query the whole chain:

```
model → prediction → signal → portfolio decision → risk decision
      → order intent → execution order → IBKR order → IBKR execution
      → fill → position → P&L
```

plus decision price, reference price, bid, ask, slippage, fees and
execution latency. No learning engine is implemented, and none should
be inferred from the data being present.

## 15. What was NOT validated

**This integration has not been run against a real IBKR paper
account.** There is no IBKR account, no gateway and no credential in
the environment where it was built, and obtaining one is the user's
decision, not something to work around.

What that means precisely:

- Every test runs against `MockIBKRTransport` — deterministic, and
  proving the adapter's logic, not IBKR's behaviour.
- The endpoint paths, payload shapes and status vocabulary come from
  IBKR's published documentation, not from observed traffic.
- The gap is real: IBKR may return a field shape the mapper mishandles,
  or a status not in the table (which becomes
  `RECONCILIATION_REQUIRED` rather than a wrong guess — the failure
  mode was chosen for exactly this).

`scripts/run_ibkr.py` without `--mock` is what closes that gap. The
runbook walks through it.
