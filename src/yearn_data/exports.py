"""CSV exports for analysis outputs."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .storage import from_json

# Outputs that are append-only event detail rather than cumulative aggregates.
# Rotating these would double large files (reports.csv ~20MB) in git for no
# delta value, since old rows never change between runs.
NO_ROTATE_OUTPUTS = frozenset({"reports", "vault_flows", "strategy_debt_flows"})

INDEX_EVENTS_BY_ANALYSIS = {
    "lifetime-yield": ("StrategyReported",),
    # v2 strategy debt flows are derived from StrategyReported rows.
    "vault-volume": ("VaultFlow", "DebtUpdated", "StrategyReported"),
}


def latest_run_id(conn, name: str) -> int:
    row = conn.execute(
        "SELECT id FROM analysis_runs WHERE name=? AND status='complete' ORDER BY id DESC LIMIT 1",
        (name,),
    ).fetchone()
    if not row:
        raise ValueError(f"no completed analysis run for {name}")
    return int(row["id"])


def _run_meta(conn, name: str, run_id: int) -> dict[str, Any]:
    """Time/block anchor for an export so clients can rate-normalize deltas."""
    run = conn.execute(
        "SELECT id, name, started_at, completed_at FROM analysis_runs WHERE id=?",
        (run_id,),
    ).fetchone()
    event_names = INDEX_EVENTS_BY_ANALYSIS.get(name, ())
    placeholders = ",".join("?" for _ in event_names)
    last_block_rows = (
        conn.execute(
            f"""
            SELECT chain_id, event_name, MIN(last_block) AS last_block
            FROM index_state
            WHERE event_name IN ({placeholders})
            GROUP BY chain_id, event_name
            """,
            event_names,
        )
        if event_names
        else []
    )
    event_last_blocks: dict[int, list[int]] = {}
    for row in last_block_rows:
        event_last_blocks.setdefault(int(row["chain_id"]), []).append(int(row["last_block"]))
    last_blocks = {
        str(chain_id): min(chain_last_blocks)
        for chain_id, chain_last_blocks in event_last_blocks.items()
    }
    return {
        "run_id": run_id,
        "job": name,
        "started_at": run["started_at"] if run else None,
        "completed_at": run["completed_at"] if run else None,
        "last_blocks": last_blocks,
    }


def export_analysis(
    conn,
    name: str,
    out_dir: str | Path = "exports",
    run_id: int | None = None,
    rotate: bool = True,
) -> list[Path]:
    rid = run_id or latest_run_id(conn, name)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    meta = _run_meta(conn, name, rid)
    meta_text = json.dumps(meta, indent=2)
    written: list[Path] = []

    output_names = [
        row["name"]
        for row in conn.execute(
            "SELECT DISTINCT name FROM analysis_outputs WHERE run_id=? ORDER BY name",
            (rid,),
        )
    ]
    for output_name in output_names:
        first = conn.execute(
            """
            SELECT rowid, row_json
            FROM analysis_outputs
            WHERE run_id=? AND name=?
            ORDER BY rowid
            LIMIT 1
            """,
            (rid, output_name),
        ).fetchone()
        if not first:
            continue
        first_item = from_json(first["row_json"])
        path = out / f"{output_name}.csv"
        meta_path = out / f"{output_name}_meta.json"

        # Rotate the prior snapshot (csv + meta) aside so clients can diff
        # latest vs previous. Append-only detail files are not rotated.
        if rotate and output_name not in NO_ROTATE_OUTPUTS and path.exists():
            path.replace(out / f"{output_name}_previous.csv")
            if meta_path.exists():
                meta_path.replace(out / f"{output_name}_previous_meta.json")

        columns: list[str] = list(first_item)
        with path.open("w", newline="") as fp:
            writer = csv.DictWriter(fp, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerow(first_item)
            rows = conn.execute(
                """
                SELECT row_json
                FROM analysis_outputs
                WHERE run_id=? AND name=? AND rowid > ?
                ORDER BY rowid
                """,
                (rid, output_name, int(first["rowid"])),
            )
            for row in rows:
                writer.writerow(from_json(row["row_json"]))
        meta_path.write_text(meta_text)
        written.append(path)
    return written
