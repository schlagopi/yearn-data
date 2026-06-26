from decimal import Decimal

from yearn_data.analysis import run_lifetime_yield
from yearn_data.config import CHAINS
from yearn_data.storage import connect, from_json, init_db, seed_chains


def test_lifetime_yield_uses_net_gain_minus_loss(tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    init_db(conn)
    seed_chains(conn, CHAINS)
    conn.execute(
        """
        INSERT INTO vaults (
            chain_id, version, address, asset, asset_symbol, asset_decimals,
            updated_at
        )
        VALUES (1, 'v3', '0x0000000000000000000000000000000000000001',
                '0x0000000000000000000000000000000000000002', 'USDC', 6, 1)
        """
    )
    conn.execute(
        """
        INSERT INTO strategy_reports (
            chain_id, version, vault_address, strategy_address, tx_hash, log_index,
            block_number, block_timestamp, asset, asset_decimals, gain_raw, loss_raw,
            net_raw, extra_json
        )
        VALUES (1, 'v3', '0x0000000000000000000000000000000000000001',
                '0x0000000000000000000000000000000000000003',
                '0xabc', 0, 10, 100, '0x0000000000000000000000000000000000000002',
                6, '1500000', '250000', '1250000', '{}')
        """
    )
    conn.execute(
        """
        INSERT INTO prices (chain_id, token_address, timestamp, source, price_usd, status)
        VALUES (1, '0x0000000000000000000000000000000000000002', 100, 'defillama', 2.0, 'ok')
        """
    )
    conn.commit()

    run_id = run_lifetime_yield(conn)
    row = conn.execute(
        "SELECT row_json FROM analysis_outputs WHERE run_id=? AND name='total_yield_summary'",
        (run_id,),
    ).fetchone()
    output = from_json(row["row_json"])
    assert Decimal(output["gross_gain_usd"]) == Decimal("3.00")
    assert Decimal(output["loss_usd"]) == Decimal("0.500")
    assert Decimal(output["net_yield_usd"]) == Decimal("2.500")
    assert output["reports"] == 1
    assert output["priced_reports"] == 1


def test_lifetime_yield_excludes_known_incident_adjustments(tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    init_db(conn)
    seed_chains(conn, CHAINS)
    conn.execute(
        """
        INSERT INTO vaults (
            chain_id, version, address, asset, asset_symbol, asset_decimals,
            updated_at
        )
        VALUES (1, 'v2', '0x0000000000000000000000000000000000000001',
                '0x0000000000000000000000000000000000000002', 'DAI', 18, 1)
        """
    )
    reports = [
        (
            "0x5558d40c511524b015cd307a134b3358f52326cf39c2c6e61604d5726c64dfdd",
            "0",
            "100000000000000000000",
            "-100000000000000000000",
            100,
        ),
        (
            "0x66847f4dc80a6b4c32666972a9a68416d802d78b54619503fd0aec358fedb185",
            "200000000000000000000",
            "0",
            "200000000000000000000",
            101,
        ),
    ]
    for tx_hash, gain, loss, net, timestamp in reports:
        conn.execute(
            """
            INSERT INTO strategy_reports (
                chain_id, version, vault_address, strategy_address, tx_hash, log_index,
                block_number, block_timestamp, asset, asset_decimals, gain_raw, loss_raw,
                net_raw, extra_json
            )
            VALUES (1, 'v2', '0x0000000000000000000000000000000000000001',
                    '0x0000000000000000000000000000000000000003',
                    ?, 0, 10, ?, '0x0000000000000000000000000000000000000002',
                    18, ?, ?, ?, '{}')
            """,
            (tx_hash, timestamp, gain, loss, net),
        )
        conn.execute(
            """
            INSERT INTO prices (chain_id, token_address, timestamp, source, price_usd, status)
            VALUES (1, '0x0000000000000000000000000000000000000002', ?, 'defillama', 1.0, 'ok')
            """,
            (timestamp,),
        )
    conn.commit()

    run_id = run_lifetime_yield(conn)
    row = conn.execute(
        "SELECT row_json FROM analysis_outputs WHERE run_id=? AND name='total_yield_summary'",
        (run_id,),
    ).fetchone()
    output = from_json(row["row_json"])
    assert Decimal(output["raw_gross_gain_usd"]) == Decimal("200.0")
    assert Decimal(output["raw_loss_usd"]) == Decimal("100.0")
    assert Decimal(output["raw_net_yield_usd"]) == Decimal("100.0")
    assert Decimal(output["gross_gain_usd"]) == Decimal("0.0")
    assert Decimal(output["loss_usd"]) == Decimal("0.0")
    assert Decimal(output["net_yield_usd"]) == Decimal("0.0")
    assert output["adjusted_reports"] == 2

    report_rows = conn.execute(
        "SELECT row_json FROM analysis_outputs WHERE run_id=? AND name='reports'",
        (run_id,),
    ).fetchall()
    outputs = [from_json(row["row_json"]) for row in report_rows]
    assert {row["incident_id"] for row in outputs} == {"yearn-2021-05-14-dai-curve-paper-loss"}
    assert all(row["is_adjusted"] for row in outputs)
