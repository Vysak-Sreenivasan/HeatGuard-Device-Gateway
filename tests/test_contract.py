"""Contract snapshot test and schema hash verification."""

import json
from pathlib import Path
import pytest

from gateway.contract import compute_contract_hash, export_tools_contract
from gateway.main import mcp_server

SNAPSHOT_PATH = Path("contract/tools_schema.json")


@pytest.mark.asyncio
async def test_tools_contract_snapshot_and_hash():
    """
    Contract test ensuring tool names, schemas, descriptions, and annotations stay stable.
    Outputs the SHA256 schema hash that the app pins.
    """
    assert SNAPSHOT_PATH.is_file(), "Contract snapshot file does not exist"

    active_contract = await export_tools_contract(mcp_server)
    active_hash = compute_contract_hash(active_contract)

    with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
        snapshot_contract = json.load(f)
    snapshot_hash = compute_contract_hash(snapshot_contract)

    # Print the schema hash for visibility
    print(f"\n[CONTRACT] Active Tool Schema SHA256 Hash: {active_hash}")

    # Verify exact match with pinned snapshot
    assert active_hash == snapshot_hash, (
        f"Tool contract schema hash mismatch!\nActive:   {active_hash}\nSnapshot: {snapshot_hash}\n"
        "If tool schemas were deliberately changed, update contract/tools_schema.json."
    )

    # Verify expected 7 tools are present
    tool_names = [t["name"] for t in active_contract]
    expected_tools = [
        "execute_operations",
        "get_available_devices",
        "get_device_alarms",
        "get_sensor_history",
        "get_telemetry_data",
        "list_operations",
        "list_parameters",
    ]
    assert tool_names == expected_tools

    # Verify execute_operations has write annotations
    exec_op = next(t for t in active_contract if t["name"] == "execute_operations")
    assert exec_op["annotations"]["readOnlyHint"] is False
    assert exec_op["annotations"]["idempotentHint"] is True

    # Verify read tools have read-only annotations
    for name in expected_tools[1:]:
        tool_meta = next(t for t in active_contract if t["name"] == name)
        assert tool_meta["annotations"]["readOnlyHint"] is True
