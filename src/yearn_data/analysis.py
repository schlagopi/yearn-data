"""Analysis jobs built on top of normalized indexed data."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .incident_adjustments import adjustment_for_tx
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
        raw_gain_usd = _usd(row["gain_raw"], decimals, price)
        raw_loss_usd = _usd(row["loss_raw"], decimals, price)
        raw_net_usd = _usd(row["net_raw"], decimals, price)
        adjustment = adjustment_for_tx(row["tx_hash"])
        adjusted_gain_raw = adjustment.adjusted_gain_raw if adjustment else row["gain_raw"]
        adjusted_loss_raw = adjustment.adjusted_loss_raw if adjustment else row["loss_raw"]
        adjusted_net_raw = adjustment.adjusted_net_raw if adjustment else row["net_raw"]
        gain_usd = _usd(adjusted_gain_raw, decimals, price)
        loss_usd = _usd(adjusted_loss_raw, decimals, price)
        net_usd = _usd(adjusted_net_raw, decimals, price)
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
            "adjusted_gain_raw": adjusted_gain_raw,
            "adjusted_loss_raw": adjusted_loss_raw,
            "adjusted_net_raw": adjusted_net_raw,
            "price_usd": price,
            "price_status": row["price_status"] or "missing",
            "raw_gross_gain_usd": _decimal_or_none(raw_gain_usd),
            "raw_loss_usd": _decimal_or_none(raw_loss_usd),
            "raw_net_yield_usd": _decimal_or_none(raw_net_usd),
            "gross_gain_usd": _decimal_or_none(gain_usd),
            "loss_usd": _decimal_or_none(loss_usd),
            "net_yield_usd": _decimal_or_none(net_usd),
            "is_adjusted": bool(adjustment),
            "incident_id": adjustment.incident_id if adjustment else None,
            "incident_classification": adjustment.classification if adjustment else None,
            "incident_description": adjustment.description if adjustment else None,
            "incident_disclosure_url": adjustment.disclosure_url if adjustment else None,
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
                    "raw_gross_gain_usd": Decimal(0),
                    "raw_loss_usd": Decimal(0),
                    "raw_net_yield_usd": Decimal(0),
                    "adjusted_reports": 0,
                },
            )
            bucket["reports"] += 1
            if adjustment:
                bucket["adjusted_reports"] += 1
            if priced:
                bucket["priced_reports"] += 1
                bucket["raw_gross_gain_usd"] += raw_gain_usd or Decimal(0)
                bucket["raw_loss_usd"] += raw_loss_usd or Decimal(0)
                bucket["raw_net_yield_usd"] += raw_net_usd or Decimal(0)
                bucket["gross_gain_usd"] += gain_usd or Decimal(0)
                bucket["loss_usd"] += loss_usd or Decimal(0)
                bucket["net_yield_usd"] += net_usd or Decimal(0)
            else:
                bucket["unpriced_reports"] += 1

    for (output_name, _), row in totals.items():
        serialized = dict(row)
        for key in (
            "gross_gain_usd",
            "loss_usd",
            "net_yield_usd",
            "raw_gross_gain_usd",
            "raw_loss_usd",
            "raw_net_yield_usd",
        ):
            serialized[key] = _decimal_or_none(serialized[key])
        write_output(conn, run_id, output_name, serialized)
    conn.commit()
    complete_analysis_run(conn, run_id)
    return run_id


def _selected_price_join(table_alias: str) -> str:
    return f"""
        LEFT JOIN prices p
          ON p.chain_id = {table_alias}.chain_id
         AND p.token_address = {table_alias}.asset
         AND p.timestamp = {table_alias}.block_timestamp
         AND p.source = 'defillama'
         AND p.status = 'ok'
    """


def _month(timestamp: int) -> str:
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).strftime("%Y-%m")


def _empty_volume_bucket(dimension: str, key: str, row: Any) -> dict[str, Any]:
    return {
        "dimension": dimension,
        "key": key,
        "chain_id": row["chain_id"] if dimension != "all" else None,
        "version": row["version"] if dimension in {"version", "vault", "strategy"} else None,
        "vault_address": row["vault_address"] if dimension == "vault" else None,
        "strategy_address": row["strategy_address"] if dimension == "strategy" else None,
        "asset": row["asset"] if dimension == "token" else None,
        "asset_symbol": row["asset_symbol"] if dimension == "token" else None,
        "month": key if dimension == "month" else None,
        "events": 0,
        "priced_events": 0,
        "unpriced_events": 0,
        "deposit_usd": Decimal(0),
        "withdraw_usd": Decimal(0),
        "net_user_flow_usd": Decimal(0),
        "allocation_usd": Decimal(0),
        "deallocation_usd": Decimal(0),
        "net_strategy_flow_usd": Decimal(0),
        "gross_user_volume_usd": Decimal(0),
        "gross_strategy_volume_usd": Decimal(0),
        "gross_total_volume_usd": Decimal(0),
    }


def _volume_dimensions(row: Any) -> dict[str, tuple[str, str]]:
    dimensions = {
        "volume_summary": ("all", "all"),
        "volume_by_chain": ("chain", str(row["chain_id"])),
        "volume_by_vault": ("vault", f"{row['chain_id']}:{row['vault_address']}"),
        "volume_by_token": ("token", f"{row['chain_id']}:{row['asset']}"),
        "volume_by_month": ("month", _month(row["block_timestamp"])),
    }
    if row["strategy_address"]:
        dimensions["volume_by_strategy"] = ("strategy", f"{row['chain_id']}:{row['strategy_address']}")
    return dimensions


def _add_volume_to_totals(
    totals: dict[tuple[str, str], dict[str, Any]],
    row: Any,
    amount_usd: Decimal | None,
    event_type: str,
) -> None:
    for output_name, key in _volume_dimensions(row).items():
        bucket = totals.setdefault((output_name, key[1]), _empty_volume_bucket(key[0], key[1], row))
        bucket["events"] += 1
        if amount_usd is None:
            bucket["unpriced_events"] += 1
            continue
        bucket["priced_events"] += 1
        if event_type == "deposit":
            bucket["deposit_usd"] += amount_usd
            bucket["net_user_flow_usd"] += amount_usd
            bucket["gross_user_volume_usd"] += amount_usd
        elif event_type == "withdraw":
            bucket["withdraw_usd"] += amount_usd
            bucket["net_user_flow_usd"] -= amount_usd
            bucket["gross_user_volume_usd"] += amount_usd
        elif event_type == "allocation":
            bucket["allocation_usd"] += amount_usd
            bucket["net_strategy_flow_usd"] += amount_usd
            bucket["gross_strategy_volume_usd"] += amount_usd
        elif event_type == "deallocation":
            bucket["deallocation_usd"] += amount_usd
            bucket["net_strategy_flow_usd"] -= amount_usd
            bucket["gross_strategy_volume_usd"] += amount_usd
        bucket["gross_total_volume_usd"] = bucket["gross_user_volume_usd"] + bucket["gross_strategy_volume_usd"]


def run_vault_volume(conn) -> int:
    run_id = create_analysis_run(conn, "vault-volume")
    totals: dict[tuple[str, str], dict[str, Any]] = {}

    vault_rows = conn.execute(
        f"""
        SELECT
            vf.*,
            NULL AS strategy_address,
            v.asset_symbol,
            v.name AS vault_name,
            p.price_usd,
            p.status AS price_status
        FROM vault_flows vf
        LEFT JOIN vaults v
          ON v.chain_id = vf.chain_id AND v.address = vf.vault_address
        {_selected_price_join("vf")}
        ORDER BY vf.chain_id, vf.block_number, vf.log_index
        """
    ).fetchall()
    for row in vault_rows:
        amount_usd = _usd(row["assets_raw"], row["asset_decimals"], row["price_usd"])
        output = {
            "chain_id": row["chain_id"],
            "version": row["version"],
            "vault_address": row["vault_address"],
            "vault_name": row["vault_name"],
            "direction": row["direction"],
            "sender": row["sender"],
            "owner": row["owner"],
            "receiver": row["receiver"],
            "tx_hash": row["tx_hash"],
            "log_index": row["log_index"],
            "block_number": row["block_number"],
            "block_timestamp": row["block_timestamp"],
            "asset": row["asset"],
            "asset_symbol": row["asset_symbol"],
            "assets_raw": row["assets_raw"],
            "shares_raw": row["shares_raw"],
            "price_usd": row["price_usd"],
            "price_status": row["price_status"] or "missing",
            "amount_usd": _decimal_or_none(amount_usd),
        }
        write_output(conn, run_id, "vault_flows", output)
        _add_volume_to_totals(totals, row, amount_usd, row["direction"])

    debt_rows = conn.execute(
        f"""
        SELECT
            sdf.*,
            v.asset_symbol,
            v.name AS vault_name,
            p.price_usd,
            p.status AS price_status
        FROM strategy_debt_flows sdf
        LEFT JOIN vaults v
          ON v.chain_id = sdf.chain_id AND v.address = sdf.vault_address
        {_selected_price_join("sdf")}
        ORDER BY sdf.chain_id, sdf.block_number, sdf.log_index, sdf.direction
        """
    ).fetchall()
    for row in debt_rows:
        amount_usd = _usd(row["debt_delta_raw"], row["asset_decimals"], row["price_usd"])
        output = {
            "chain_id": row["chain_id"],
            "version": row["version"],
            "vault_address": row["vault_address"],
            "vault_name": row["vault_name"],
            "strategy_address": row["strategy_address"],
            "direction": row["direction"],
            "source_event": row["source_event"],
            "tx_hash": row["tx_hash"],
            "log_index": row["log_index"],
            "block_number": row["block_number"],
            "block_timestamp": row["block_timestamp"],
            "asset": row["asset"],
            "asset_symbol": row["asset_symbol"],
            "debt_delta_raw": row["debt_delta_raw"],
            "current_debt_raw": row["current_debt_raw"],
            "new_debt_raw": row["new_debt_raw"],
            "price_usd": row["price_usd"],
            "price_status": row["price_status"] or "missing",
            "amount_usd": _decimal_or_none(amount_usd),
        }
        write_output(conn, run_id, "strategy_debt_flows", output)
        _add_volume_to_totals(totals, row, amount_usd, row["direction"])

    for (output_name, _), row in totals.items():
        serialized = dict(row)
        for key in (
            "deposit_usd",
            "withdraw_usd",
            "net_user_flow_usd",
            "allocation_usd",
            "deallocation_usd",
            "net_strategy_flow_usd",
            "gross_user_volume_usd",
            "gross_strategy_volume_usd",
            "gross_total_volume_usd",
        ):
            serialized[key] = _decimal_or_none(serialized[key])
        write_output(conn, run_id, output_name, serialized)
    conn.commit()
    complete_analysis_run(conn, run_id)
    return run_id
