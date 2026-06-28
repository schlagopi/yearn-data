import json

from yearn_data.exports import export_analysis
from yearn_data.storage import connect, init_db


def _complete_run(conn, name: str, output_name: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO analysis_runs (name, started_at, completed_at, status, params_json)
        VALUES (?, 10, 20, 'complete', '{}')
        """,
        (name,),
    )
    run_id = int(cur.lastrowid)
    conn.execute(
        """
        INSERT INTO analysis_outputs (run_id, name, row_json)
        VALUES (?, ?, '{"dimension":"all","key":"all"}')
        """,
        (run_id, output_name),
    )
    conn.commit()
    return run_id


def _index_state(conn, chain_id: int, target: str, event_name: str, last_block: int) -> None:
    conn.execute(
        """
        INSERT INTO index_state (chain_id, target, event_name, last_block, updated_at)
        VALUES (?, ?, ?, ?, 1)
        """,
        (chain_id, target, event_name, last_block),
    )


def test_lifetime_yield_meta_uses_strategy_report_blocks_only(tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    init_db(conn)
    run_id = _complete_run(conn, "lifetime-yield", "total_yield_summary")
    _index_state(conn, 1, "vault-a", "StrategyReported", 100)
    _index_state(conn, 1, "vault-b", "VaultFlow", 200)
    conn.commit()

    export_analysis(conn, "lifetime-yield", tmp_path / "exports", run_id=run_id)

    meta = json.loads((tmp_path / "exports" / "total_yield_summary_meta.json").read_text())
    assert meta["last_blocks"] == {"1": 100}


def test_vault_volume_meta_uses_slowest_relevant_event_stream(tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    init_db(conn)
    run_id = _complete_run(conn, "vault-volume", "volume_summary")
    _index_state(conn, 1, "vault-a", "VaultFlow", 200)
    _index_state(conn, 1, "vault-a", "DebtUpdated", 150)
    _index_state(conn, 1, "vault-a", "StrategyReported", 300)
    conn.commit()

    export_analysis(conn, "vault-volume", tmp_path / "exports", run_id=run_id)

    meta = json.loads((tmp_path / "exports" / "volume_summary_meta.json").read_text())
    assert meta["last_blocks"] == {"1": 150}
