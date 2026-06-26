"""Historical pricing adapters."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any
import os
import json

import requests

from .config import CHAINS
from .config import get_rpc_url
from .storage import to_json


DEFILLAMA_BASE = "https://coins.llama.fi"
SUPPORTED_SOURCES = {"defillama", "yprice"}


def defillama_coin_id(chain_id: int, token_address: str) -> str:
    cfg = next(c for c in CHAINS.values() if c.chain_id == int(chain_id))
    return f"{cfg.defillama_slug}:{token_address.lower()}"


def fetch_defillama_price(chain_id: int, token_address: str, timestamp: int, timeout: int = 30) -> tuple[float | None, str, dict[str, Any]]:
    coin = defillama_coin_id(chain_id, token_address)
    url = f"{DEFILLAMA_BASE}/prices/historical/{int(timestamp)}/{coin}"
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    entry = payload.get("coins", {}).get(coin)
    if not entry or entry.get("price") is None:
        return None, "missing", payload
    return float(entry["price"]), "ok", payload


def fetch_defillama_prices_batch(
    requests_: list[tuple[int, str, int]],
    timeout: int = 30,
) -> dict[tuple[int, str, int], tuple[float | None, str, dict[str, Any]]]:
    coins: dict[str, list[int]] = {}
    key_by_coin: dict[str, tuple[int, str]] = {}
    for chain_id, token_address, timestamp in requests_:
        coin = defillama_coin_id(chain_id, token_address)
        coins.setdefault(coin, []).append(int(timestamp))
        key_by_coin[coin] = (int(chain_id), token_address)
    response = requests.get(
        f"{DEFILLAMA_BASE}/batchHistorical",
        params={"coins": json.dumps(coins, separators=(",", ":"))},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    output: dict[tuple[int, str, int], tuple[float | None, str, dict[str, Any]]] = {}
    for coin, timestamps in coins.items():
        chain_id, token_address = key_by_coin[coin]
        prices = payload.get("coins", {}).get(coin, {}).get("prices") or []
        for requested_ts in timestamps:
            key = (chain_id, token_address, int(requested_ts))
            if not prices:
                output[key] = (None, "missing", {"coin": coin, "timestamp": int(requested_ts)})
                continue
            matched = min(prices, key=lambda item: abs(int(item["timestamp"]) - int(requested_ts)))
            price = matched.get("price")
            if price is None:
                output[key] = (None, "missing", {"coin": coin, "timestamp": int(requested_ts), "matched": matched})
            else:
                output[key] = (float(price), "ok", {"coin": coin, "timestamp": int(requested_ts), "matched": matched})
    return output


def _brownie_network_id(chain_id: int) -> str:
    # ypricemagic uses Brownie network ids. These names match common Brownie config.
    mapping = {
        1: "yearn-mainnet",
        137: "yearn-polygon",
        8453: "yearn-base",
        42161: "yearn-arbitrum",
        747474: "yearn-katana",
    }
    if chain_id not in mapping:
        raise ValueError(f"yprice network mapping is not configured for chain_id={chain_id}")
    return mapping[chain_id]


def fetch_yprice_price(chain_id: int, token_address: str, block_number: int) -> tuple[float | None, str, dict[str, Any]]:
    """Fetch historical price from ypricemagic if the optional dependency is installed."""
    previous_network = os.environ.get("BROWNIE_NETWORK_ID")
    previous_provider = os.environ.get("WEB3_PROVIDER_URI")
    previous_etherscan = os.environ.get("ETHERSCAN_TOKEN")
    previous_typedenvs = os.environ.get("TYPEDENVS_SHUTUP")
    os.environ.setdefault("BROWNIE_NETWORK_ID", _brownie_network_id(chain_id))
    chain = next(c.key for c in CHAINS.values() if c.chain_id == int(chain_id))
    os.environ.setdefault("WEB3_PROVIDER_URI", get_rpc_url(chain))
    os.environ.setdefault("TYPEDENVS_SHUTUP", "1")
    if previous_etherscan is None and os.environ.get("ETHERSCAN_API_KEY"):
        os.environ["ETHERSCAN_TOKEN"] = os.environ["ETHERSCAN_API_KEY"]
    try:
        from y import get_price  # type: ignore

        price = get_price(token_address, int(block_number), fail_to_None=True, sync=True)
        if price is None:
            return None, "missing", {"block": int(block_number)}
        return float(price), "ok", {"block": int(block_number)}
    except ModuleNotFoundError as exc:
        return None, "unavailable", {"error": f"optional ypricemagic dependency not installed: {exc}"}
    except Exception as exc:
        return None, "error", {"error": str(exc), "block": int(block_number)}
    finally:
        if previous_network is None:
            os.environ.pop("BROWNIE_NETWORK_ID", None)
        else:
            os.environ["BROWNIE_NETWORK_ID"] = previous_network
        if previous_provider is None:
            os.environ.pop("WEB3_PROVIDER_URI", None)
        else:
            os.environ["WEB3_PROVIDER_URI"] = previous_provider
        if previous_etherscan is None:
            os.environ.pop("ETHERSCAN_TOKEN", None)
        else:
            os.environ["ETHERSCAN_TOKEN"] = previous_etherscan
        if previous_typedenvs is None:
            os.environ.pop("TYPEDENVS_SHUTUP", None)
        else:
            os.environ["TYPEDENVS_SHUTUP"] = previous_typedenvs


def report_amount(raw_value: str | int, decimals: int | None) -> Decimal:
    scale = Decimal(10) ** int(decimals or 18)
    return Decimal(int(raw_value)) / scale


def _fetch_price(source: str, chain_id: int, token_address: str, timestamp: int, block_number: int):
    if source == "defillama":
        return fetch_defillama_price(chain_id, token_address, timestamp)
    if source == "yprice":
        return fetch_yprice_price(chain_id, token_address, block_number)
    raise ValueError(f"unsupported price source {source!r}; expected {sorted(SUPPORTED_SOURCES)}")


def price_unpriced_reports(
    conn,
    limit: int | None = None,
    source: str = "defillama",
    fallback: str | None = "yprice",
) -> int:
    if source not in SUPPORTED_SOURCES:
        raise ValueError(f"unsupported price source {source!r}")
    if fallback is not None and fallback not in SUPPORTED_SOURCES:
        raise ValueError(f"unsupported fallback source {fallback!r}")
    sql = """
    SELECT
        r.chain_id,
        r.asset,
        r.block_timestamp,
        MIN(r.block_number) AS block_number
    FROM strategy_reports r
    LEFT JOIN prices p
      ON p.chain_id = r.chain_id
     AND lower(p.token_address) = lower(r.asset)
     AND p.timestamp = r.block_timestamp
     AND p.source = ?
    WHERE r.asset IS NOT NULL
      AND p.token_address IS NULL
    GROUP BY r.chain_id, r.asset, r.block_timestamp
    ORDER BY r.block_timestamp
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, (source,)).fetchall()
    count = 0
    if source == "defillama":
        count += _price_unpriced_reports_defillama_batched(conn, rows, fallback)
        return count

    for row in rows:
        sources = [source]
        if fallback and fallback != source:
            sources.append(fallback)
        for price_source in sources:
            price, status, payload = _fetch_price(
                price_source,
                int(row["chain_id"]),
                row["asset"],
                int(row["block_timestamp"]),
                int(row["block_number"]),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO prices (
                    chain_id, token_address, timestamp, block_number,
                    source, price_usd, status, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(row["chain_id"]),
                    row["asset"],
                    int(row["block_timestamp"]),
                    int(row["block_number"]),
                    price_source,
                    price,
                    status,
                    to_json(payload),
                ),
            )
            count += 1
            if status == "ok":
                break
        if count % 100 == 0:
            conn.commit()
            time.sleep(0.2)
    conn.commit()
    return count


