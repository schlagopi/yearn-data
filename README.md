# yearn-data

Reusable Python tooling for indexing Yearn vault data and running research jobs on top of normalized onchain data.

The first analysis job is `lifetime-yield`, which backfills Yearn V2/V3 `StrategyReported` events, prices report-time vault asset gains/losses, and exports aggregate yield totals.

## Quick Start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
# Optional, recommended for best historical DeFi-native pricing:
pip install -e '.[yprice]'

yearn-data init-db
yearn-data discover
yearn-data index-events
yearn-data price
yearn-data analyze lifetime-yield
yearn-data export lifetime-yield
```

By default the CLI loads RPC and API values from `.env` in this repo, then `/home/bot/bots/yvusd-bots/.env`.

## Useful Options

```bash
yearn-data --db data/yearn.sqlite discover --chains eth arb kat
yearn-data index-events --to-block 25400000 --chunk-size 5000
yearn-data run lifetime-yield
```

The SQLite database stores raw event rows, normalized strategy reports, prices, resumable cursors, and analysis outputs so later research jobs can reuse the same indexed data.

## Lifetime Yield Outputs

The `lifetime-yield` aggregate columns `gross_gain_usd`, `loss_usd`, and `net_yield_usd` are economic totals adjusted for known Yearn public incident disclosures where vault reports emitted paper losses or compensating phantom profits. The raw report-time accounting values remain available as `raw_gross_gain_usd`, `raw_loss_usd`, and `raw_net_yield_usd`.

Rows changed by an incident adjustment are marked in `reports.csv` with `is_adjusted`, `incident_id`, `incident_classification`, `incident_description`, and `incident_disclosure_url`. Raw indexed `strategy_reports` rows in SQLite are not modified.

## Pricing

The `price` command supports three modes:

```bash
yearn-data price --source defillama
yearn-data price --source yprice
yearn-data price --source defillama --fallback yprice
```

DefiLlama is the default primary source. `yprice` uses `ypricemagic` historical block pricing when that optional dependency is installed and is used as the default fallback for assets DefiLlama cannot price. All source/status rows are stored in SQLite.

## Backfill Efficiency

Event indexing uses resumable `eth_getLogs` block chunks and dedupes logs by `(chain_id, tx_hash, log_index)`. The default chunk size is `50,000` blocks, intended for Tenderly-style archive RPCs; lower it if an RPC returns block range errors. Block timestamps are cached in the local database after first lookup.
