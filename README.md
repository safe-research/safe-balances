# safe-balances

Generates a CSV of SAFE token holders with their mainnet balance, Gnosis Chain balance, staking
balance, and remaining (unclaimed) investor vesting amount.

Output columns: `address, balance_mainnet, balance_gnosis, staking, vesting`

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env  # defaults to a free public RPC gateway; edit if you want your own
```

## Getting the holder-list CSVs

Etherscan/Gnosisscan's holder export requires a logged-in browser session, so it can't be
downloaded by the script. While logged in, open these URLs in your browser and save the CSV:

- Mainnet: https://etherscan.io/exportData?type=tokenholders&contract=0x5afe3855358e112b5647b952709e6165e1c1eeee&decimal=18
- Gnosis Chain: https://gnosisscan.io/exportData?type=tokenholders&contract=0x4d18815d14fe5c3304e87b3fa18318baa5c23820&decimal=18

## Usage

```bash
python generate_holders_csv.py \
  --mainnet-holders-csv mainnet_holders.csv \
  --gnosis-holders-csv gnosis_holders.csv \
  --output safe_holders.csv
```

Staking and vesting are read live from Ethereum mainnet via the configured RPC endpoint:
- Staking: sums `StakeIncreased` events on the Safenet Beta staking contract
  (`0x115E78f160e1E3eF163B05C84562Fa16fA338509`) into each staker's current
  `totalStakerStakes(address)` balance. The set of discovered staker addresses and the last
  block scanned are cached in `staking_stakers_cache.json` (see `--staker-cache-file`), so
  re-running the script only scans blocks since the previous run instead of rescanning from the
  contract's deployment block every time. Use `--no-staker-cache` to force a full rescan.
- Vesting: follows the [`safe-research/check_vestings`](https://github.com/safe-research/check_vestings)
  approach against the investor `VestingPool` contract (`0x96b71e2551915d98d22c448b040a3bc4801ea4ff`),
  reporting `amount - amountClaimed` (total unclaimed, whether already unlocked or not) per current
  on-chain owner. Only investor vestings are covered; there is no Gnosis Chain vesting contract.

## Tests

```bash
pytest
```
