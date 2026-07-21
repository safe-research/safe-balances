#!/usr/bin/env python3
"""Generate a CSV of SAFE token holders: address, balance_mainnet, balance_gnosis, staking, vesting.

balance_mainnet/balance_gnosis come from manually-exported Etherscan/Gnosisscan token-holder
CSVs (their `exportData` endpoint requires a logged-in browser session, so it can't be fetched
automatically). staking and vesting are read live from Ethereum mainnet.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

import requests
from dotenv import load_dotenv
from tqdm import tqdm
from web3 import Web3

SAFE_DECIMALS = 18
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

VESTING_POOL_ADDRESS = "0x96b71e2551915d98d22c448b040a3bc4801ea4ff"
VESTING_POOL_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "", "type": "bytes32"}],
        "name": "vestings",
        "outputs": [
            {"name": "account", "type": "address"},
            {"name": "curveType", "type": "uint8"},
            {"name": "managed", "type": "bool"},
            {"name": "durationWeeks", "type": "uint16"},
            {"name": "startDate", "type": "uint64"},
            {"name": "amount", "type": "uint128"},
            {"name": "amountClaimed", "type": "uint128"},
            {"name": "pausingDate", "type": "uint64"},
            {"name": "cancelled", "type": "bool"},
        ],
        "type": "function",
    }
]
INVESTOR_VESTINGS_CSV_URL = (
    "https://raw.githubusercontent.com/safe-global/claiming-app-data/"
    "9fbbe2b90a4ca635a0883dd5cb45493695c70c3b/vestings/assets/1/investor_vestings.csv"
)

STAKING_ADDRESS = "0x115E78f160e1E3eF163B05C84562Fa16fA338509"
STAKING_ABI = [
    {
        "inputs": [{"name": "staker", "type": "address"}],
        "name": "totalStakerStakes",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]
STAKE_INCREASED_TOPIC = "0x" + Web3.keccak(
    text="StakeIncreased(address,address,uint256)"
).hex().removeprefix("0x")
DEFAULT_STAKING_FROM_BLOCK = 24585750  # staking contract's deployment block
DEFAULT_STAKER_CACHE_PATH = Path("staking_stakers_cache.json")

# Multicall3 is deployed at this same address on virtually every EVM chain, including mainnet.
# https://github.com/mds1/multicall3
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"
MULTICALL3_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "target", "type": "address"},
                    {"name": "allowFailure", "type": "bool"},
                    {"name": "callData", "type": "bytes"},
                ],
                "name": "calls",
                "type": "tuple[]",
            }
        ],
        "name": "aggregate3",
        "outputs": [
            {
                "components": [
                    {"name": "success", "type": "bool"},
                    {"name": "returnData", "type": "bytes"},
                ],
                "name": "returnData",
                "type": "tuple[]",
            }
        ],
        "stateMutability": "payable",
        "type": "function",
    }
]
MULTICALL_BATCH_SIZE = 500  # calls per aggregate3 invocation, to stay within provider response limits


def _normalize(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _find_column(fieldnames: list[str], keywords: tuple[str, ...]) -> str:
    for name in fieldnames:
        normalized = _normalize(name)
        if any(keyword in normalized for keyword in keywords):
            return name
    raise ValueError(f"could not find a column matching {keywords} in header {fieldnames}")


def parse_holder_csv(path: Path) -> dict[str, Decimal]:
    """Parse an Etherscan/Gnosisscan `exportData?type=tokenholders` CSV into address -> balance."""
    balances: dict[str, Decimal] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"{path}: no header row found")
        address_col = _find_column(reader.fieldnames, ("address",))
        balance_col = _find_column(reader.fieldnames, ("balance", "quantity"))
        for row in reader:
            raw_address = row[address_col].strip()
            if not raw_address:
                continue
            address = Web3.to_checksum_address(raw_address)
            balance = Decimal(row[balance_col].strip().replace(",", ""))
            balances[address] = balances.get(address, Decimal(0)) + balance
    return balances


def fetch_investor_vestings(source: str) -> list[str]:
    """Return the vestingId values from investor_vestings.csv (a local path or URL)."""
    if source.startswith("http://") or source.startswith("https://"):
        response = requests.get(source, timeout=30)
        response.raise_for_status()
        text = response.text
    else:
        text = Path(source).read_text(encoding="utf-8-sig")
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames:
        raise ValueError("investor_vestings.csv has no header row")
    id_col = _find_column(reader.fieldnames, ("vestingid",))
    return [row[id_col].strip() for row in reader if row[id_col].strip()]


def compute_vesting_left(w3: Web3, vesting_ids: list[str]) -> dict[str, Decimal]:
    """Sum `amount - amountClaimed` per current on-chain owner, across all given vestingIds."""
    contract = w3.eth.contract(address=Web3.to_checksum_address(VESTING_POOL_ADDRESS), abi=VESTING_POOL_ABI)
    left: dict[str, Decimal] = {}
    for vesting_id in tqdm(vesting_ids, desc="Fetching vestings"):
        try:
            vesting_id_bytes = bytes.fromhex(vesting_id.removeprefix("0x"))
            account, _curve_type, _managed, _duration_weeks, _start_date, amount, amount_claimed, _pausing_date, _cancelled = (
                contract.functions.vestings(vesting_id_bytes).call()
            )
        except Exception as exc:
            print(f"warning: failed to fetch vesting {vesting_id}: {exc}", file=sys.stderr)
            continue
        if account == ZERO_ADDRESS:
            continue
        account = Web3.to_checksum_address(account)
        remaining = Decimal(amount - amount_claimed) / Decimal(10**SAFE_DECIMALS)
        left[account] = left.get(account, Decimal(0)) + remaining
    return left


def multicall_aggregate(w3: Web3, calls: list[tuple[str, bytes]], batch_size: int = MULTICALL_BATCH_SIZE) -> list[tuple[bool, bytes]]:
    """Batch read-only `calls` (target address, ABI-encoded calldata) via Multicall3.aggregate3.

    Returns (success, returnData) per call, in the same order as `calls`, using far fewer RPC
    round-trips than issuing one eth_call per entry.
    """
    multicall = w3.eth.contract(address=Web3.to_checksum_address(MULTICALL3_ADDRESS), abi=MULTICALL3_ABI)
    results: list[tuple[bool, bytes]] = []
    for start in tqdm(range(0, len(calls), batch_size), desc="Multicall batches", disable=len(calls) <= batch_size):
        batch = calls[start : start + batch_size]
        call3_structs = [(target, True, call_data) for target, call_data in batch]
        results.extend(multicall.functions.aggregate3(call3_structs).call())
    return results


def _address_from_topic(topic) -> str:
    hex_str = topic.hex().removeprefix("0x")
    return Web3.to_checksum_address("0x" + hex_str[-40:])


def load_staker_cache(path: Path) -> tuple[int | None, set[str]]:
    """Return (last_scanned_block, stakers) from a previous run's cache, or (None, set()) if absent."""
    if not path.exists():
        return None, set()
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("last_scanned_block"), set(data.get("stakers", []))


