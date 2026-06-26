"""Chain and Web3 helpers."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from .config import CHAINS, ChainConfig, get_rpc_url, normalize_chain_key


RPC_TIMEOUT_SECONDS = 30


def chain_config(chain: str) -> ChainConfig:
    return CHAINS[normalize_chain_key(chain)]


@lru_cache(maxsize=16)
def web3_for(chain: str) -> Web3:
    cfg = chain_config(chain)
    w3 = Web3(Web3.HTTPProvider(get_rpc_url(cfg.key), request_kwargs={"timeout": RPC_TIMEOUT_SECONDS}))
    if cfg.key != "eth":
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


def latest_block(chain: str) -> int:
    return int(web3_for(chain).eth.block_number)


def block_timestamp(chain: str, block_number: int) -> int:
    block: dict[str, Any] = web3_for(chain).eth.get_block(block_number)
    return int(block["timestamp"])


def cached_block_timestamp(conn, chain: str, block_number: int) -> int:
    cfg = chain_config(chain)
    row = conn.execute(
        "SELECT timestamp FROM block_timestamps WHERE chain_id=? AND block_number=?",
        (cfg.chain_id, int(block_number)),
    ).fetchone()
    if row:
        return int(row["timestamp"])
    ts = block_timestamp(chain, block_number)
    conn.execute(
        """
        INSERT OR REPLACE INTO block_timestamps (chain_id, block_number, timestamp)
        VALUES (?, ?, ?)
        """,
        (cfg.chain_id, int(block_number), ts),
    )
    return ts


def find_contract_creation_block(chain: str, address: str, low: int = 0, high: int | None = None) -> int:
    """Find the first block where contract code exists using binary search."""
    w3 = web3_for(chain)
    target = Web3.to_checksum_address(address)
    if high is None:
        high = int(w3.eth.block_number)
    while True:
        try:
            if w3.eth.get_code(target, high):
                break
            raise ValueError(f"no contract code found for {target} at block {high}")
        except ValueError:
            raise
        except Exception as exc:
            if high <= low:
                raise
            if "block not found" not in str(exc).lower():
                raise
            high -= 1

    left, right = low, high
    while left < right:
        mid = (left + right) // 2
        if w3.eth.get_code(target, mid):
            right = mid
        else:
            left = mid + 1
    return left
