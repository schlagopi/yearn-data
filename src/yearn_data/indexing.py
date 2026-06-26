"""Generic event backfill and strategy report normalization."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from web3 import Web3

from .chains import cached_block_timestamp, latest_block, web3_for
from .config import CHAINS
from .events import (
    debt_updated_event_abis,
    debt_updated_topics,
    decode_event,
    strategy_report_event_abis,
    strategy_report_topics,
    vault_flow_event_abis,
    vault_flow_topics,
)
from .storage import to_json


ProgressCallback = Callable[[str], None]


def _hex(value: Any) -> str:
    if hasattr(value, "hex"):
        text = value.hex()
        return text if text.startswith("0x") else f"0x{text}"
    return str(value)


def _jsonable_args(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, bytes):
            out[key] = value.hex()
        else:
            out[key] = value
    return out


def get_index_state(conn, chain_id: int, target: str, event_name: str) -> int | None:
    row = conn.execute(
        "SELECT last_block FROM index_state WHERE chain_id=? AND target=? AND event_name=?",
        (chain_id, target, event_name),
    ).fetchone()
    return int(row["last_block"]) if row else None


def set_index_state(conn, chain_id: int, target: str, event_name: str, last_block: int) -> None:
    conn.execute(
        """
        INSERT INTO index_state (chain_id, target, event_name, last_block, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chain_id, target, event_name) DO UPDATE SET
            last_block=excluded.last_block,
            updated_at=excluded.updated_at
        """,
        (chain_id, target, event_name, int(last_block), int(time.time())),
    )
    conn.commit()


def normalize_strategy_report(
    chain_id: int,
    version: str,
    vault_address: str,
    asset: str | None,
    asset_decimals: int | None,
    log: dict[str, Any],
    block_ts: int,
    args: dict[str, Any],
) -> tuple[Any, ...]:
    gain = int(args.get("gain", 0))
    loss = int(args.get("loss", 0))
    current_debt = args.get("current_debt", args.get("totalDebt"))
    extra = {
        key: int(value) if isinstance(value, int) else value
        for key, value in args.items()
        if key
        not in {
            "strategy",
            "gain",
            "loss",
            "current_debt",
            "protocol_fees",
            "total_fees",
            "total_refunds",
        }
    }
    return (
        chain_id,
        version,
        Web3.to_checksum_address(vault_address),
        Web3.to_checksum_address(args["strategy"]),
        _hex(log["transactionHash"]),
        int(log["logIndex"]),
        int(log["blockNumber"]),
        int(block_ts),
        Web3.to_checksum_address(asset) if asset else None,
        asset_decimals,
        str(gain),
        str(loss),
        str(gain - loss),
        str(current_debt) if current_debt is not None else None,
        str(args.get("protocol_fees")) if args.get("protocol_fees") is not None else None,
        str(args.get("total_fees")) if args.get("total_fees") is not None else None,
        str(args.get("total_refunds")) if args.get("total_refunds") is not None else None,
        to_json(extra),
    )


def insert_raw_event(
    conn,
    chain_id: int,
    contract_address: str,
    event_name: str,
    log: dict[str, Any],
    block_ts: int,
    args: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO events_raw (
            chain_id, contract_address, event_name, tx_hash, log_index,
            block_number, block_timestamp, decoded_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chain_id,
            Web3.to_checksum_address(contract_address),
            event_name,
            _hex(log["transactionHash"]),
            int(log["logIndex"]),
            int(log["blockNumber"]),
            int(block_ts),
            json.dumps(_jsonable_args(args), sort_keys=True, default=str),
        ),
    )


def normalize_vault_flow(
    chain_id: int,
    version: str,
    vault_address: str,
    asset: str | None,
    asset_decimals: int | None,
    log: dict[str, Any],
    block_ts: int,
    event_name: str,
    args: dict[str, Any],
) -> tuple[Any, ...]:
    direction = "deposit" if event_name == "Deposit" else "withdraw"
    if version == "v2":
        sender = args.get("recipient")
        owner = args.get("recipient")
        receiver = args.get("recipient")
        assets = args.get("amount", 0)
    else:
        sender = args.get("sender")
        owner = args.get("owner")
        receiver = args.get("receiver") if direction == "withdraw" else args.get("owner")
        assets = args.get("assets", 0)
    return (
        chain_id,
        version,
        Web3.to_checksum_address(vault_address),
        direction,
        Web3.to_checksum_address(sender) if sender else None,
        Web3.to_checksum_address(owner) if owner else None,
        Web3.to_checksum_address(receiver) if receiver else None,
        _hex(log["transactionHash"]),
        int(log["logIndex"]),
        int(log["blockNumber"]),
        int(block_ts),
        Web3.to_checksum_address(asset) if asset else None,
        asset_decimals,
        str(int(assets)),
        str(int(args.get("shares", 0))),
        json.dumps(_jsonable_args(args), sort_keys=True, default=str),
    )


def insert_vault_flow(conn, row: tuple[Any, ...]) -> int:
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO vault_flows (
            chain_id, version, vault_address, direction, sender, owner, receiver,
            tx_hash, log_index, block_number, block_timestamp, asset,
            asset_decimals, assets_raw, shares_raw, decoded_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        row,
    )
    return int(cur.rowcount)


def normalize_v3_debt_flow(
    chain_id: int,
    version: str,
    vault_address: str,
    asset: str | None,
    asset_decimals: int | None,
    log: dict[str, Any],
    block_ts: int,
    args: dict[str, Any],
) -> tuple[Any, ...] | None:
    current_debt = int(args.get("current_debt", 0))
    new_debt = int(args.get("new_debt", 0))
    delta = new_debt - current_debt
    if delta == 0:
        return None
    direction = "allocation" if delta > 0 else "deallocation"
    return (
        chain_id,
        version,
        Web3.to_checksum_address(vault_address),
        Web3.to_checksum_address(args["strategy"]),
        direction,
        _hex(log["transactionHash"]),
        int(log["logIndex"]),
        int(log["blockNumber"]),
        int(block_ts),
        Web3.to_checksum_address(asset) if asset else None,
        asset_decimals,
        str(abs(delta)),
        str(current_debt),
        str(new_debt),
        "DebtUpdated",
        json.dumps(_jsonable_args(args), sort_keys=True, default=str),
    )


def insert_strategy_debt_flow(conn, row: tuple[Any, ...]) -> int:
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO strategy_debt_flows (
            chain_id, version, vault_address, strategy_address, direction,
            tx_hash, log_index, block_number, block_timestamp, asset,
            asset_decimals, debt_delta_raw, current_debt_raw, new_debt_raw,
            source_event, decoded_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        row,
    )
    return int(cur.rowcount)


def insert_strategy_report(conn, row: tuple[Any, ...]) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO strategy_reports (
            chain_id, version, vault_address, strategy_address, tx_hash, log_index,
            block_number, block_timestamp, asset, asset_decimals, gain_raw, loss_raw,
            net_raw, current_debt_raw, protocol_fees_raw, total_fees_raw,
            total_refunds_raw, extra_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        row,
    )


def index_vault_reports(conn, vault, to_block: int | None = None, chunk_size: int = 50_000) -> int:
    cfg = next(c for c in CHAINS.values() if c.chain_id == int(vault["chain_id"]))
    chain = cfg.key
    w3 = web3_for(chain)
    target = Web3.to_checksum_address(vault["address"])
    start = vault["deployment_block"] or 0
    last_done = get_index_state(conn, cfg.chain_id, target, "StrategyReported")
    from_block = max(start, (last_done + 1) if last_done is not None else start)
    final_block = int(to_block) if to_block is not None else latest_block(chain)
    if from_block > final_block:
        return 0

    version = vault["version"]
    event_abis = strategy_report_event_abis(version)
    topics = strategy_report_topics(version)
    inserted = 0
    current = from_block
    while current <= final_block:
        end = min(current + chunk_size - 1, final_block)
        logs = w3.eth.get_logs(
            {
                "fromBlock": current,
                "toBlock": end,
                "address": target,
                "topics": [topics],
            }
        )
        ts_cache: dict[int, int] = {}
        for log in logs:
            event_abi, args = decode_event(chain, event_abis, log)
            _ = event_abi
            block_number = int(log["blockNumber"])
            if block_number not in ts_cache:
                ts_cache[block_number] = cached_block_timestamp(conn, chain, block_number)
            block_ts = ts_cache[block_number]
            insert_raw_event(conn, cfg.chain_id, target, "StrategyReported", log, block_ts, args)
            row = normalize_strategy_report(
                cfg.chain_id,
                version,
                target,
                vault["asset"],
                vault["asset_decimals"],
                log,
                block_ts,
                args,
            )
            insert_strategy_report(conn, row)
            inserted += 1
        set_index_state(conn, cfg.chain_id, target, "StrategyReported", end)
        current = end + 1
    conn.commit()
    return inserted


def _get_logs_with_split(w3, params: dict[str, Any], min_chunk_size: int = 1_000) -> list[dict[str, Any]]:
    try:
        return list(w3.eth.get_logs(params))
    except Exception:
        start = int(params["fromBlock"])
        end = int(params["toBlock"])
        if end - start + 1 <= min_chunk_size:
            raise
        mid = (start + end) // 2
        left = dict(params, toBlock=mid)
        right = dict(params, fromBlock=mid + 1)
        return _get_logs_with_split(w3, left, min_chunk_size) + _get_logs_with_split(w3, right, min_chunk_size)


def _set_index_state_many(conn, chain_id: int, targets: list[str], event_name: str, last_block: int) -> None:
    now = int(time.time())
    conn.executemany(
        """
        INSERT INTO index_state (chain_id, target, event_name, last_block, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chain_id, target, event_name) DO UPDATE SET
            last_block=excluded.last_block,
            updated_at=excluded.updated_at
        """,
        [(chain_id, target, event_name, int(last_block), now) for target in targets],
    )
    conn.commit()


def _index_vault_batch(
    conn,
    chain: str,
    version: str,
    vaults: list[Any],
    to_block: int | None,
    chunk_size: int,
    progress: ProgressCallback | None = None,
) -> int:
    cfg = CHAINS[chain]
    w3 = web3_for(chain)
    final_block = int(to_block) if to_block is not None else latest_block(chain)
    event_abis = strategy_report_event_abis(version)
    topics = strategy_report_topics(version)

    by_address: dict[str, Any] = {Web3.to_checksum_address(v["address"]): v for v in vaults}
    start_by_address: dict[str, int] = {}
    for address, vault in by_address.items():
        deployment_block = int(vault["deployment_block"] or 0)
        last_done = get_index_state(conn, cfg.chain_id, address, "StrategyReported")
        start_by_address[address] = max(deployment_block, (last_done + 1) if last_done is not None else deployment_block)

    pending_starts = [start for start in start_by_address.values() if start <= final_block]
    if not pending_starts:
        return 0

    inserted = 0
    current = min(pending_starts)
    addresses = list(by_address)
    while current <= final_block:
        end = min(current + chunk_size - 1, final_block)
        active = [address for address in addresses if start_by_address[address] <= end]
        if not active:
            current = end + 1
            continue
        if progress:
            progress(f"{chain} {version}: scanning {current}-{end} across {len(active)} vaults")
        logs = _get_logs_with_split(
            w3,
            {
                "fromBlock": current,
                "toBlock": end,
                "address": active,
                "topics": [topics],
            },
        )
        ts_cache: dict[int, int] = {}
        for log in logs:
            target = Web3.to_checksum_address(log["address"])
            if int(log["blockNumber"]) < start_by_address[target]:
                continue
            event_abi, args = decode_event(chain, event_abis, log)
            _ = event_abi
            block_number = int(log["blockNumber"])
            if block_number not in ts_cache:
                ts_cache[block_number] = cached_block_timestamp(conn, chain, block_number)
            vault = by_address[target]
            block_ts = ts_cache[block_number]
            insert_raw_event(conn, cfg.chain_id, target, "StrategyReported", log, block_ts, args)
            row = normalize_strategy_report(
                cfg.chain_id,
                version,
                target,
                vault["asset"],
                vault["asset_decimals"],
                log,
                block_ts,
                args,
            )
            insert_strategy_report(conn, row)
            inserted += 1
        _set_index_state_many(conn, cfg.chain_id, active, "StrategyReported", end)
        current = end + 1
    conn.commit()
    return inserted


def _chunks(rows: list[Any], size: int) -> list[list[Any]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def _index_vault_flow_batch(
    conn,
    chain: str,
    version: str,
    vaults: list[Any],
    to_block: int | None,
    chunk_size: int,
    progress: ProgressCallback | None = None,
) -> int:
    cfg = CHAINS[chain]
    w3 = web3_for(chain)
    final_block = int(to_block) if to_block is not None else latest_block(chain)
    event_abis = vault_flow_event_abis(version)
    topics = vault_flow_topics(version)

    by_address: dict[str, Any] = {Web3.to_checksum_address(v["address"]): v for v in vaults}
    start_by_address: dict[str, int] = {}
    for address, vault in by_address.items():
        deployment_block = int(vault["deployment_block"] or 0)
        last_done = get_index_state(conn, cfg.chain_id, address, "VaultFlow")
        start_by_address[address] = max(deployment_block, (last_done + 1) if last_done is not None else deployment_block)

    pending_starts = [start for start in start_by_address.values() if start <= final_block]
    if not pending_starts:
        return 0

    inserted = 0
    current = min(pending_starts)
    addresses = list(by_address)
    while current <= final_block:
        end = min(current + chunk_size - 1, final_block)
        active = [address for address in addresses if start_by_address[address] <= end]
        if not active:
            current = end + 1
            continue
        if progress:
            progress(f"{chain} {version}: scanning vault flows {current}-{end} across {len(active)} vaults")
        logs = _get_logs_with_split(
            w3,
            {
                "fromBlock": current,
                "toBlock": end,
                "address": active,
                "topics": [topics],
            },
        )
        ts_cache: dict[int, int] = {}
        for log in logs:
            target = Web3.to_checksum_address(log["address"])
            if int(log["blockNumber"]) < start_by_address[target]:
                continue
            event_abi, args = decode_event(chain, event_abis, log)
            block_number = int(log["blockNumber"])
            if block_number not in ts_cache:
                ts_cache[block_number] = cached_block_timestamp(conn, chain, block_number)
            vault = by_address[target]
            block_ts = ts_cache[block_number]
            insert_raw_event(conn, cfg.chain_id, target, event_abi["name"], log, block_ts, args)
            row = normalize_vault_flow(
                cfg.chain_id,
                version,
                target,
                vault["asset"],
                vault["asset_decimals"],
                log,
                block_ts,
                event_abi["name"],
                args,
            )
            inserted += insert_vault_flow(conn, row)
        _set_index_state_many(conn, cfg.chain_id, active, "VaultFlow", end)
        current = end + 1
    conn.commit()
    return inserted


def _index_v3_debt_flow_batch(
    conn,
    chain: str,
    vaults: list[Any],
    to_block: int | None,
    chunk_size: int,
    progress: ProgressCallback | None = None,
) -> int:
    cfg = CHAINS[chain]
    w3 = web3_for(chain)
    final_block = int(to_block) if to_block is not None else latest_block(chain)
    event_abis = debt_updated_event_abis("v3")
    topics = debt_updated_topics("v3")

    by_address: dict[str, Any] = {Web3.to_checksum_address(v["address"]): v for v in vaults}
    start_by_address: dict[str, int] = {}
    for address, vault in by_address.items():
        deployment_block = int(vault["deployment_block"] or 0)
        last_done = get_index_state(conn, cfg.chain_id, address, "DebtUpdated")
        start_by_address[address] = max(deployment_block, (last_done + 1) if last_done is not None else deployment_block)

    pending_starts = [start for start in start_by_address.values() if start <= final_block]
    if not pending_starts:
        return 0

    inserted = 0
    current = min(pending_starts)
    addresses = list(by_address)
    while current <= final_block:
        end = min(current + chunk_size - 1, final_block)
        active = [address for address in addresses if start_by_address[address] <= end]
        if not active:
            current = end + 1
            continue
        if progress:
            progress(f"{chain} v3: scanning debt updates {current}-{end} across {len(active)} vaults")
        logs = _get_logs_with_split(
            w3,
            {
                "fromBlock": current,
                "toBlock": end,
                "address": active,
                "topics": [topics],
            },
        )
        ts_cache: dict[int, int] = {}
        for log in logs:
            target = Web3.to_checksum_address(log["address"])
            if int(log["blockNumber"]) < start_by_address[target]:
                continue
            event_abi, args = decode_event(chain, event_abis, log)
            block_number = int(log["blockNumber"])
            if block_number not in ts_cache:
                ts_cache[block_number] = cached_block_timestamp(conn, chain, block_number)
            vault = by_address[target]
            block_ts = ts_cache[block_number]
            insert_raw_event(conn, cfg.chain_id, target, event_abi["name"], log, block_ts, args)
            row = normalize_v3_debt_flow(
                cfg.chain_id,
                "v3",
                target,
                vault["asset"],
                vault["asset_decimals"],
                log,
                block_ts,
                args,
            )
            if row is not None:
                inserted += insert_strategy_debt_flow(conn, row)
        _set_index_state_many(conn, cfg.chain_id, active, "DebtUpdated", end)
        current = end + 1
    conn.commit()
    return inserted


def index_v2_debt_flows_from_reports(conn, chains: list[str] | None = None) -> int:
    params: list[Any] = []
    where = "WHERE r.version='v2'"
    if chains:
        chain_ids = [CHAINS[c].chain_id for c in chains]
        where += f" AND r.chain_id IN ({','.join('?' for _ in chain_ids)})"
        params.extend(chain_ids)
    rows = conn.execute(
        f"""
        SELECT r.*, v.asset_symbol
        FROM strategy_reports r
        LEFT JOIN vaults v
          ON v.chain_id = r.chain_id AND v.address = r.vault_address
        {where}
        """,
        params,
    ).fetchall()
    inserted = 0
    for row in rows:
        extra = json.loads(row["extra_json"] or "{}")
        values = [
            ("allocation", int(extra.get("debtAdded") or 0)),
            ("deallocation", int(extra.get("debtPaid") or 0)),
        ]
        for direction, amount in values:
            if amount <= 0:
                continue
            inserted += insert_strategy_debt_flow(
                conn,
                (
                    int(row["chain_id"]),
                    row["version"],
                    Web3.to_checksum_address(row["vault_address"]),
                    Web3.to_checksum_address(row["strategy_address"]),
                    direction,
                    row["tx_hash"],
                    int(row["log_index"]),
                    int(row["block_number"]),
                    int(row["block_timestamp"]),
                    Web3.to_checksum_address(row["asset"]) if row["asset"] else None,
                    row["asset_decimals"],
                    str(amount),
                    row["current_debt_raw"],
                    row["current_debt_raw"],
                    "StrategyReported",
                    row["extra_json"],
                ),
            )
    conn.commit()
    return inserted


def index_all_volume(
    conn,
    chains: list[str] | None = None,
    to_block: int | None = None,
    chunk_size: int = 50_000,
    address_batch_size: int = 100,
    progress: ProgressCallback | None = None,
) -> int:
    params: list[Any] = []
    where = ""
    if chains:
        chain_ids = [CHAINS[c].chain_id for c in chains]
        where = f"WHERE chain_id IN ({','.join('?' for _ in chain_ids)})"
        params.extend(chain_ids)
    rows = conn.execute(f"SELECT * FROM vaults {where} ORDER BY chain_id, version, address", params).fetchall()
    total = 0
    by_group: dict[tuple[str, str], list[Any]] = {}
    chain_by_id = {cfg.chain_id: cfg.key for cfg in CHAINS.values()}
    for vault in rows:
        by_group.setdefault((chain_by_id[int(vault["chain_id"])], vault["version"]), []).append(vault)
    for (chain, version), vaults in sorted(by_group.items()):
        for batch in _chunks(vaults, address_batch_size):
            total += _index_vault_flow_batch(
                conn,
                chain,
                version,
                batch,
                to_block=to_block,
                chunk_size=chunk_size,
                progress=progress,
            )
            if version == "v3":
                total += _index_v3_debt_flow_batch(
                    conn,
                    chain,
                    batch,
                    to_block=to_block,
                    chunk_size=chunk_size,
                    progress=progress,
                )
    total += index_v2_debt_flows_from_reports(conn, chains=chains)
    return total


def index_all_reports(
    conn,
    chains: list[str] | None = None,
    to_block: int | None = None,
    chunk_size: int = 50_000,
    address_batch_size: int = 100,
    progress: ProgressCallback | None = None,
) -> int:
    params: list[Any] = []
    where = ""
    if chains:
        chain_ids = [CHAINS[c].chain_id for c in chains]
        where = f"WHERE chain_id IN ({','.join('?' for _ in chain_ids)})"
        params.extend(chain_ids)
    rows = conn.execute(f"SELECT * FROM vaults {where} ORDER BY chain_id, version, address", params).fetchall()
    total = 0
    by_group: dict[tuple[str, str], list[Any]] = {}
    chain_by_id = {cfg.chain_id: cfg.key for cfg in CHAINS.values()}
    for vault in rows:
        by_group.setdefault((chain_by_id[int(vault["chain_id"])], vault["version"]), []).append(vault)
    for (chain, version), vaults in sorted(by_group.items()):
        for batch in _chunks(vaults, address_batch_size):
            total += _index_vault_batch(
                conn,
                chain,
                version,
                batch,
                to_block=to_block,
                chunk_size=chunk_size,
                progress=progress,
            )
    return total
