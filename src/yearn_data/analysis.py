"""Analysis jobs built on top of normalized indexed data."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from .pricing import report_amount
from .storage import to_json


def _usd(raw_value: str, decimals: int | None, price: float | None) -> Decimal | None:
    if price is None:
        return None
    return report_amount(raw_value, decimals) * Decimal(str(price))


def create_analysis_run(conn, name: str, params: dict[str, Any] | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO analysis_runs (name, started_at, status, params_json) VALUES (?, ?, 'running', ?)",
        (name, int(time.time()), to_json(params or {})),
    )
    conn.commit()
    return int(cur.lastrowid)


def complete_analysis_run(conn, run_id: int, status: str = "complete") -> None:
    conn.execute(
        "UPDATE analysis_runs SET completed_at=?, status=? WHERE id=?",
        (int(time.time()), status, run_id),
    )
    conn.commit()


def write_output(conn, run_id: int, name: str, row: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO analysis_outputs (run_id, name, row_json) VALUES (?, ?, ?)",
        (run_id, name, to_json(row)),
    )


def _decimal_or_none(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def run_lifetime_yield(conn) -> int:
    run_id = create_analysis_run(conn, "lifetime-yield")
    rows = conn.execute(
        """
        SELECT
            r.*,
            v.asset_symbol,
            v.name AS vault_name,
            p.price_usd,
            p.status AS price_status
        FROM strategy_reports r
        LEFT JOIN vaults v
          ON v.chain_id = r.chain_id AND v.address = r.vault_address
        LEFT JOIN prices p
          ON p.chain_id = r.chain_id
         AND p.token_address = r.asset
         AND p.timestamp = r.block_timestamp
         AND p.source = (
            SELECT p2.source
            FROM prices p2
            WHERE p2.chain_id = r.chain_id
              AND p2.token_address = r.asset
              AND p2.timestamp = r.block_timestamp
              AND p2.status = 'ok'
            ORDER BY CASE p2.source WHEN 'defillama' THEN 0 WHEN 'yprice' THEN 1 ELSE 2 END
            LIMIT 1
         )
        """
    ).fetchall()

    totals: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        price = row["price_usd"]
        decimals = row["asset_decimals"]
        gain_usd = _usd(row["gain_raw"], decimals, price)
        loss_usd = _usd(row["loss_raw"], decimals, price)
        net_usd = _usd(row["net_raw"], decimals, price)
        priced = net_usd is not None
        report_row = {
            "chain_id": row["chain_id"],
            "version": row["version"],
            "vault_address": row["vault_address"],
            "vault_name": row["vault_name"],
            "strategy_address": row["strategy_address"],
            "tx_hash": row["tx_hash"],
            "log_index": row["log_index"],
            "block_number": row["block_number"],
            "block_timestamp": row["block_timestamp"],
            "asset": row["asset"],
            "asset_symbol": row["asset_symbol"],
            "gain_raw": row["gain_raw"],
            "loss_raw": row["loss_raw"],
            "net_raw": row["net_raw"],
            "price_usd": price,
            "price_status": row["price_status"] or "missing",
            "gross_gain_usd": _decimal_or_none(gain_usd),
            "loss_usd": _decimal_or_none(loss_usd),
            "net_yield_usd": _decimal_or_none(net_usd),
        }
        write_output(conn, run_id, "reports", report_row)

        dimensions = {
            "total_yield_summary": ("all", "all"),
            "yield_by_chain": ("chain", str(row["chain_id"])),
            "yield_by_vault": ("vault", f"{row['chain_id']}:{row['vault_address']}"),
            "yield_by_strategy": ("strategy", f"{row['chain_id']}:{row['strategy_address']}"),
        }
        for output_name, key in dimensions.items():
            bucket = totals.setdefault(
                (output_name, key[1]),
                {
                    "dimension": key[0],
                    "key": key[1],
                    "chain_id": row["chain_id"] if key[0] != "all" else None,
                    "vault_address": row["vault_address"] if key[0] == "vault" else None,
                    "strategy_address": row["strategy_address"] if key[0] == "strategy" else None,
                    "reports": 0,
                    "priced_reports": 0,
                    "unpriced_reports": 0,
                    "gross_gain_usd": Decimal(0),
                    "loss_usd": Decimal(0),
                    "net_yield_usd": Decimal(0),
                },
            )
            bucket["reports"] += 1
            if priced:
                bucket["priced_reports"] += 1
                bucket["gross_gain_usd"] += gain_usd or Decimal(0)
                bucket["loss_usd"] += loss_usd or Decimal(0)
                bucket["net_yield_usd"] += net_usd or Decimal(0)
            else:
                bucket["unpriced_reports"] += 1

    for (output_name, _), row in totals.items():
        serialized = dict(row)
        for key in ("gross_gain_usd", "loss_usd", "net_yield_usd"):
            serialized[key] = _decimal_or_none(serialized[key])
        write_output(conn, run_id, output_name, serialized)
    conn.commit()
    complete_analysis_run(conn, run_id)
    return run_id
