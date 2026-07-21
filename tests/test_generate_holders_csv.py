from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from web3 import Web3

import generate_holders_csv as ghc
from generate_holders_csv import (
    ZERO_ADDRESS,
    _address_from_topic,
    build_rpc_url,
    compute_vesting_left,
    fetch_investor_vestings,
    fetch_staking_balances,
    load_staker_cache,
    multicall_aggregate,
    parse_holder_csv,
    save_staker_cache,
)

ADDR_1 = "0x1111111111111111111111111111111111111111"
ADDR_2 = "0x2222222222222222222222222222222222222222"


def write_csv(path, text):
    path.write_text(text, encoding="utf-8")
    return path


# -- parse_holder_csv ---------------------------------------------------------------------------


def test_parse_holder_csv_basic(tmp_path):
    csv_path = write_csv(
        tmp_path / "holders.csv",
        '"HolderAddress","Balance","PercentageA"\n'
        f'"{ADDR_1}","1234.56","0.01"\n'
        f'"{ADDR_2}","500","0.001"\n',
    )
    result = parse_holder_csv(csv_path)
    assert result == {
        Web3.to_checksum_address(ADDR_1): Decimal("1234.56"),
        Web3.to_checksum_address(ADDR_2): Decimal("500"),
    }


def test_parse_holder_csv_column_name_variants(tmp_path):
    csv_path = write_csv(tmp_path / "holders.csv", f'"Address","Quantity"\n"{ADDR_1}","42"\n')
    result = parse_holder_csv(csv_path)
    assert result == {Web3.to_checksum_address(ADDR_1): Decimal("42")}


def test_parse_holder_csv_sums_duplicate_addresses(tmp_path):
    csv_path = write_csv(
        tmp_path / "holders.csv",
        f'"Address","Balance"\n"{ADDR_1}","1"\n"{ADDR_1}","2"\n',
    )
    result = parse_holder_csv(csv_path)
    assert result[Web3.to_checksum_address(ADDR_1)] == Decimal("3")


def test_parse_holder_csv_skips_blank_addresses(tmp_path):
    csv_path = write_csv(tmp_path / "holders.csv", f'"Address","Balance"\n"","1"\n"{ADDR_1}","2"\n')
    result = parse_holder_csv(csv_path)
    assert result == {Web3.to_checksum_address(ADDR_1): Decimal("2")}


def test_parse_holder_csv_missing_column_raises(tmp_path):
    csv_path = write_csv(tmp_path / "holders.csv", '"Foo","Bar"\n"1","2"\n')
    with pytest.raises(ValueError):
        parse_holder_csv(csv_path)


# -- fetch_investor_vestings ---------------------------------------------------------------------


def test_fetch_investor_vestings_local_path(tmp_path):
    csv_path = write_csv(
        tmp_path / "investor_vestings.csv",
        "vestingId,owner,amount,startDate,duration\n"
        f"0xabc,{ADDR_1},100,1000,208\n"
        f",{ADDR_2},200,1000,208\n",
    )
    assert fetch_investor_vestings(str(csv_path)) == ["0xabc"]


# -- compute_vesting_left ------------------------------------------------------------------------


def _vesting_result(account, amount, amount_claimed):
    return (account, 0, False, 208, 1000, amount, amount_claimed, 0, False)


def test_compute_vesting_left_sums_per_account():
    checksum_1 = Web3.to_checksum_address(ADDR_1)
    results = {
        "0xaa": _vesting_result(checksum_1, 100, 40),
        "0xbb": _vesting_result(checksum_1, 50, 50),
    }

    w3 = MagicMock()

    def vestings_side_effect(vesting_id_bytes):
        key = "0x" + vesting_id_bytes.hex()
        call_mock = MagicMock()
        call_mock.call.return_value = results[key]
        return call_mock

    w3.eth.contract.return_value.functions.vestings.side_effect = vestings_side_effect

    left = compute_vesting_left(w3, ["0xaa", "0xbb"])
    assert left == {checksum_1: Decimal(60) / Decimal(10**18)}


