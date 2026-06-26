"""Contract metadata and explorer helpers."""

from __future__ import annotations

import json
import time
from typing import Any

import requests
from web3 import Web3

from .abis import ERC20_ABI, VAULT_METADATA_ABI
from .chains import chain_config, web3_for
from .config import get_etherscan_api_key
from .multicall import aggregate3, decode_first, multicall_available
from .storage import to_json


ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"


def checksum(address: str) -> str:
    return Web3.to_checksum_address(address)


def fetch_explorer_abi(chain: str, address: str, timeout: int = 30) -> list[dict[str, Any]]:
    cfg = chain_config(chain)
    api_key = get_etherscan_api_key()
    if cfg.explorer_chain_id is None:
        raise ValueError(f"no Etherscan-compatible explorer configured for {cfg.key}")
    if not api_key:
        raise ValueError("missing ETHERSCAN_API_KEY")
    params = {
        "chainid": str(cfg.explorer_chain_id),
        "module": "contract",
        "action": "getabi",
        "address": checksum(address),
        "apikey": api_key,
    }
    response = requests.get(ETHERSCAN_V2_URL, params=params, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "1":
        raise RuntimeError(f"explorer ABI lookup failed for {address}: {payload.get('result')}")
    return json.loads(payload["result"])


def cache_contract_abi(conn, chain: str, address: str, kind: str, abi: list[dict[str, Any]]) -> None:
    cfg = chain_config(chain)
    conn.execute(
        """
        INSERT INTO contracts (chain_id, address, kind, abi_json, source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(chain_id, address, kind) DO UPDATE SET
            abi_json=excluded.abi_json,
            source=excluded.source,
            updated_at=excluded.updated_at
        """,
        (cfg.chain_id, checksum(address), kind, to_json(abi), "explorer", int(time.time())),
    )
    conn.commit()


def contract(chain: str, address: str, abi: list[dict[str, Any]]):
    return web3_for(chain).eth.contract(address=checksum(address), abi=abi)


def call_optional(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def vault_metadata(chain: str, address: str) -> dict[str, Any]:
    c = contract(chain, address, VAULT_METADATA_ABI)
    asset = call_optional(lambda: c.functions.asset().call())
    if asset is None:
        asset = call_optional(lambda: c.functions.token().call())
    symbol = call_optional(lambda: c.functions.symbol().call())
    name = call_optional(lambda: c.functions.name().call())
    api_version = call_optional(lambda: c.functions.apiVersion().call())
    decimals = None
    asset_symbol = None
    if asset:
        token = contract(chain, asset, ERC20_ABI)
        decimals = call_optional(lambda: token.functions.decimals().call())
        asset_symbol = call_optional(lambda: token.functions.symbol().call())
    return {
        "asset": checksum(asset) if asset else None,
        "asset_symbol": asset_symbol,
        "asset_decimals": int(decimals) if decimals is not None else None,
        "name": name,
        "api_version": api_version,
        "symbol": symbol,
    }


def vault_metadata_many(chain: str, addresses: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch vault and asset metadata with Multicall3 when available."""
    if not addresses:
        return {}
    if not multicall_available(chain):
        return {checksum(address): vault_metadata(chain, address) for address in addresses}

    normalized = [checksum(address) for address in addresses]
    signatures = ["asset()", "token()", "symbol()", "name()", "apiVersion()"]
    calls = [(address, sig) for address in normalized for sig in signatures]
    results = aggregate3(chain, calls)
    metadata: dict[str, dict[str, Any]] = {}
    i = 0
    for address in normalized:
        asset_result = results[i]
        token_result = results[i + 1]
        symbol_result = results[i + 2]
        name_result = results[i + 3]
        api_version_result = results[i + 4]
        i += len(signatures)
        asset = decode_first("address", asset_result) or decode_first("address", token_result)
        metadata[address] = {
            "asset": checksum(asset) if asset else None,
            "symbol": decode_first("string", symbol_result),
            "name": decode_first("string", name_result),
            "api_version": decode_first("string", api_version_result),
            "asset_symbol": None,
            "asset_decimals": None,
        }

    asset_addresses = sorted({item["asset"] for item in metadata.values() if item["asset"]})
    asset_calls = [(asset, "decimals()") for asset in asset_addresses] + [
        (asset, "symbol()") for asset in asset_addresses
    ]
    asset_results = aggregate3(chain, asset_calls) if asset_calls else []
    asset_meta: dict[str, dict[str, Any]] = {}
    n = len(asset_addresses)
    for idx, asset in enumerate(asset_addresses):
        decimals = decode_first("uint8", asset_results[idx]) if idx < len(asset_results) else None
        symbol = decode_first("string", asset_results[n + idx]) if n + idx < len(asset_results) else None
        asset_meta[asset] = {
            "asset_decimals": int(decimals) if decimals is not None else None,
            "asset_symbol": symbol,
        }

    for item in metadata.values():
        if item["asset"] in asset_meta:
            item.update(asset_meta[item["asset"]])
    return metadata
