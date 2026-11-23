"""Direct tests for PublicPriceAttestation — mocked web + LLM, no network.

Every consensus round runs against mocks: price pages are stubbed with
mock_web, model answers with mock_llm. run_validator() replays the
captured validator with the current mocks, so swapping the mocks
between the leader run and the replay simulates a validator that sees
different external data.
"""

import json
import re

from tests.direct.conftest import to_hex

URL_CG = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
URL_BIN = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
URL_KRA = "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"

SOURCES_3 = URL_CG + "," + URL_BIN + "," + URL_KRA
SOURCES_2 = URL_CG + "," + URL_BIN

# Prices in micro-USD: $65,000 -> 65_000_000_000.
P_CG = 65000000000
P_BIN = 65050000000
P_KRA = 64980000000
MEDIAN_3 = 65000000000
MEDIAN_2 = 65025000000  # two sources: integer average of 65000000000 and 65050000000

PAGE_CG = '{"bitcoin":{"usd":65000}}'
PAGE_BIN = '{"symbol":"BTCUSDT","price":"65050.00"}'
PAGE_KRA = '{"result":{"XXBTZUSD":{"c":["64980.0","1"]}}}'


def _prices_answer(prices):
    """Model answer: one price per source, in source order."""
    return {
        "prices": [
            {"source": URL_CG, "price_micro": prices[0]},
            {"source": URL_BIN, "price_micro": prices[1]},
            {"source": URL_KRA, "price_micro": prices[2]},
        ]
    }


def _mock_pages(vm, pages):
    for url, body in pages.items():
        vm.mock_web(re.escape(url), {"status": 200, "body": body})


def _mock_page_status(vm, url, status):
    vm.mock_web(re.escape(url), {"status": status, "body": ""})


def _mock_llm(vm, answer):
    vm.mock_llm("price_micro", json.dumps(answer))


def _request(vm, contract, pair="BTC/USD", sources=SOURCES_3):
    return contract.request_attestation(pair, sources)


def _attest_3_sources(vm, contract, request_id=1, prices=(P_CG, P_BIN, P_KRA)):
    _mock_pages(vm, {URL_CG: PAGE_CG, URL_BIN: PAGE_BIN, URL_KRA: PAGE_KRA})
    _mock_llm(vm, _prices_answer(prices))
    return contract.attest_price(request_id)


# -- requests ------------------------------------------------------------


