from yearn_data.events import event_topic, strategy_report_topics
from yearn_data.abis import V2_STRATEGY_REPORTED_EVENTS, V3_STRATEGY_REPORTED_EVENT


def test_strategy_report_topics_are_distinct_for_versions():
    v3_topics = strategy_report_topics("v3")
    v2_topics = strategy_report_topics("v2")
    assert v3_topics == [event_topic(V3_STRATEGY_REPORTED_EVENT)]
    assert len(v2_topics) == 2
    assert len(set(v2_topics)) == 2
    assert not set(v3_topics).intersection(v2_topics)


def test_v2_variants_cover_debt_paid_difference():
    old_fields = [item["name"] for item in V2_STRATEGY_REPORTED_EVENTS[0]["inputs"]]
    new_fields = [item["name"] for item in V2_STRATEGY_REPORTED_EVENTS[1]["inputs"]]
    assert "debtPaid" in old_fields
    assert "debtPaid" not in new_fields
