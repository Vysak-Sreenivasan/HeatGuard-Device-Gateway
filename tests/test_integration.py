"""Integration tests over Streamable HTTP transport with MCP ClientSession."""

import asyncio
import json
import httpx2
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from gateway.config import settings
from gateway.idempotency import idempotency_store
from gateway.main import app, fault_manager
from gateway.tokens import create_approval_token


@pytest.fixture(autouse=True)
async def clean_state():
    await idempotency_store.clear()
    fault_manager.reset()


@pytest.mark.asyncio
async def test_full_mcp_client_integration():
    """
    Test full end-to-end integration:
    MCP Client connects via Streamable HTTP, authenticates, exercises all 7 tools,
    and verifies rejection of write without approval token.
    """
    port = 8044
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.5)

    mcp_url = f"http://127.0.0.1:{port}/mcp"

    try:
        headers = {"Authorization": f"Bearer {settings.mcp_auth_token}"}
        timeout = httpx2.Timeout(10.0, read=None)
        async with httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client:
            async with streamable_http_client(mcp_url, http_client=http_client) as (
                read_stream,
                write_stream,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    init_res = await session.initialize()
                    assert init_res.server_info.name == "heatguard"

                    # 1. List tools
                    tools_list = await session.list_tools()
                    names = [t.name for t in tools_list.tools]
                    assert len(names) == 7
                    assert "get_available_devices" in names
                    assert "execute_operations" in names

                    # 2. Tool: get_available_devices
                    res_devs = await session.call_tool("get_available_devices", {})
                    devices = [json.loads(c.text) for c in res_devs.content]
                    assert len(devices) == 9

                    # 3. Tool: list_parameters
                    res_params = await session.call_tool(
                        "list_parameters", {"device_id": "hp_living_1"}
                    )
                    params = [json.loads(c.text) for c in res_params.content]
                    assert len(params) >= 4
                    param_ids = [p["id"] for p in params]
                    assert "current_temp_c" in param_ids

                    # 4. Tool: get_telemetry_data
                    res_telem = await session.call_tool(
                        "get_telemetry_data", {"device_id": "hp_living_1"}
                    )
                    telem = json.loads(res_telem.content[0].text)
                    assert "timestamp" in telem
                    assert "values" in telem
                    assert "state" in telem

                    # 5. Tool: get_device_alarms
                    res_alarms = await session.call_tool("get_device_alarms", {})
                    alarms = [json.loads(c.text) for c in res_alarms.content]
                    assert isinstance(alarms, list)

                    # 6. Tool: list_operations
                    res_ops = await session.call_tool(
                        "list_operations", {"device_id": "hp_living_1"}
                    )
                    ops = [json.loads(c.text) for c in res_ops.content]
                    assert len(ops) >= 3

                    # 7. Tool: get_sensor_history
                    res_hist = await session.call_tool(
                        "get_sensor_history",
                        {"device_id": "hp_living_1", "parameter": "current_temp_c", "days": 3},
                    )
                    hist = json.loads(res_hist.content[0].text)
                    assert hist["stats"]["count"] == 3 * 24

                    # 8. Tool: execute_operations WITHOUT token -> REJECTED
                    res_write_no_tok = await session.call_tool(
                        "execute_operations",
                        {
                            "device_id": "hp_living_1",
                            "operation_id": "set_power",
                            "instruction_id": "set_power_state",
                            "params": {"power": "on"},
                            "idempotency_key": "k_integ_no_tok",
                            "approval_token": "",
                        },
                    )
                    res_no_tok_data = json.loads(res_write_no_tok.content[0].text)
                    assert res_no_tok_data["status"] == "rejected"
                    assert res_no_tok_data["error_code"] == "INVALID_APPROVAL"

                    # 9. Tool: execute_operations WITH valid token -> ACCEPTED
                    valid_params = {"setpoint_c": 23.0}
                    valid_tok = create_approval_token(
                        "plan_integ", "sha256:hash", "hp_living_1", "set_temperature", valid_params
                    )
                    res_write_ok = await session.call_tool(
                        "execute_operations",
                        {
                            "device_id": "hp_living_1",
                            "operation_id": "set_temperature",
                            "instruction_id": "set_setpoint",
                            "params": valid_params,
                            "idempotency_key": "k_integ_ok",
                            "approval_token": valid_tok,
                        },
                    )
                    res_ok_data = json.loads(res_write_ok.content[0].text)
                    assert res_ok_data["status"] == "accepted"
                    assert res_ok_data["device_state_hint"]["setpoint_c"] == 23.0

                    # 10. Replay with same idempotency key -> DUPLICATE_REQUEST
                    res_replay = await session.call_tool(
                        "execute_operations",
                        {
                            "device_id": "hp_living_1",
                            "operation_id": "set_temperature",
                            "instruction_id": "set_setpoint",
                            "params": valid_params,
                            "idempotency_key": "k_integ_ok",
                            "approval_token": valid_tok,
                        },
                    )
                    res_replay_data = json.loads(res_replay.content[0].text)
                    assert res_replay_data["status"] == "accepted"
                    assert res_replay_data["duplicate"] is True
                    assert res_replay_data["duplicate_code"] == "DUPLICATE_REQUEST"
    finally:
        server.should_exit = True
        await server_task