def save_staker_cache(path: Path, last_scanned_block: int, stakers: set[str]) -> None:
    path.write_text(
        json.dumps({"last_scanned_block": last_scanned_block, "stakers": sorted(stakers)}, indent=2),
        encoding="utf-8",
    )


def fetch_staking_balances(
    w3: Web3,
    from_block: int,
    chunk_size: int,
    cache_path: Path | None = DEFAULT_STAKER_CACHE_PATH,
) -> dict[str, Decimal]:
    """Scan StakeIncreased logs to find stakers, then read each one's current staked balance.

    If `cache_path` is set, previously-discovered stakers and the last block scanned are loaded
    from it so a re-run only scans the block range since the previous run, instead of rescanning
    from `from_block` every time. Staked balances themselves are always read fresh.
    """
    staking_address = Web3.to_checksum_address(STAKING_ADDRESS)
    latest_block = w3.eth.block_number

    stakers: set[str] = set()
    scan_from = from_block
    if cache_path is not None:
        cached_last_block, cached_stakers = load_staker_cache(cache_path)
        stakers |= cached_stakers
        if cached_last_block is not None:
            scan_from = max(from_block, cached_last_block + 1)

    if scan_from <= latest_block:
        for start in tqdm(range(scan_from, latest_block + 1, chunk_size), desc="Scanning StakeIncreased logs"):
            end = min(start + chunk_size - 1, latest_block)
            logs = w3.eth.get_logs(
                {
                    "address": staking_address,
                    "topics": [STAKE_INCREASED_TOPIC],
                    "fromBlock": start,
                    "toBlock": end,
                }
            )
            for log in logs:
                stakers.add(_address_from_topic(log["topics"][1]))

    if cache_path is not None:
        save_staker_cache(cache_path, latest_block, stakers)

    contract = w3.eth.contract(address=staking_address, abi=STAKING_ABI)
    sorted_stakers = sorted(stakers)
    calls = [
        (staking_address, bytes.fromhex(contract.encode_abi(abi_element_identifier="totalStakerStakes", args=[staker]).removeprefix("0x")))
        for staker in sorted_stakers
    ]

    balances: dict[str, Decimal] = {}
    for staker, (success, return_data) in zip(sorted_stakers, multicall_aggregate(w3, calls)):
        if not success:
            print(f"warning: failed to fetch staking balance for {staker}", file=sys.stderr)
            continue
        (raw_balance,) = w3.codec.decode(["uint256"], return_data)
        if raw_balance:
            balances[staker] = Decimal(raw_balance) / Decimal(10**SAFE_DECIMALS)
    return balances


