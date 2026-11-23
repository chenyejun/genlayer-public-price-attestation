# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

"""PublicPriceAttestation — a price oracle attestation registry.

Anyone requests an attestation for an asset pair (for example BTC/USD)
together with two or three public price sources. Validator consensus
fetches those sources on chain, extracts the current price from each,
and agrees on the median. The agreed price is stored with its sources
and a validity window, so other contracts and readers can check the
price a pair was trading at without trusting a single feed or a single
caller.

Why consensus, and why the median: a price is a number, not a
judgement, so agreement is numeric. The leader and each validator
independently extract one price per source and the contract derives
the median from the extracted list in deterministic code — the model
never outputs the agreed price itself, only the per-source facts. The
two medians must land within 1% of each other. A single stale or
manipulated feed cannot move the attestation: the median of two or
three independent sources absorbs one outlier, and a round where fewer
than two sources load fails instead of storing a one-feed price.
Before any result is stored, the validator verifies the complete
result: the per-source price list must be valid, its recomputed
median must match the accepted price, and the loaded/failed counts
must be consistent with that list and the requested sources.

Prices go stale, so attestations expire. An attestation is fresh for
one hour after verification; after that it stays readable as history
but is_fresh reports false and a new request is needed. A fresh
attestation can be requested again at any time — re-requesting a pair
is the refresh path, not an error.

Price unit: micro-units of the quote currency. The model returns the
price multiplied by 1,000,000 as an integer (for example $65,000.50
is 65000500000), so no floating point ever enters the contract.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from genlayer import *


# Error prefixes. Failures are raised as gl.vm.UserError with one of
# these so the leader and the validators can tell deterministic failures
# apart from transient ones: [EXPECTED] and [EXTERNAL] must match
# exactly, [TRANSIENT] only has to be transient on both sides, and
# [LLM_ERROR] never agrees, so a bad model answer rotates the leader
# instead of locking bad state in.
ERR_EXPECTED = "[EXPECTED]"
ERR_EXTERNAL = "[EXTERNAL]"
ERR_TRANSIENT = "[TRANSIENT]"
ERR_LLM = "[LLM_ERROR]"


# Agreement band for the agreed price: the leader's median and the
# validator's median must be within this many percent of each other.
# Two independent extractions of the same live price differ by
# fractions of a percent (rounding, a few seconds of market movement);
# a genuinely different reading breaks consensus. The band is relative,
# so it works the same for a $65,000 pair and a $0.50 pair.
PRICE_TOLERANCE_PERCENT = u256(1)

# An attestation is fresh for one hour after verification.
VALIDITY_SECONDS = u256(3600)

# Bounds. A contract that reads the open web needs limits that do not
# depend on the pages behaving: every transaction has a compute budget,
# and the leader's return value is written to the chain.
MAX_PAIR_CHARS = 20
MAX_URL_CHARS = 500
MIN_SOURCES = 2
MAX_SOURCES = 3
MAX_PAGE_CHARS = 6000
PRICE_SCALE = 1000000

STATUS_REQUESTED = "REQUESTED"
STATUS_ATTESTED = "ATTESTED"


@allow_storage
@dataclass
class PriceRequest:
    request_id: u256
    pair: str
    # Source URLs, joined with "\n". URLs are validated to contain no
    # whitespace, so the join is unambiguous and reversible.
    sources: str
    requester: Address
    requested_at: u256
    status: str


@allow_storage
@dataclass
class PriceAttestation:
    attestation_id: u256
    request_id: u256
    pair: str
    # Agreed median price, in micro-units of the quote currency.
    price: u256
    # Per-source extracted prices, in the same unit, in source order.
    prices: DynArray[u256]
    # Sources actually checked, joined with "\n".
    sources: str
    verified_by: Address
    verified_at: u256
    valid_until: u256
    sources_loaded: u256
    sources_failed: u256


def _fail(prefix: str, detail: str) -> None:
    raise gl.vm.UserError(prefix + " " + detail)


def _now_seconds() -> u256:
    """Current transaction time as unix seconds.

    The GenVM wires the standard library clock to the transaction
    datetime, so every validator observes the same value.
    """
    return u256(int(datetime.now(timezone.utc).timestamp()))


def _as_u256(value, label: str) -> u256:
    """Coerce one numeric argument to u256.

    Studio's write form serializes numbers as JSON strings, and the
    pinned py-genlayer's u256() is a pass-through (typing.NewType), so
    it does not convert them. int() normalizes both forms (a no-op on
    values that are already ints). A string that is not a whole number
    raises a clean [EXPECTED] revert here instead of letting a bare
    ValueError escape the contract.
    """
    try:
        return u256(int(value))
    except (TypeError, ValueError):
        raise gl.vm.UserError("[EXPECTED] " + label + " must be a whole number")


def _clean_pair(pair: str) -> str:
    """Validate and normalize an asset pair: BASE/QUOTE, uppercased.

    Both sides are short alphanumeric codes. The pair is the lookup key
    for the latest attestation, so the same pair spelled two ways must
    not create two registries.
    """
    text = str(pair).strip().upper()
    if len(text) == 0:
        _fail(ERR_EXPECTED, "pair must not be empty")
    if len(text) > MAX_PAIR_CHARS:
        _fail(ERR_EXPECTED, "pair must be " + str(MAX_PAIR_CHARS) + " characters or fewer")
    parts = text.split("/")
    if len(parts) != 2:
        _fail(ERR_EXPECTED, "pair must look like BASE/QUOTE, for example BTC/USD")
    for part in parts:
        if len(part) == 0 or len(part) > 10:
            _fail(ERR_EXPECTED, "pair must look like BASE/QUOTE, for example BTC/USD")
        for ch in part:
            if not (("A" <= ch <= "Z") or ("0" <= ch <= "9")):
                _fail(ERR_EXPECTED, "pair must look like BASE/QUOTE, for example BTC/USD")
    return text


def _clean_url(url: str) -> str:
    """Validate one price source URL. HTTPS only, no credentials, no
    explicit port, no whitespace, and a dotted DNS hostname (never an
    IP literal). URL syntax alone never makes a source trustworthy —
    that is the requester's choice, and the attestation records exactly
    which sources the price came from."""
    text = str(url).strip()
    if len(text) == 0:
        _fail(ERR_EXPECTED, "source URL must not be empty")
    if len(text) > MAX_URL_CHARS:
        _fail(ERR_EXPECTED, "source URL must be " + str(MAX_URL_CHARS) + " characters or fewer")
    if not text.startswith("https://"):
        _fail(ERR_EXPECTED, "source URL must start with https://: " + text)
    if text != "".join(text.split()):
        _fail(ERR_EXPECTED, "source URL must not contain whitespace: " + text)
    rest = text[len("https://"):]
    authority_end = len(rest)
    for separator in ("/", "?", "#"):
        found = rest.find(separator)
        if found >= 0 and found < authority_end:
            authority_end = found
    authority = rest[:authority_end]
    if len(authority) == 0:
        _fail(ERR_EXPECTED, "source URL has no host: " + text)
    if "@" in authority:
        _fail(ERR_EXPECTED, "source URL must not contain credentials: " + text)
    if ":" in authority:
        _fail(ERR_EXPECTED, "source URL must not contain a port: " + text)
    host = authority.lower()
    if "." not in host:
        _fail(ERR_EXPECTED, "source URL host must be a dotted domain: " + text)
    return text


def _parse_sources(sources: str) -> list:
    """Split the comma-separated sources argument. The CLI passes
    strings, numbers and booleans only — a list has to go through a
    deploy script — so the public shape is one string and the splitting
    happens here."""
    parsed = [part.strip() for part in str(sources).split(",")]
    parsed = [part for part in parsed if part != ""]
    if len(parsed) < MIN_SOURCES:
        _fail(ERR_EXPECTED, "at least " + str(MIN_SOURCES) + " source URLs are required")
    if len(parsed) > MAX_SOURCES:
        _fail(ERR_EXPECTED, "at most " + str(MAX_SOURCES) + " source URLs, got " + str(len(parsed)))
    cleaned = [_clean_url(url) for url in parsed]
    if len(set(cleaned)) != len(cleaned):
        _fail(ERR_EXPECTED, "source URLs must be distinct")
    return cleaned


def _median(values: list) -> u256:
    """Median of a non-empty list of prices, in deterministic code.

    Odd count takes the middle value; even count takes the integer
    average of the two middle values. The median is derived here, never
    copied from model output, so both consensus sides compute the same
    function of whatever they extracted.
    """
    ordered = sorted(values)
    count = len(ordered)
    if count % 2 == 1:
        return u256(ordered[count // 2])
    middle = (int(ordered[count // 2 - 1]) + int(ordered[count // 2])) // 2
    return u256(middle)


def _within_tolerance(own: u256, leader: u256) -> bool:
    """Relative agreement check on two medians.

    The gap between the two values must be within
    PRICE_TOLERANCE_PERCENT of the leader's value. u256 subtraction
    cannot go negative, so the gap is computed both ways, the way the
    score check in a milestone review does it.
    """
    if own >= leader:
        gap = own - leader
    else:
        gap = leader - own
    return gap * u256(100) <= leader * PRICE_TOLERANCE_PERCENT


def _error_text(exc) -> str:
    """Read the message out of a UserError / Result, across the runner
    versions that name the payload `.data` or `.message`."""
    for attr in ("data", "message"):
        value = getattr(exc, attr, None)
        if value is not None:
            return str(value)
    return str(exc)


def _leader_error_is_agreeable(leaders_res, judge) -> bool:
    """Decide whether to agree with a leader that failed. Deterministic
    failures (a bad argument, a 4xx) must match exactly. A transient
    failure only has to be transient on both sides. An LLM failure
    always disagrees, so consensus rotates rather than locking bad
    state."""
    leader_msg = _error_text(leaders_res)
    try:
        judge()
    except gl.vm.UserError as exc:
        validator_msg = _error_text(exc)
        if validator_msg.startswith(ERR_EXPECTED) or validator_msg.startswith(ERR_EXTERNAL):
            return validator_msg == leader_msg
        if validator_msg.startswith(ERR_TRANSIENT) and leader_msg.startswith(ERR_TRANSIENT):
            return True
        return False
    except Exception:
        return False
    # The leader failed where this validator succeeded: disagree.
    return False


def _validate_prices(answer, loaded: list) -> list:
    """Check the model's per-source prices; return the prices in
    source order.

    Every source that loaded must have exactly one entry, with the URL
    copied exactly and the price a positive integer in micro-units.
    Entries for sources that did not load are dropped — that is either
    a hallucination or an injected URL, and neither may be stored.
    Any deviation is an [LLM_ERROR]: a model that cannot report prices
    in the required shape is not a model whose numbers can be stored.
    """
    if not isinstance(answer, dict):
        _fail(ERR_LLM, "model answer was not a JSON object")
    raw = answer.get("prices")
    if not isinstance(raw, list):
        _fail(ERR_LLM, "model answer has no prices list")
    by_source = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        price = entry.get("price_micro")
        if not isinstance(source, str):
            continue
        if source not in loaded:
            continue
        if (
            not isinstance(price, int)
            or isinstance(price, bool)
            or price <= 0
        ):
            _fail(ERR_LLM, "model price is not a positive integer")
        by_source[source] = price
    prices = []
    for url in loaded:
        if url not in by_source:
            _fail(ERR_LLM, "model answer has no price for a loaded source")
        prices.append(by_source[url])
    return prices


def _verify_attestation_result(result: dict, requested_sources: list) -> bool:
    """Verify a complete attestation result before anyone trusts it.

    The result a consensus round stores must stand on its own: the
    per-source price list is valid (one positive integer per loaded
    source), the median recomputed from that list matches the
    accepted price exactly, and the loaded/failed counts add up to
    the sources that were requested. A leader that reports a median
    its own price list does not support, or counts that do not match
    its list, is not agreed with — the numbers in the stored record
    must check out, not rest on the leader's word.
    """
    if not isinstance(result, dict):
        return False
    prices = result.get("prices")
    if not isinstance(prices, list):
        return False
    if len(prices) < MIN_SOURCES:
        return False
    for price in prices:
        if (
            not isinstance(price, int)
            or isinstance(price, bool)
            or price <= 0
        ):
            return False
    loaded = result.get("sources_loaded")
    failed = result.get("sources_failed")
    if (
        not isinstance(loaded, int)
        or isinstance(loaded, bool)
        or not isinstance(failed, int)
        or isinstance(failed, bool)
    ):
        return False
    if len(prices) != loaded:
        return False
    if loaded + failed != len(requested_sources):
        return False
    accepted = result.get("price")
    if (
        not isinstance(accepted, int)
        or isinstance(accepted, bool)
        or accepted <= 0
    ):
        return False
    return _median(prices) == u256(accepted)


# --------------------------------------------------------------------------
# Consensus worker (module level: it takes plain values, touches no
# storage, and is shared by leader and validators, so both sides run
# the same code).
# --------------------------------------------------------------------------
def _judge(pair: str, sources: list) -> dict:
    """Fetch the sources, ask the model for one price per source, and
    return the judgement: per-source prices, the median, and how many
    sources loaded. Raises a prefixed gl.vm.UserError on failure."""
    loaded = []
    failed = u256(0)
    failed_client = u256(0)
    pages = []
    for url in sources:
        try:
            resp = gl.nondet.web.get(url)
        except Exception:
            failed = failed + u256(1)
            continue
        status = resp.status
        if status == 200:
            raw = resp.body
            if raw is None:
                text = ""
            elif isinstance(raw, bytes):
                text = raw.decode("utf-8", "ignore")
            else:
                text = str(raw)
            loaded.append(url)
            pages.append({"source": url, "text": text[:MAX_PAGE_CHARS]})
        elif 400 <= status < 500:
            failed = failed + u256(1)
            failed_client = failed_client + u256(1)
        else:
            failed = failed + u256(1)
    # A price from a single feed is not an attestation: at least two
    # independent sources must load, or the round fails instead of
    # storing a one-feed number.
    if len(loaded) < MIN_SOURCES:
        if failed_client == failed:
            _fail(ERR_EXTERNAL, "fewer than two price sources loaded, all with client errors")
        _fail(ERR_TRANSIENT, "fewer than two price sources loaded")
    base, quote = pair.split("/")
    prompt = (
        "You are extracting the current market price of an asset from "
        "the source pages below. Answer with JSON only, using exactly "
        "one key:\n"
        '- "prices": a list of objects with keys "source" and "price_micro"\n\n'
        "Rules:\n"
        '- "source" must be one of the URLs listed below, copied exactly.\n'
        '- "price_micro" is the current price of ' + base + " in " + quote
        + ", multiplied by 1000000, as an integer. For example a price of "
        "$65,000.50 is 65000500000. Never use a decimal point, never use "
        "a string.\n"
        "- Give exactly one entry for every source page listed below.\n"
        "- Read the price from the page itself. Do not use outside "
        "knowledge, do not guess, and do not convert between currencies."
    )
    prompt += "\n\nPair: " + pair
    prompt += "\n\nSources:\n" + json.dumps(pages)
    try:
        answer = gl.nondet.exec_prompt(prompt, response_format="json")
    except Exception as exc:
        _fail(ERR_LLM, "model call failed: " + str(exc))
    prices = _validate_prices(answer, loaded)
    return {
        "prices": prices,
        "price": _median(prices),
        "sources_loaded": u256(len(loaded)),
        "sources_failed": failed,
    }


def _request_to_dict(req: PriceRequest) -> dict:
    return {
        "request_id": req.request_id,
        "pair": req.pair,
        "sources": req.sources.split("\n") if req.sources != "" else [],
        "requester": req.requester.as_hex,
        "requested_at": req.requested_at,
        "status": req.status,
        "found": True,
    }


def _attestation_to_dict(att: PriceAttestation) -> dict:
    return {
        "attestation_id": att.attestation_id,
        "request_id": att.request_id,
        "pair": att.pair,
        "price": att.price,
        "prices": [int(p) for p in att.prices],
        "sources": att.sources.split("\n") if att.sources != "" else [],
        "verified_by": att.verified_by.as_hex,
        "verified_at": att.verified_at,
        "valid_until": att.valid_until,
        "sources_loaded": att.sources_loaded,
        "sources_failed": att.sources_failed,
        "found": True,
    }


class PublicPriceAttestation(gl.Contract):
    """Registry of consensus-attested public prices.

    Requests are made once per attestation; a pair can be requested
    again at any time to refresh its price. Attestations are never
    deleted: an old attestation stays readable as history after its
    validity window passes, and get_latest_attestation always returns
    the newest one for the pair.
    """

    requests: TreeMap[u256, PriceRequest]
    attestations: TreeMap[u256, PriceAttestation]
    request_ids: DynArray[u256]
    attestation_ids: DynArray[u256]
    request_counter: u256
    attestation_counter: u256

    def __init__(self):
        self.request_counter = u256(0)
        self.attestation_counter = u256(0)

    # -- consensus ------------------------------------------------------
    # One round with gl.vm.run_nondet_unsafe. The leader and each
    # validator re-run _judge and their medians must agree within the
    # 1% relative tolerance. The median itself is derived by
    # deterministic code on both sides, never copied from model output,
    # and no strict_eq is used on any LLM output anywhere in this
    # contract: a byte-equality check on extracted prices would fail
    # whenever two validators read the same live price seconds apart.
    # The validator also verifies the leader's complete result before
    # agreeing: a valid per-source price list, the median recomputed
    # from that list matching the accepted price, and loaded/failed
    # counts consistent with the list and the requested sources. The
    # same check runs once more in attest_price before the result is
    # stored, so the stored record is verified as a whole.
    def _attest_with_consensus(self, pair: str, sources: list) -> dict:
        def leader_fn() -> dict:
            return _judge(pair, sources)

        def validator_fn(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return _leader_error_is_agreeable(
                    leaders_res, lambda: _judge(pair, sources)
                )
            try:
                mine = _judge(pair, sources)
            except Exception:
                return False
            leader = leaders_res.calldata
            if not isinstance(leader, dict):
                return False
            leader_price = leader.get("price")
            if (
                not isinstance(leader_price, int)
                or isinstance(leader_price, bool)
                or leader_price <= 0
            ):
                return False
            if leader.get("sources_loaded", 0) < MIN_SOURCES:
                return False
            if mine["sources_loaded"] < MIN_SOURCES:
                return False
            # The stored result must stand on its own: a valid
            # per-source price list, a median recomputed from that
            # list matching the accepted price, and loaded/failed
            # counts consistent with the list and the requested
            # sources.
            if not _verify_attestation_result(leader, sources):
                return False
            return _within_tolerance(mine["price"], u256(int(leader_price)))

        return gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

    # -- public writes --------------------------------------------------
    @gl.public.write
    def request_attestation(self, pair: str, sources: str) -> dict:
        """Request a price attestation for a pair, from 2-3 public
        price sources (comma-separated). Returns the new request id.
        Re-requesting a pair is the refresh path: it is always
        allowed, even while a fresh attestation exists."""
        clean_pair = _clean_pair(pair)
        parsed = _parse_sources(sources)
        self.request_counter = self.request_counter + u256(1)
        request_id = self.request_counter
        self.requests[request_id] = PriceRequest(
            request_id=request_id,
            pair=clean_pair,
            sources="\n".join(parsed),
            requester=gl.message.sender_address,
            requested_at=_now_seconds(),
            status=STATUS_REQUESTED,
        )
        self.request_ids.append(request_id)
        return {"request_id": request_id, "status": STATUS_REQUESTED}

    @gl.public.write
    def attest_price(self, request_id: u256) -> dict:
        """Run validator consensus on a requested pair and store the
        attestation. Anyone may call this: the caller decides nothing,
        consensus does. One request is attested once; a fresh price
        needs a new request."""
        request_id = _as_u256(request_id, "request id")
        if request_id not in self.requests:
            _fail(ERR_EXPECTED, "unknown request " + str(request_id))
        request = self.requests[request_id]
        if request.status != STATUS_REQUESTED:
            _fail(ERR_EXPECTED, "request " + str(request_id) + " is already " + request.status)
        # Storage cannot be read inside a nondet block: copy first,
        # then hand the consensus round plain in-memory values.
        request_mem = gl.storage.copy_to_memory(request)
        pair = request_mem.pair
        sources = request_mem.sources.split("\n")
        result = self._attest_with_consensus(pair, sources)
        # The record that gets stored must verify as a whole, even
        # though consensus already checked it: a valid per-source
        # price list, a median recomputed from that list, and counts
        # consistent with the requested sources.
        if not _verify_attestation_result(result, sources):
            _fail(ERR_EXPECTED, "consensus result failed attestation verification")
        verified_at = _now_seconds()
        self.attestation_counter = self.attestation_counter + u256(1)
        attestation_id = self.attestation_counter
        self.attestations[attestation_id] = PriceAttestation(
            attestation_id=attestation_id,
            request_id=request_id,
            pair=pair,
            price=u256(int(result["price"])),
            prices=[u256(int(p)) for p in result["prices"]],
            sources="\n".join(sources),
            verified_by=gl.message.sender_address,
            verified_at=verified_at,
            valid_until=verified_at + VALIDITY_SECONDS,
            sources_loaded=u256(int(result["sources_loaded"])),
            sources_failed=u256(int(result["sources_failed"])),
        )
        self.attestation_ids.append(attestation_id)
        request.status = STATUS_ATTESTED
        self.requests[request_id] = request
        return {
            "attestation_id": attestation_id,
            "request_id": request_id,
            "pair": pair,
            "price": int(result["price"]),
            "valid_until": verified_at + VALIDITY_SECONDS,
            "status": STATUS_ATTESTED,
        }

    # -- public views ---------------------------------------------------
    @gl.public.view
    def get_request(self, request_id: u256) -> dict:
        request_id = _as_u256(request_id, "request id")
        if request_id not in self.requests:
            _fail(ERR_EXPECTED, "unknown request " + str(request_id))
        return _request_to_dict(self.requests[request_id])

    @gl.public.view
    def get_attestation(self, attestation_id: u256) -> dict:
        attestation_id = _as_u256(attestation_id, "attestation id")
        if attestation_id not in self.attestations:
            _fail(ERR_EXPECTED, "unknown attestation " + str(attestation_id))
        return _attestation_to_dict(self.attestations[attestation_id])

    @gl.public.view
    def get_latest_attestation(self, pair: str) -> dict:
        """The attestation a reader should trust for a pair: the
        newest one. Raises when the pair has none yet. Attestation
        ids grow with time, so scanning backwards finds the latest
        attestation for the pair."""
        clean_pair = _clean_pair(pair)
        index = len(self.attestation_ids) - 1
        while index >= 0:
            attestation = self.attestations[self.attestation_ids[index]]
            if attestation.pair == clean_pair:
                return _attestation_to_dict(attestation)
            index = index - 1
        _fail(ERR_EXPECTED, "pair " + clean_pair + " has no attestation yet")

    @gl.public.view
    def is_fresh(self, pair: str) -> dict:
        """Whether the latest attestation for a pair is still inside
        its validity window. Never reverts: a pair with no attestation
        is simply not fresh."""
        clean_pair = _clean_pair(pair)
        index = len(self.attestation_ids) - 1
        while index >= 0:
            latest = self.attestations[self.attestation_ids[index]]
            if latest.pair == clean_pair:
                return {
                    "pair": clean_pair,
                    "fresh": bool(_now_seconds() <= latest.valid_until),
                    "attestation_id": latest.attestation_id,
                    "valid_until": latest.valid_until,
                }
            index = index - 1
        return {
            "pair": clean_pair,
            "fresh": False,
            "attestation_id": u256(0),
            "valid_until": u256(0),
        }

    @gl.public.view
    def list_requests(self) -> dict:
        return {"requests": [_request_to_dict(self.requests[rid]) for rid in self.request_ids]}

    @gl.public.view
    def list_attestations(self) -> dict:
        return {
            "attestations": [
                _attestation_to_dict(self.attestations[aid]) for aid in self.attestation_ids
            ]
        }

    @gl.public.view
    def stats(self) -> dict:
        fresh = u256(0)
        now = _now_seconds()
        for attestation_id in self.attestation_ids:
            if self.attestations[attestation_id].valid_until >= now:
                fresh = fresh + u256(1)
        return {
            "requests": u256(len(self.request_ids)),
            "attestations": u256(len(self.attestation_ids)),
            "fresh_attestations": fresh,
        }
