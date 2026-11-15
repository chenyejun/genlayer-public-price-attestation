# Deploy notes — PublicPriceAttestation

## Contract

- File: `contracts/PublicPriceAttestation.py`
- Class: `PublicPriceAttestation`
- Runner (line 1, pinned): `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6`
  — the hash the official project boilerplate pins
  (`genlayer-project-boilerplate`, `contracts/football_bets.py`), verified
  against the boilerplate on 2026-11-13. Keep the blank line after it.
- `genvm-lint check` passes (3 checks) and validation passes: 9 methods
  (7 view, 2 write). Note: the linter reports a newer runner is available
  (`1zr6nqk...`); the contract targets the boilerplate's runner, and
  moving to a newer runner requires the renamed APIs, not just the hash —
  do not change the hash alone.
- Direct tests: 17 passed
  (`./.venv/bin/python -m pytest tests/direct -q`).

## Constructor

**No constructor arguments.** `__init__` takes none and only zeroes the
two counters. Deploy from Studio (https://studio.genlayer.com/contracts,
StudioNet network) or the CLI:

```bash
genlayer deploy --contract contracts/PublicPriceAttestation.py
```

No value needs to be sent with any call. Consensus rounds (fetching
pages + model calls) run on the leader and validators; Studio shows the
fee estimate before you confirm — if an `attest_price` fee looks wrong,
do not confirm; re-check the pair first.

## Post-deploy checklist

Run these on Studio against the deployed address, in order. Prices
move, so the expected values below are ranges, not exact numbers:
before each `attest_price`, check the live price of the pair on
CoinGecko or Binance and confirm the attested price is within a few
percent of it.

### 1. `request_attestation` — happy path

Args:

- `pair`: `BTC/USD`

Expected: returns `{request_id: 1, pair: "BTC/USD", status:
"REQUESTED"}`. Then call `get_request(1)` and confirm `pair:
"BTC/USD"`, `status: "REQUESTED"`, and `requester` is your address.

### 2. `request_attestation` — pair normalization

Args: `pair`: `btc/usd`.

Expected: returns a new request id (2) with `pair: "BTC/USD"` —
lowercase input is normalized to uppercase. A second request for the
same pair is allowed; each request is attested independently.

### 3. `request_attestation` — bad pair rejected

Args: `pair`: `BTCUSD` (no slash).

Expected: reverts with `[EXPECTED]` (`pair must be BASE/QUOTE`).

### 4. `attest_price` — happy path

Args: `request_id: 1`.

Expected: returns `{attestation_id: 1, request_id: 1, pair:
"BTC/USD", price: <median in micro-USD>, sources_loaded: 2 or 3,
status: "CURRENT", valid_until: <verified_at + 3600>}`. The price
should be close to the live BTC price: at the time of writing BTC is
around $65,000, so expect a price near `65000000000` (micro-USD) —
check the live price first and accept anything within a few percent.
Then call `get_attestation(1)` and confirm: `price` matches the
returned price, `prices` has one entry per loaded source (each with
`source` and `price`), `sources_loaded` is 2 or 3, `valid_until` is
exactly one hour after `verified_at`, `status: "CURRENT"`. Call
`get_request(1)` and confirm `status: "ATTESTED"`.

If it reverts with `[TRANSIENT]`, a source was briefly unreachable —
wait and retry the same call. If it reverts with `[EXTERNAL]`, a source
endpoint is down — wait and retry; the endpoints are public and
usually recover quickly. If it reverts with `[LLM_ERROR]`, the model
returned a malformed answer — retry once.

### 5. `attest_price` — already attested

Args: `request_id: 1`.

Expected: reverts with `[EXPECTED]` (`request 1 is already ATTESTED`).

### 6. `get_latest_attestation` and `is_fresh` — happy path

- `get_latest_attestation("BTC/USD")` → attestation 1, `fresh: true`,
  `price` near the live BTC price.
- `is_fresh("BTC/USD")` → `{attestation_id: 1, fresh: true}`.
- `is_fresh("ETH/USD")` → `{attestation_id: 0, fresh: false}` — a pair
  with no attestation is simply not fresh; it does not revert.
- `get_latest_attestation("ETH/USD")` → reverts with `[EXPECTED]`
  (no attestation for pair).

### 7. Second pair — `ETH/USD`

1. `request_attestation("ETH/USD")` → `{request_id: 3, ...}`.
2. `attest_price(3)` → expect `{attestation_id: 2, pair: "ETH/USD",
   price: <near the live ETH price in micro-USD>, sources_loaded: 2 or
   3}`.
3. `get_latest_attestation("ETH/USD")` → attestation 2, `fresh: true`.
4. `get_latest_attestation("BTC/USD")` → still attestation 1 — the two
   pairs are independent.

### 8. Refresh — a new request attests a new price

1. `request_attestation("BTC/USD")` → `{request_id: 4, ...}`.
2. `attest_price(4)` → expect `{attestation_id: 3, pair: "BTC/USD",
   price: <near the live BTC price>}`. The price may differ slightly
   from attestation 1 — that is expected, the market moved.
3. `get_latest_attestation("BTC/USD")` → attestation 3.
4. `get_attestation(1)` → still readable, `status: "CURRENT"` (old
   attestations are history, never deleted).

### 9. Views (read-only, no fee)

- `list_requests()` → all requests (4 after step 8).
- `list_attestations()` → all attestations (3 after step 8).
- `stats()` → `{requests: 4, attestations: 3, fresh_attestations: 3}`
  (numbers grow if you ran extra requests; `fresh_attestations` drops
  to 0 one hour after the last attestation).

## If something looks wrong

- Deploy rejected with `invalid_contract`: the Depends line is the
  usual suspect — confirm line 1 is exactly the pinned hash and line 2
  is blank.
- `attest_price` reverts `[LLM_ERROR]`: the model returned something
  outside the expected shape. Retry once; if it persists, the pages
  may be too long or too ambiguous — the three endpoints are fixed, so
  wait and retry later.
- `attest_price` reverts `[TRANSIENT]` repeatedly: one of the three
  public endpoints is having a bad day. The round needs at least two
  sources; check the endpoints in a browser (CoinGecko simple price,
  Binance ticker, Kraken ticker) and retry when at least two are up.
- `attest_price` hangs or times out on Studio: the consensus round is
  waiting on validators. Wait for the Studio timeout message; nothing
  was written. Retry.
- The attested price is far from the live price: do not trust it —
  check `get_attestation` to see which sources loaded and what prices
  were extracted. A price from only two sources during a fast move can
  lag the live price by a few percent; request a fresh attestation.
