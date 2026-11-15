# PublicPriceAttestation

A reusable on-chain price oracle for public markets: anyone requests an
attestation for a trading pair, validator consensus fetches the price
from three public sources, and the contract stores the median price
with a one-hour freshness window, so other contracts and readers can
use the attested price without re-running the check.

## Why consensus is needed

A price reported by one party is a claim, not a fact. A single
submitter could report any number, and a single model call could
misread a page. Here the price is produced by a leader and re-produced
independently by every validator, each fetching the same three public
sources, and it is only written to state when the two sides' medians
agree within 1%. The median itself is computed by deterministic code
from the extracted prices, never copied from model output, so the
model's job is only to read numbers off pages — it never decides the
attested price.

## What it does

- `request_attestation` — request an attestation for a pair (e.g.
  `BTC/USD`, normalized to uppercase). Returns the request id.
- `attest_price` — run validator consensus on a requested pair and
  store the attestation: the median price in micro-units of the quote
  currency (price × 1,000,000, as an integer), how many sources loaded,
  and the verification time. An attestation is fresh for one hour.
- Views: `get_request`, `get_attestation`, `get_latest_attestation`,
  `is_fresh` (never reverts), `list_requests`, `list_attestations`,
  `stats`.

An attestation is a snapshot, not a live feed: after one hour
(`VALIDITY_SECONDS = 3600`) it is still readable as history, but
`is_fresh` reports `false` and `get_latest_attestation` marks it
stale. Request a fresh attestation when you need a current price.

## State design

| Field | Type | Purpose |
| --- | --- | --- |
| `requests` | `TreeMap[u256, AttestationRequest]` | Requests by id |
| `attestations` | `TreeMap[u256, Attestation]` | Attestations by id |
| `request_ids` / `attestation_ids` | `DynArray[u256]` | Insertion order for listing and for scanning back to the latest attestation of a pair |
| `request_counter` / `attestation_counter` | `u256` | Monotonic ids |

`AttestationRequest` stores the pair, the requester, the request time,
and the status (`REQUESTED` / `ATTESTED`). `Attestation` stores the
request id, the pair, the median price in micro-units, the prices the
leader extracted from each source (`source | price` lines, one per
loaded source), the verification time, `valid_until`, and the status
(`CURRENT` / `EXPIRED` — expired by time, never deleted).

Two rules shape the design. First, a request is attested once: after
`attest_price` the request is `ATTESTED`, and a fresh price needs a new
request — there is no path that overwrites an attestation, so every
stored price stays auditable. Second, the pair index is a plain scan
backwards over `attestation_ids` rather than a `TreeMap[str,
DynArray[u256]]` keyed by pair: the nested-collection form is an
unproven construct in this runner, and the scan is short in practice.

## How the equivalence check works

Attestation runs one consensus round over the same judge code
(`_judge`: fetch each source with `gl.nondet.web.get`, ask the model
for one price per source with `gl.nondet.exec_prompt` and
`response_format="json"`, validate the answer, take the median). The
round is `gl.vm.run_nondet_unsafe(leader_fn, validator_fn)`.

**The leader and the validator each do the whole job.** Both sides
fetch the three sources (CoinGecko, Binance, Kraken — public JSON
endpoints, no keys), extract one price per source in micro-units, and
compute the median in deterministic code: with three prices the middle
value, with two the integer average of the two. The stored prices are
the leader's agreed set.

**Agreement is the median within 1%.** The validator requires
`|leader_median - own_median| / own_median <= 0.01`, computed in
integer arithmetic (`diff * 100 <= own * 1`). The tolerance exists
because the three sources legitimately disagree by small amounts at
any instant — a price taken a second apart on two exchanges is not the
same number, and a byte-equality bar would fail honest validators for
no reason. What the tolerance does not forgive is a wrong read: a
validator that misread a price by more than 1% disagrees, and so does
one that read a different pair.

**Two of three sources is enough.** Each source is fetched
independently and a failure is skipped, not fatal: with two sources
loaded the median is their average; with fewer than two the round
fails. The per-source failure class is recorded (`loaded` /
`failed` counts on the request), so a stored attestation says how many
sources backed it.

