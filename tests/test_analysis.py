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
