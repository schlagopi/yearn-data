from yearn_data.indexing import normalize_v3_debt_flow


def test_normalize_v3_debt_flow_classifies_allocation_delta():
    row = normalize_v3_debt_flow(
        1,
        "v3",
        "0x0000000000000000000000000000000000000001",
        "0x0000000000000000000000000000000000000002",
        18,
        {
            "transactionHash": bytes.fromhex("aa" * 32),
            "logIndex": 7,
            "blockNumber": 10,
        },
        100,
        {
            "strategy": "0x0000000000000000000000000000000000000003",
            "current_debt": 100,
            "new_debt": 150,
        },
    )

    assert row is not None
    assert row[4] == "allocation"
    assert row[11] == "50"
    assert row[12] == "100"
    assert row[13] == "150"


def test_normalize_v3_debt_flow_classifies_deallocation_delta():
    row = normalize_v3_debt_flow(
        1,
        "v3",
        "0x0000000000000000000000000000000000000001",
        "0x0000000000000000000000000000000000000002",
        18,
        {
            "transactionHash": bytes.fromhex("bb" * 32),
            "logIndex": 7,
            "blockNumber": 10,
        },
        100,
        {
            "strategy": "0x0000000000000000000000000000000000000003",
            "current_debt": 150,
            "new_debt": 100,
        },
    )

    assert row is not None
    assert row[4] == "deallocation"
    assert row[11] == "50"
