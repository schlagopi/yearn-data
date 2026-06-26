"""CSV exports for analysis outputs."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .storage import from_json


def latest_run_id(conn, name: str) -> int:
    row = conn.execute(
        "SELECT id FROM analysis_runs WHERE name=? AND status='complete' ORDER BY id DESC LIMIT 1",
        (name,),
    ).fetchone()
    if not row:
        raise ValueError(f"no completed analysis run for {name}")
    return int(row["id"])


def export_analysis(conn, name: str, out_dir: str | Path = "exports", run_id: int | None = None) -> list[Path]:
    rid = run_id or latest_run_id(conn, name)
    rows = conn.execute(
        "SELECT name, row_json FROM analysis_outputs WHERE run_id=? ORDER BY name",
        (rid,),
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["name"], []).append(from_json(row["row_json"]))

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for output_name, output_rows in grouped.items():
        if not output_rows:
            continue
        path = out / f"{output_name}.csv"
        columns: list[str] = []
        for item in output_rows:
            for key in item:
                if key not in columns:
                    columns.append(key)
        with path.open("w", newline="") as fp:
            writer = csv.DictWriter(fp, fieldnames=columns)
            writer.writeheader()
            writer.writerows(output_rows)
        written.append(path)
    return written
