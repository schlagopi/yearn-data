"""Event decoding helpers."""

from __future__ import annotations

from typing import Any

from eth_utils import event_abi_to_log_topic
from web3 import Web3
from web3._utils.events import get_event_data

from .abis import V2_STRATEGY_REPORTED_EVENTS, V3_STRATEGY_REPORTED_EVENT
from .chains import web3_for


def event_topic(event_abi: dict[str, Any]) -> str:
    return Web3.to_hex(event_abi_to_log_topic(event_abi))


def decode_event(chain: str, event_abis: list[dict[str, Any]], log: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    topic0 = Web3.to_hex(log["topics"][0])
    w3 = web3_for(chain)
    for event_abi in event_abis:
        if event_topic(event_abi) != topic0:
            continue
        decoded = get_event_data(w3.codec, event_abi, log)
        return event_abi, dict(decoded["args"])
    raise ValueError(f"unsupported event topic {topic0}")


def strategy_report_event_abis(version: str) -> list[dict[str, Any]]:
    if version == "v3":
        return [V3_STRATEGY_REPORTED_EVENT]
    if version == "v2":
        return V2_STRATEGY_REPORTED_EVENTS
    raise ValueError(f"unsupported vault version {version!r}")


def strategy_report_topics(version: str) -> list[str]:
    return [event_topic(abi) for abi in strategy_report_event_abis(version)]