def test_compute_vesting_left_skips_zero_address_and_continues_past_errors(capsys):
    checksum_1 = Web3.to_checksum_address(ADDR_1)
    results = {
        "0xaa": _vesting_result(ZERO_ADDRESS, 0, 0),
        "0xcc": _vesting_result(checksum_1, 10, 0),
    }

    w3 = MagicMock()

    def vestings_side_effect(vesting_id_bytes):
        key = "0x" + vesting_id_bytes.hex()
        if key == "0xbb":
            raise RuntimeError("rpc boom")
        call_mock = MagicMock()
        call_mock.call.return_value = results[key]
        return call_mock

    w3.eth.contract.return_value.functions.vestings.side_effect = vestings_side_effect

    left = compute_vesting_left(w3, ["0xaa", "0xbb", "0xcc"])
    assert left == {checksum_1: Decimal(10) / Decimal(10**18)}
    assert "rpc boom" in capsys.readouterr().err


# -- staking helpers ------------------------------------------------------------------------------


def test_address_from_topic():
    padded = bytes(12) + bytes.fromhex(ADDR_1.removeprefix("0x"))
    topic = MagicMock()
    topic.hex.return_value = "0x" + padded.hex()
    assert _address_from_topic(topic) == Web3.to_checksum_address(ADDR_1)


def test_staker_cache_round_trip(tmp_path):
    cache_path = tmp_path / "cache.json"
    checksum_1 = Web3.to_checksum_address(ADDR_1)
    save_staker_cache(cache_path, 123, {checksum_1})
    last_block, stakers = load_staker_cache(cache_path)
    assert last_block == 123
    assert stakers == {checksum_1}


def test_load_staker_cache_missing_file_returns_empty(tmp_path):
    last_block, stakers = load_staker_cache(tmp_path / "missing.json")
    assert last_block is None
    assert stakers == set()


def _log_with_staker(address):
    padded_topic = MagicMock()
    padded_topic.hex.return_value = "0x" + (bytes(12) + bytes.fromhex(address.removeprefix("0x"))).hex()
    return {"topics": [MagicMock(), padded_topic]}


def _encode_uint256(value):
    return Web3().codec.encode(["uint256"], [value])


def _stub_multicall(monkeypatch, balances_by_target_order=None, fixed_balance=None):
    """Patch multicall_aggregate to return one (True, encoded-uint256) result per call, in order."""

    def fake_multicall(w3, calls, batch_size=ghc.MULTICALL_BATCH_SIZE):
        if balances_by_target_order is not None:
            return [(True, _encode_uint256(v)) for v in balances_by_target_order]
        return [(True, _encode_uint256(fixed_balance)) for _ in calls]

    monkeypatch.setattr(ghc, "multicall_aggregate", fake_multicall)


