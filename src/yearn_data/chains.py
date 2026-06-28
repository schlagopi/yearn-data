"""Chain and Web3 helpers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
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


def cached_block_timestamps_many(conn, chain: str, block_numbers: list[int], max_workers: int = 32) -> dict[int, int]:
    unique = sorted({int(block_number) for block_number in block_numbers})
    if not unique:
        return {}
    cfg = chain_config(chain)
    found: dict[int, int] = {}
    for i in range(0, len(unique), 900):
        chunk = unique[i : i + 900]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT block_number, timestamp
            FROM block_timestamps
            WHERE chain_id=? AND block_number IN ({placeholders})
            """,
            [cfg.chain_id, *chunk],
        ).fetchall()
        for row in rows:
            found[int(row["block_number"])] = int(row["timestamp"])

    missing = [block_number for block_number in unique if block_number not in found]
    if not missing:
        return found

    fetched: dict[int, int] = {}
    workers = max(1, min(int(max_workers), len(missing)))
    executor = ThreadPoolExecutor(max_workers=workers)
    futures = {executor.submit(block_timestamp, chain, block_number): block_number for block_number in missing}
    try:
        for future in as_completed(futures):
            block_number = futures[future]
            fetched[block_number] = int(future.result())
    except BaseException:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)

    conn.executemany(
        """
        INSERT OR REPLACE INTO block_timestamps (chain_id, block_number, timestamp)
        VALUES (?, ?, ?)
        """,
        [(cfg.chain_id, block_number, timestamp) for block_number, timestamp in fetched.items()],
    )
    found.update(fetched)
    return found


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