def _chunks(rows, size: int):
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def _price_unpriced_reports_defillama_batched(conn, rows, fallback: str | None) -> int:
    count = 0
    blocked_fallback_tokens = {
        (int(row["chain_id"]), row["token_address"].lower())
        for row in conn.execute(
            """
            SELECT chain_id, token_address
            FROM prices
            WHERE source='yprice' AND status != 'ok'
            GROUP BY chain_id, lower(token_address)
            HAVING COUNT(*) >= 3
            """
        ).fetchall()
    }
    for batch in _chunks(rows, 100):
        requests_ = [
            (int(row["chain_id"]), row["asset"], int(row["block_timestamp"]))
            for row in batch
        ]
        try:
            results = fetch_defillama_prices_batch(requests_)
        except Exception:
            results = {}
            for chain_id, token_address, timestamp in requests_:
                price, status, payload = fetch_defillama_price(chain_id, token_address, timestamp)
                results[(chain_id, token_address, timestamp)] = (price, status, payload)
        for row in batch:
            chain_id = int(row["chain_id"])
            token_address = row["asset"]
            timestamp = int(row["block_timestamp"])
            block_number = int(row["block_number"])
            price, status, payload = results.get(
                (chain_id, token_address, timestamp),
                (None, "missing", {"timestamp": timestamp}),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO prices (
                    chain_id, token_address, timestamp, block_number,
                    source, price_usd, status, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chain_id,
                    token_address,
                    timestamp,
                    block_number,
                    "defillama",
                    price,
                    status,
                    to_json(payload),
                ),
            )
            count += 1
            if status != "ok" and fallback and fallback != "defillama":
                fallback_key = (chain_id, token_address.lower())
                if fallback_key in blocked_fallback_tokens:
                    fallback_price, fallback_status, fallback_payload = (
                        None,
                        "skipped",
                        {"reason": "prior yprice failures for token"},
                    )
                else:
                    fallback_price, fallback_status, fallback_payload = fetch_yprice_price(chain_id, token_address, block_number)
                    if fallback_status != "ok":
                        blocked_fallback_tokens.add(fallback_key)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO prices (
                        chain_id, token_address, timestamp, block_number,
                        source, price_usd, status, raw_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chain_id,
                        token_address,
                        timestamp,
                        block_number,
                        fallback,
                        fallback_price,
                        fallback_status,
                        to_json(fallback_payload),
                    ),
                )
                count += 1
            if count % 500 == 0:
                conn.commit()
        conn.commit()
        time.sleep(0.05)
    conn.commit()
    return count