def test_fetch_staking_balances_resumes_from_cache(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"
    checksum_1 = Web3.to_checksum_address(ADDR_1)
    save_staker_cache(cache_path, 999, {checksum_1})

    w3 = Web3()
    monkeypatch.setattr(type(w3.eth), "block_number", 999)  # nothing new to scan
    get_logs_mock = MagicMock()
    monkeypatch.setattr(w3.eth, "get_logs", get_logs_mock)
    _stub_multicall(monkeypatch, fixed_balance=5 * 10**18)

    balances = fetch_staking_balances(w3, from_block=100, chunk_size=50, cache_path=cache_path)

    get_logs_mock.assert_not_called()
    assert balances == {checksum_1: Decimal(5)}

    _, cached_stakers = load_staker_cache(cache_path)
    assert cached_stakers == {checksum_1}


def test_fetch_staking_balances_scans_new_blocks_and_merges_with_cache(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"
    checksum_1 = Web3.to_checksum_address(ADDR_1)
    checksum_2 = Web3.to_checksum_address(ADDR_2)
    save_staker_cache(cache_path, 100, {checksum_1})

    w3 = Web3()
    monkeypatch.setattr(type(w3.eth), "block_number", 150)
    get_logs_mock = MagicMock(return_value=[_log_with_staker(checksum_2)])
    monkeypatch.setattr(w3.eth, "get_logs", get_logs_mock)
    _stub_multicall(monkeypatch, fixed_balance=1 * 10**18)

    balances = fetch_staking_balances(w3, from_block=1, chunk_size=1000, cache_path=cache_path)

    # only scans from where the cache left off (101), not from from_block (1)
    called_kwargs = get_logs_mock.call_args[0][0]
    assert called_kwargs["fromBlock"] == 101
    assert balances == {checksum_1: Decimal(1), checksum_2: Decimal(1)}

    last_block, cached_stakers = load_staker_cache(cache_path)
    assert last_block == 150
    assert cached_stakers == {checksum_1, checksum_2}


def test_fetch_staking_balances_no_cache_scans_from_from_block(monkeypatch):
    w3 = Web3()
    monkeypatch.setattr(type(w3.eth), "block_number", 50)
    get_logs_mock = MagicMock(return_value=[])
    monkeypatch.setattr(w3.eth, "get_logs", get_logs_mock)
    _stub_multicall(monkeypatch, balances_by_target_order=[])

    fetch_staking_balances(w3, from_block=1, chunk_size=1000, cache_path=None)

    called_kwargs = get_logs_mock.call_args[0][0]
    assert called_kwargs["fromBlock"] == 1


def test_fetch_staking_balances_omits_zero_balances(monkeypatch):
    checksum_1 = Web3.to_checksum_address(ADDR_1)
    checksum_2 = Web3.to_checksum_address(ADDR_2)

    w3 = Web3()
    monkeypatch.setattr(type(w3.eth), "block_number", 10)
    monkeypatch.setattr(
        w3.eth,
        "get_logs",
        MagicMock(return_value=[_log_with_staker(checksum_1), _log_with_staker(checksum_2)]),
    )
    # sorted() order determines which balance lines up with which staker
    ordered_stakers = sorted([checksum_1, checksum_2])
    balances_in_order = [0 if s == checksum_1 else 3 * 10**18 for s in ordered_stakers]
    _stub_multicall(monkeypatch, balances_by_target_order=balances_in_order)

    balances = fetch_staking_balances(w3, from_block=1, chunk_size=1000, cache_path=None)

    assert balances == {checksum_2: Decimal(3)}


def test_multicall_aggregate_batches_and_preserves_order():
    w3 = MagicMock()
    contract_mock = w3.eth.contract.return_value

    def aggregate3_side_effect(call3_structs):
        call_mock = MagicMock()
        call_mock.call.return_value = [(True, target.encode()) for target, _allow_failure, _data in call3_structs]
        return call_mock

    contract_mock.functions.aggregate3.side_effect = aggregate3_side_effect

    calls = [(f"target{i}", b"data") for i in range(5)]
    results = multicall_aggregate(w3, calls, batch_size=2)

    assert [return_data for _success, return_data in results] == [c[0].encode() for c in calls]
    assert contract_mock.functions.aggregate3.call_count == 3  # batches of 2, 2, 1


def test_multicall_aggregate_surfaces_per_call_failure():
    w3 = MagicMock()
    w3.eth.contract.return_value.functions.aggregate3.return_value.call.return_value = [
        (False, b""),
        (True, b"ok"),
    ]

    results = multicall_aggregate(w3, [("a", b"1"), ("b", b"2")])

    assert results == [(False, b""), (True, b"ok")]


# -- build_rpc_url --------------------------------------------------------------------------------


def test_build_rpc_url_prefers_explicit_arg(monkeypatch):
    monkeypatch.setenv("ETH_RPC_URL", "https://env-url")
    assert build_rpc_url("https://arg-url") == "https://arg-url"


def test_build_rpc_url_falls_back_to_env_rpc_url(monkeypatch):
    monkeypatch.delenv("INFURA_API_KEY", raising=False)
    monkeypatch.setenv("ETH_RPC_URL", "https://env-url")
    assert build_rpc_url(None) == "https://env-url"


def test_build_rpc_url_falls_back_to_infura_key(monkeypatch):
    monkeypatch.delenv("ETH_RPC_URL", raising=False)
    monkeypatch.setenv("INFURA_API_KEY", "abc123")
    assert build_rpc_url(None) == "https://mainnet.infura.io/v3/abc123"


def test_build_rpc_url_raises_without_any_config(monkeypatch):
    monkeypatch.delenv("ETH_RPC_URL", raising=False)
    monkeypatch.delenv("INFURA_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        build_rpc_url(None)