def test_request_stores_request(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/PublicPriceAttestation.py")
    direct_vm.sender = direct_alice

    result = _request(direct_vm, contract)

    assert result["request_id"] == 1
    assert result["status"] == "REQUESTED"

    req = contract.get_request(1)
    assert req["pair"] == "BTC/USD"
    assert req["sources"] == [URL_CG, URL_BIN, URL_KRA]
    assert req["status"] == "REQUESTED"
    assert req["requester"] == to_hex(direct_alice)
    assert req["requested_at"] > 0
    assert req["found"] is True


def test_request_normalizes_pair_to_uppercase(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/PublicPriceAttestation.py")

    _request(direct_vm, contract, pair="btc/usd")

    assert contract.get_request(1)["pair"] == "BTC/USD"


def test_request_validation(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/PublicPriceAttestation.py")

    # Fewer than two sources.
    with direct_vm.expect_revert("[EXPECTED]"):
        _request(direct_vm, contract, sources=URL_CG)
    # Four sources.
    with direct_vm.expect_revert("[EXPECTED]"):
        _request(direct_vm, contract, sources=SOURCES_3 + "," + URL_CG)
    # Duplicate sources.
    with direct_vm.expect_revert("[EXPECTED]"):
        _request(direct_vm, contract, sources=URL_CG + "," + URL_CG)
    # Non-HTTPS source.
    with direct_vm.expect_revert("[EXPECTED]"):
        _request(
            direct_vm,
            contract,
            sources="http://api.coingecko.com/api/v3/simple/price," + URL_BIN,
        )
    # Bad pair shapes.
    with direct_vm.expect_revert("[EXPECTED]"):
        _request(direct_vm, contract, pair="BTCUSD")
    with direct_vm.expect_revert("[EXPECTED]"):
        _request(direct_vm, contract, pair="BTC/")
    with direct_vm.expect_revert("[EXPECTED]"):
        _request(direct_vm, contract, pair="BTC/USD/ETH")
    with direct_vm.expect_revert("[EXPECTED]"):
        _request(direct_vm, contract, pair="   ")


def test_re_requesting_a_pair_is_allowed(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/PublicPriceAttestation.py")

    first = _request(direct_vm, contract)
    second = _request(direct_vm, contract)

    assert first["request_id"] == 1
    assert second["request_id"] == 2
    assert len(contract.list_requests()["requests"]) == 2


# -- attestation ---------------------------------------------------------


def test_attest_stores_median_and_validity(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/PublicPriceAttestation.py")
    direct_vm.sender = direct_alice
    direct_vm.warp("2026-11-16T00:00:00Z")
    _request(direct_vm, contract)

    result = _attest_3_sources(direct_vm, contract)

    assert result["attestation_id"] == 1
    assert result["request_id"] == 1
    assert result["pair"] == "BTC/USD"
    assert result["price"] == MEDIAN_3
    assert result["status"] == "ATTESTED"

    att = contract.get_attestation(1)
    assert att["price"] == MEDIAN_3
    assert att["prices"] == [P_CG, P_BIN, P_KRA]
    assert att["sources"] == [URL_CG, URL_BIN, URL_KRA]
    assert att["sources_loaded"] == 3
    assert att["sources_failed"] == 0
    assert att["verified_by"] == to_hex(direct_alice)
    assert att["verified_at"] > 0
    # One hour of validity, in seconds.
    assert att["valid_until"] == att["verified_at"] + 3600

    assert contract.get_request(1)["status"] == "ATTESTED"
    assert contract.get_latest_attestation("BTC/USD")["attestation_id"] == 1
    fresh = contract.is_fresh("BTC/USD")
    assert fresh["fresh"] is True
    assert fresh["attestation_id"] == 1


def test_validator_agrees_with_leader(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/PublicPriceAttestation.py")
    _request(direct_vm, contract)
    _attest_3_sources(direct_vm, contract)

    # Replays the captured validator with the same mocks.
    assert direct_vm.run_validator() is True


def test_validator_agrees_within_tolerance(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/PublicPriceAttestation.py")
    _request(direct_vm, contract)
    _attest_3_sources(direct_vm, contract)

    # The validator extracts slightly different prices: its median is
    # $65,300 against the leader's $65,000 — a 0.46% gap, inside the
    # 1% band.
    direct_vm.clear_mocks()
    _mock_pages(direct_vm, {URL_CG: PAGE_CG, URL_BIN: PAGE_BIN, URL_KRA: PAGE_KRA})
    _mock_llm(
        direct_vm, _prices_answer((65300000000, 65400000000, 65200000000))
    )

    assert direct_vm.run_validator() is True


def test_validator_disagrees_when_median_gap_exceeds_tolerance(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/PublicPriceAttestation.py")
    _request(direct_vm, contract)
    _attest_3_sources(direct_vm, contract)

    # The validator extracts $70,000 across the board: a 7.7% gap.
    direct_vm.clear_mocks()
    _mock_pages(direct_vm, {URL_CG: PAGE_CG, URL_BIN: PAGE_BIN, URL_KRA: PAGE_KRA})
    _mock_llm(
        direct_vm, _prices_answer((70000000000, 70100000000, 69900000000))
    )

    assert direct_vm.run_validator() is False


def _leader_result(prices, price, loaded, failed):
    """A leader's attestation result, as the validator receives it."""
    return {
        "prices": list(prices),
        "price": price,
        "sources_loaded": loaded,
        "sources_failed": failed,
    }


def test_validator_rejects_median_mismatch(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/PublicPriceAttestation.py")
    _request(direct_vm, contract)
    _attest_3_sources(direct_vm, contract)

    # The price list is valid and the counts add up, but the accepted
    # price is 1% above the median the list supports. The gap is
    # inside the 1% tolerance band, so only the recomputation check
    # can catch it.
    assert direct_vm.run_validator(
        leader_result=_leader_result(
            [P_CG, P_BIN, P_KRA], MEDIAN_3 + 650000000, 3, 0
        )
    ) is False


def test_validator_rejects_sources_loaded_mismatch(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/PublicPriceAttestation.py")
    _request(direct_vm, contract)
    _attest_3_sources(direct_vm, contract)

    # Three prices in the list, but the leader claims only two loaded.
    assert direct_vm.run_validator(
        leader_result=_leader_result([P_CG, P_BIN, P_KRA], MEDIAN_3, 2, 1)
    ) is False


def test_validator_rejects_sources_count_mismatch(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/PublicPriceAttestation.py")
    _request(direct_vm, contract)
    _attest_3_sources(direct_vm, contract)

    # Two prices, two loaded, zero failed — but three sources were
    # requested, so one source is unaccounted for.
    assert direct_vm.run_validator(
        leader_result=_leader_result([P_CG, P_BIN], MEDIAN_2, 2, 0)
    ) is False


def test_validator_rejects_non_positive_price_in_list(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/PublicPriceAttestation.py")
    _request(direct_vm, contract)
    _attest_3_sources(direct_vm, contract)

    # A zero in the price list, even though the median and the counts
    # look right.
    assert direct_vm.run_validator(
        leader_result=_leader_result([0, P_BIN, P_KRA], P_KRA, 3, 0)
    ) is False


def test_validator_accepts_two_source_result(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/PublicPriceAttestation.py")
    _request(direct_vm, contract)
    _attest_3_sources(direct_vm, contract)

    # Two prices, two loaded, one failed: a complete, consistent
    # result from a round where one source was down.
    assert direct_vm.run_validator(
        leader_result=_leader_result([P_CG, P_BIN], MEDIAN_2, 2, 1)
    ) is True


def test_two_sources_loading_is_enough(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/PublicPriceAttestation.py")
    _request(direct_vm, contract)

    # Kraken is down with a 404; the other two load.
    _mock_pages(direct_vm, {URL_CG: PAGE_CG, URL_BIN: PAGE_BIN})
    _mock_page_status(direct_vm, URL_KRA, 404)
    _mock_llm(
        direct_vm,
        {
            "prices": [
                {"source": URL_CG, "price_micro": P_CG},
                {"source": URL_BIN, "price_micro": P_BIN},
            ]
        },
    )
    result = contract.attest_price(1)

    # Median of two values is their integer average.
    assert result["price"] == MEDIAN_2
    att = contract.get_attestation(1)
    assert att["sources_loaded"] == 2
    assert att["sources_failed"] == 1
    assert att["prices"] == [P_CG, P_BIN]


def test_fewer_than_two_sources_loading_fails(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/PublicPriceAttestation.py")
    _request(direct_vm, contract)

    # Two sources fail with client errors: classified [EXTERNAL].
    _mock_pages(direct_vm, {URL_CG: PAGE_CG})
    _mock_page_status(direct_vm, URL_BIN, 404)
    _mock_page_status(direct_vm, URL_KRA, 404)
    _mock_llm(direct_vm, _prices_answer((P_CG, P_BIN, P_KRA)))

    with direct_vm.expect_revert("[EXTERNAL]"):
        contract.attest_price(1)
    assert contract.get_request(1)["status"] == "REQUESTED"


def test_all_sources_server_error_is_transient(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/PublicPriceAttestation.py")
    _request(direct_vm, contract)

    _mock_page_status(direct_vm, URL_CG, 500)
    _mock_page_status(direct_vm, URL_BIN, 503)
    _mock_page_status(direct_vm, URL_KRA, 429)

    with direct_vm.expect_revert("[TRANSIENT]"):
        contract.attest_price(1)


def test_bad_model_answers_are_llm_errors(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/PublicPriceAttestation.py")
    _request(direct_vm, contract)

    # No prices list at all.
    _mock_pages(direct_vm, {URL_CG: PAGE_CG, URL_BIN: PAGE_BIN, URL_KRA: PAGE_KRA})
    _mock_llm(direct_vm, {"price": 65000000000})
    with direct_vm.expect_revert("[LLM_ERROR]"):
        contract.attest_price(1)

    # A loaded source is missing from the answer.
    direct_vm.clear_mocks()
    _mock_pages(direct_vm, {URL_CG: PAGE_CG, URL_BIN: PAGE_BIN, URL_KRA: PAGE_KRA})
    _mock_llm(
        direct_vm,
        {
            "prices": [
                {"source": URL_CG, "price_micro": P_CG},
                {"source": URL_BIN, "price_micro": P_BIN},
            ]
        },
    )
    with direct_vm.expect_revert("[LLM_ERROR]"):
        contract.attest_price(1)

    # A price is a float, not an integer.
    direct_vm.clear_mocks()
    _mock_pages(direct_vm, {URL_CG: PAGE_CG, URL_BIN: PAGE_BIN, URL_KRA: PAGE_KRA})
    _mock_llm(
        direct_vm,
        {
            "prices": [
                {"source": URL_CG, "price_micro": 65000.5},
                {"source": URL_BIN, "price_micro": P_BIN},
                {"source": URL_KRA, "price_micro": P_KRA},
            ]
        },
    )
    with direct_vm.expect_revert("[LLM_ERROR]"):
        contract.attest_price(1)

    # A price is not positive.
    direct_vm.clear_mocks()
    _mock_pages(direct_vm, {URL_CG: PAGE_CG, URL_BIN: PAGE_BIN, URL_KRA: PAGE_KRA})
    _mock_llm(
        direct_vm,
        {
            "prices": [
                {"source": URL_CG, "price_micro": 0},
                {"source": URL_BIN, "price_micro": P_BIN},
                {"source": URL_KRA, "price_micro": P_KRA},
            ]
        },
    )
    with direct_vm.expect_revert("[LLM_ERROR]"):
        contract.attest_price(1)

    assert contract.get_request(1)["status"] == "REQUESTED"


def test_attest_twice_and_unknown_ids_rejected(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/PublicPriceAttestation.py")
    _request(direct_vm, contract)
    _attest_3_sources(direct_vm, contract)

    with direct_vm.expect_revert("[EXPECTED]"):
        contract.attest_price(1)
    with direct_vm.expect_revert("[EXPECTED]"):
        contract.attest_price(999)
    with direct_vm.expect_revert("[EXPECTED]"):
        contract.get_request(999)
    with direct_vm.expect_revert("[EXPECTED]"):
        contract.get_attestation(999)
    with direct_vm.expect_revert("[EXPECTED]"):
        contract.get_latest_attestation("ETH/USD")


def test_string_ids_accepted_from_studio_form(direct_vm, direct_deploy):
    """Studio's write form sends numbers as JSON strings."""
    contract = direct_deploy("contracts/PublicPriceAttestation.py")
    _request(direct_vm, contract)
    # Re-registering a mock under the same key does not replace the old
    # one, so clear first, then mock the new prices.
    direct_vm.clear_mocks()
    _mock_pages(direct_vm, {URL_CG: PAGE_CG, URL_BIN: PAGE_BIN, URL_KRA: PAGE_KRA})
    _mock_llm(direct_vm, _prices_answer((P_CG, P_BIN, P_KRA)))

    result = contract.attest_price("1")
    assert result["attestation_id"] == 1

    assert contract.get_request("1")["status"] == "ATTESTED"
    assert contract.get_attestation("1")["price"] == MEDIAN_3
    assert type(contract.get_attestation("1")["price"]) is int


def test_is_fresh_expires_after_one_hour(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/PublicPriceAttestation.py")
    direct_vm.warp("2026-11-16T00:00:00Z")
    _request(direct_vm, contract)
    _attest_3_sources(direct_vm, contract)

    assert contract.is_fresh("BTC/USD")["fresh"] is True

    # One hour later the attestation is history, not fresh.
    direct_vm.warp("2026-11-16T01:00:01Z")
    fresh = contract.is_fresh("BTC/USD")
    assert fresh["fresh"] is False
    assert fresh["attestation_id"] == 1
    # ...but the attestation itself is still readable.
    assert contract.get_latest_attestation("BTC/USD")["price"] == MEDIAN_3

    # A pair with no attestation is simply not fresh, no revert.
    assert contract.is_fresh("ETH/USD")["fresh"] is False
    assert contract.is_fresh("ETH/USD")["attestation_id"] == 0


def test_refresh_creates_a_new_attestation(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/PublicPriceAttestation.py")
    _request(direct_vm, contract)
    _attest_3_sources(direct_vm, contract)

    _request(direct_vm, contract)
    # Re-registering a mock under the same key does not replace the old
    # one, so clear first, then mock the new prices.
    direct_vm.clear_mocks()
    _mock_pages(direct_vm, {URL_CG: PAGE_CG, URL_BIN: PAGE_BIN, URL_KRA: PAGE_KRA})
    _mock_llm(direct_vm, _prices_answer((66000000000, 66050000000, 65980000000)))
    result = contract.attest_price(2)

    assert result["attestation_id"] == 2
    assert result["price"] == 66000000000
    assert contract.get_latest_attestation("BTC/USD")["attestation_id"] == 2
    assert contract.get_attestation(1)["price"] == MEDIAN_3


def test_views_and_stats(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/PublicPriceAttestation.py")
    _request(direct_vm, contract)
    _request(direct_vm, contract, pair="ETH/USD", sources=SOURCES_2)
    _attest_3_sources(direct_vm, contract, request_id=1)

    requests = contract.list_requests()
    assert len(requests["requests"]) == 2
    assert requests["requests"][0]["pair"] == "BTC/USD"
    assert requests["requests"][1]["pair"] == "ETH/USD"

    atts = contract.list_attestations()
    assert len(atts["attestations"]) == 1
    assert atts["attestations"][0]["pair"] == "BTC/USD"

    assert contract.stats() == {
        "requests": 2,
        "attestations": 1,
        "fresh_attestations": 1,
    }