**Leader failures.** If the leader fails, the validator re-runs the
judge and compares failure classes: `[EXPECTED]` and `[EXTERNAL]` must
match exactly, `[TRANSIENT]` only has to be transient on both sides,
and `[LLM_ERROR]` never agrees, so a bad model answer rotates the
leader instead of locking bad state in.

Storage is never read inside a nondet block: `attest_price` copies the
request to memory with `gl.storage.copy_to_memory` first and hands the
consensus round plain in-memory values. The leader and validator
functions never reference `self`.

No `strict_eq` is used on any LLM output anywhere in the contract:
model-extracted numbers are compared with the 1% tolerance rule above,
not byte equality.

## Failure paths

| Prefix | Meaning | Examples | Caller should |
| --- | --- | --- | --- |
| `[EXPECTED]` | Bad input or invalid state | Empty or malformed pair, unknown request id, attesting an already-attested request, a price the model returned as a float or a non-positive number | Fix the input; no retry |
| `[EXTERNAL]` | The outside world is at fault, deterministically | A source returned 4xx (page gone, moved, or access denied); fewer than two sources loaded because of 4xx failures | Retry only if the source comes back; a dead source means the pair cannot be attested from these endpoints |
| `[TRANSIENT]` | The outside world failed, possibly temporarily | A source returned 5xx or 429, or fewer than two sources loaded because of transient failures | Retry later; nothing was written |
| `[LLM_ERROR]` | The model answer was unusable | Non-JSON answer, a missing price for a loaded source, a price for a source that did not load, a price in the wrong units | Retry; the leader rotates, so a persistent failure means the pages are the problem |

A failed `attest_price` writes nothing: the request stays
`REQUESTED` and the call can be retried.

## Methods

| Method | Access | Arguments | Returns |
| --- | --- | --- | --- |
| `request_attestation` | write | `pair: str` (e.g. `BTC/USD`) | `{request_id, pair, status}` |
| `attest_price` | write | `request_id: u256` | `{attestation_id, request_id, pair, price, sources_loaded, status, valid_until}` |
| `get_request` | view | `request_id: u256` | Request dict |
| `get_attestation` | view | `attestation_id: u256` | Attestation dict (prices as `[{source, price}]`) |
| `get_latest_attestation` | view | `pair: str` | Latest attestation for the pair, with `fresh: bool` |
| `is_fresh` | view | `pair: str` | `{attestation_id, fresh}` — never reverts |
| `list_requests` | view | — | `{requests: [...]}` |
| `list_attestations` | view | — | `{attestations: [...]}` |
| `stats` | view | — | `{requests, attestations, fresh_attestations}` |

Notes on arguments: prices are stored in micro-units of the quote
currency — a BTC price of $65,000 is stored as `65000000000`. The
model is instructed to return prices in micro-units, and the contract
validates that every price is a positive integer (a float, a string,
or a non-positive value is an `[LLM_ERROR]`). Pairs are normalized to
uppercase and must match `BASE/QUOTE` with 1–10 letters per side.

## Deploying on StudioNet

Prerequisites: a GenLayer Studio account and the Studio app at
https://studio.genlayer.com/contracts.

1. Open Studio, switch to the StudioNet network, and create a new
   contract from `contracts/PublicPriceAttestation.py` (paste the file
   or upload it).
2. Deploy with **no constructor arguments** — `__init__` takes none.
   The first line of the file pins the runner:
   `# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }`.
   That is the hash the official project boilerplate pins
   (`genlayer-project-boilerplate`, `contracts/football_bets.py`), verified
   against the boilerplate on 2026-11-13. Keep the blank line after it:
   the runner parses the leading `#` lines as one JSON document, and a
   comment glued directly under the Depends line breaks deployment.
3. Note the deployed contract address. The full step-by-step checklist,
   including the exact calls to run after deploy and what each should
   return, is in `DEPLOY_NOTES.md`.

CLI alternative (same network, same file):

```bash
genlayer network set testnet-bradbury   # or the StudioNet equivalent for your CLI version
genlayer deploy --contract contracts/PublicPriceAttestation.py
```

## Reading attestations from another contract