def build_rpc_url(rpc_url_arg: str | None) -> str:
    if rpc_url_arg:
        return rpc_url_arg
    env_url = os.environ.get("ETH_RPC_URL")
    if env_url:
        return env_url
    api_key = os.environ.get("INFURA_API_KEY")
    if not api_key:
        raise SystemExit(
            "No RPC endpoint configured. Set INFURA_API_KEY or ETH_RPC_URL (env var, .env, or --rpc-url)."
        )
    return f"https://mainnet.infura.io/v3/{api_key}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mainnet-holders-csv",
        required=True,
        type=Path,
        help="CSV exported from https://etherscan.io/exportData?type=tokenholders&contract=0x5afe3855358e112b5647b952709e6165e1c1eeee&decimal=18",
    )
    parser.add_argument(
        "--gnosis-holders-csv",
        required=True,
        type=Path,
        help="CSV exported from https://gnosisscan.io/exportData?type=tokenholders&contract=0x4d18815d14fe5c3304e87b3fa18318baa5c23820&decimal=18",
    )
    parser.add_argument("--output", default=Path("safe_holders.csv"), type=Path)
    parser.add_argument("--investor-vestings-csv", default=INVESTOR_VESTINGS_CSV_URL, help="Local path or URL")
    parser.add_argument("--staking-from-block", default=DEFAULT_STAKING_FROM_BLOCK, type=int)
    parser.add_argument("--log-chunk-size", default=5000, type=int)
    parser.add_argument("--rpc-url", default=None, help="Overrides INFURA_API_KEY/ETH_RPC_URL")
    parser.add_argument(
        "--staker-cache-file",
        default=DEFAULT_STAKER_CACHE_PATH,
        type=Path,
        help="Where to persist discovered staker addresses so re-runs only scan new blocks",
    )
    parser.add_argument(
        "--no-staker-cache",
        action="store_true",
        help="Ignore/skip the staker cache file and rescan StakeIncreased logs from scratch",
    )
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()

    w3 = Web3(Web3.HTTPProvider(build_rpc_url(args.rpc_url)))
    if not w3.is_connected():
        raise SystemExit("Could not connect to the configured RPC endpoint.")

    mainnet_balances = parse_holder_csv(args.mainnet_holders_csv)
    gnosis_balances = parse_holder_csv(args.gnosis_holders_csv)
    vesting_ids = fetch_investor_vestings(str(args.investor_vestings_csv))
    vesting_left = compute_vesting_left(w3, vesting_ids)
    staker_cache_path = None if args.no_staker_cache else args.staker_cache_file
    staking_balances = fetch_staking_balances(w3, args.staking_from_block, args.log_chunk_size, staker_cache_path)

    all_addresses = set(mainnet_balances) | set(gnosis_balances) | set(vesting_left) | set(staking_balances)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["address", "balance_mainnet", "balance_gnosis", "staking", "vesting"])
        for address in sorted(all_addresses):
            writer.writerow(
                [
                    address,
                    mainnet_balances.get(address, Decimal(0)),
                    gnosis_balances.get(address, Decimal(0)),
                    staking_balances.get(address, Decimal(0)),
                    vesting_left.get(address, Decimal(0)),
                ]
            )

    print(f"Wrote {len(all_addresses)} addresses to {args.output}")


if __name__ == "__main__":
    main()
