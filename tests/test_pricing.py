from yearn_data.config import CHAINS
from yearn_data.pricing import price_unpriced_reports, price_unpriced_volume
from yearn_data.storage import connect, init_db, seed_chains


def test_price_unpriced_reports_uses_defillama_primary_and_yprice_fallback(monkeypatch, tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    init_db(conn)
    seed_chains(conn, CHAINS)
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
                6, '1', '0', '1', '{}')
        """
    )
    conn.execute(
        """
        INSERT INTO prices (
            chain_id, token_address, timestamp, block_number, source, price_usd, status
        )
        VALUES (1, '0x0000000000000000000000000000000000000002', 100, 10, 'yprice', NULL, 'error')
        """
    )
    conn.commit()

    def fake_defillama_batch(requests_):
        return {
            (chain_id, token_address, timestamp): (None, "error", {"source": "defillama"})
            for chain_id, token_address, timestamp in requests_
        }

    def fake_yprice(chain_id, token_address, block_number):
        return 2.0, "ok", {"source": "yprice"}

    monkeypatch.setattr("yearn_data.pricing.fetch_defillama_prices_batch", fake_defillama_batch)
    monkeypatch.setattr("yearn_data.pricing.fetch_yprice_price", fake_yprice)
    count = price_unpriced_reports(conn, source="defillama", fallback="yprice")
    assert count == 2
    statuses = {
        row["source"]: row["status"]
        for row in conn.execute("SELECT source, status FROM prices").fetchall()
    }
    assert statuses == {"defillama": "error", "yprice": "ok"}


def test_price_unpriced_volume_uses_flow_tables(monkeypatch, tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    init_db(conn)
    seed_chains(conn, CHAINS)
    conn.execute(
        """
        INSERT INTO vault_flows (
            chain_id, version, vault_address, direction, tx_hash, log_index,
            block_number, block_timestamp, asset, asset_decimals, assets_raw,
            shares_raw, decoded_json
        )
        VALUES (1, 'v3', '0x0000000000000000000000000000000000000001',
                'deposit', '0xabc', 0, 10, 100,
                '0x0000000000000000000000000000000000000002',
                6, '1000000', '1000000', '{}')
        """
    )
    conn.commit()

    def fake_defillama_batch(requests_):
        return {
            (chain_id, token_address, timestamp): (3.0, "ok", {"source": "defillama"})
            for chain_id, token_address, timestamp in requests_
        }

    monkeypatch.setattr("yearn_data.pricing.fetch_defillama_prices_batch", fake_defillama_batch)
    count = price_unpriced_volume(conn, source="defillama", fallback=None)
    assert count == 1
    row = conn.execute("SELECT price_usd, status FROM prices").fetchone()
    assert row["price_usd"] == 3.0
    assert row["status"] == "ok"