Use a typed interface for the methods you need. The interface is only
for type safety; at runtime it is the same as `gl.get_contract_at`.

```python
from genlayer import *


@gl.contract_interface
class PublicPriceAttestationReader:
    @gl.public.view
    def get_latest_attestation(self, pair: str) -> dict: ...


# ... inside a consumer contract, with the registry address in storage:
registry = gl.get_contract_at(PublicPriceAttestationReader, self.registry)
latest = registry.get_latest_attestation("BTC/USD")
# latest["price"] is the median in micro-USD; latest["fresh"] says
# whether it is within the one-hour window.
```

Two things to keep in mind when composing. First, cross-contract calls
are asynchronous: the child call is created when the parent transaction
reaches `on='finalized'` by default, so do not write a consumer that
expects the price back in the same synchronous step — design around
the message flow (for example, read the attestation in a view first,
then act on it in a separate call). Second, `get_latest_attestation`
raises `[EXPECTED]` when the pair has no attestation yet; handle that
in the consumer rather than assuming every pair is attested. A stale
attestation (`fresh: false`) is a definitive answer that the price is
old, not an error — only a missing attestation raises. Consumers that
need a current price should check `fresh` and request a new
attestation when it is `false`.

## Verifying after deploy

Run each write method once on Studio and check the result against
`DEPLOY_NOTES.md`. In short: request an attestation for `BTC/USD`,
read it back with `get_request`; attest it and confirm
`get_attestation` shows the median price (close to the live BTC price
on CoinGecko/Binance at the time), `sources_loaded` of 2 or 3, and
`valid_until` one hour after `verified_at`; confirm `is_fresh`
returns `true` for the pair and `false` for a pair with no
attestation; request and attest a second pair (`ETH/USD`) and confirm
`get_latest_attestation` returns the newer attestation per pair.

## Tests

Direct tests (mocked web + LLM, no network) live in
`tests/direct/test_public_price_attestation.py` and follow the official
`genlayer-test` pattern (`direct_vm` / `direct_deploy` fixtures). They
cover request validation and pair normalization, a successful
attestation with the median and the one-hour validity window, validator
agreement and disagreement (a 0.46% gap agrees, a 7.7% gap disagrees,
via mock swapping between leader and replay), two-of-three sources
loading (median becomes the integer average), fewer than two loading
(`[EXTERNAL]` for 4xx, `[TRANSIENT]` for 429), malformed model answers
(`[LLM_ERROR]`: missing prices, a price for a source that did not load,
a float price, a non-positive price), double attestation and unknown
ids, string ids from the Studio form, expiry via warp, refresh creating
a new attestation, and all views.

```bash
./.venv/bin/python -m pytest tests/direct -q
./.venv/bin/genvm-lint check contracts/PublicPriceAttestation.py
```

## Limitations

- **Attestations are snapshots, not a live feed.** A price is only as
  current as its `verified_at`. Consumers must check `fresh` and
  request a new attestation when the window has passed; the contract
  never refreshes itself.
- **Three fixed sources.** CoinGecko, Binance, and Kraken are
  hard-coded per pair (BTC and ETH pairs are mapped to their
  endpoints; other pairs fall back to the CoinGecko simple-price
  endpoint with the base symbol lowercased). A pair none of the
  endpoints serves cannot be attested.
- **Sources are chosen by the contract, not the requester.** The
  requester picks the pair; the sources are fixed. That is deliberate
  — a requester who picked the sources could pick three that agree
  with a price they want — but it means the attestation is only as
  good as those three endpoints.
- **1% tolerance is a judgement call.** Two exchanges can legitimately
  differ by more than 1% in a fast market, and a misread inside 1%
  passes. The tolerance trades a small amount of precision for
  consensus that honest validators can actually reach.
- **Runner version.** The contract pins the boilerplate's runner hash
  (`1jb45aa8...`, rc7-era APIs such as `gl.vm.run_nondet_unsafe` and
  `gl.get_contract_at`). The SDK's current docs describe a newer runner
  with renamed APIs (`run_nondet`, `gl.contract.get_at`, and others);
  moving to it requires updating the consensus calls, not just the hash.
