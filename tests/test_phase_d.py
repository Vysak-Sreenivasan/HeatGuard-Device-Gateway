"""Tests for Phase D: Fault injection engine, profiles, and admin API."""

import asyncio
import json
import time
import pytest
from httpx import ASGITransport, AsyncClient

from gateway.adapters.simulator import SimulatorAdapter
from gateway.config import settings
from gateway.faults import FaultManager
from gateway.idempotency import idempotency_store
from gateway.main import app
from gateway.tokens import create_approval_token
from gateway.tools.read import register_read_tools
from gateway.tools.write import register_write_tools
from mcp.server.mcpserver import MCPServer


@pytest.fixture(autouse=True)
async def clean_stores():
    await idempotency_store.clear()


@pytest.fixture
def sim_and_faults():
    sim = SimulatorAdapter("seed/devices.yaml")
    faults = FaultManager(sim)
    mcp = MCPServer("test_mcp")
    register_read_tools(mcp, sim, faults)
    register_write_tools(mcp, sim, faults)
    return sim, faults, mcp


# --- 1. Admin API Tests ---

@pytest.mark.asyncio
async def test_admin_faults_endpoint_profiles():
    headers = {"Authorization": f"Bearer {settings.admin_token}"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # Get active faults
        r = await client.get("/admin/faults", headers=headers)
        assert r.status_code == 200
        assert "profile" in r.json()

        # Switch to timeouts profile
        r_prof = await client.post(
            "/admin/faults", json={"profile": "timeouts"}, headers=headers
        )
        assert r_prof.status_code == 200
        assert r_prof.json()["profile"] == "timeouts"

        # Switch to chaos profile
        r_chaos = await client.post(
            "/admin/faults", json={"profile": "chaos"}, headers=headers
        )
        assert r_chaos.status_code == 200
        assert r_chaos.json()["profile"] == "chaos"

        # Reset endpoint
        r_reset = await client.post("/admin/faults/reset", headers=headers)
        assert r_reset.status_code == 200
        assert r_reset.json()["status"] == "reset_completed"


# --- 2. Specific Fault Behaviors from Section 9.2 ---

@pytest.mark.asyncio
async def test_fault_latency(sim_and_faults):
    sim, faults, mcp = sim_and_faults
    faults.update_faults({"latency": {"ms": 150}})

    t0 = time.time()
    await mcp.call_tool("get_available_devices", {})
    elapsed = time.time() - t0
    assert elapsed >= 0.14


@pytest.mark.asyncio
async def test_fault_error_rate(sim_and_faults):
    sim, faults, mcp = sim_and_faults
    faults.update_faults({"error_rate": 1.0})

    params = {"setpoint_c": 22.0}
    tok = create_approval_token("p1", "h1", "hp_living_1", "set_temperature", params)
    res = await mcp.call_tool(
        "execute_operations",
        {
            "device_id": "hp_living_1",
            "operation_id": "set_temperature",
            "instruction_id": "set_setpoint",
            "params": params,
            "idempotency_key": "k_err_1",
            "approval_token": tok,
        },
    )
    data = json.loads(res.content[0].text)
    assert data["status"] == "rejected"
    assert data["error_code"] == "INTERNAL"
    assert "error_rate" in data["error"]


@pytest.mark.asyncio
async def test_fault_timeout_on_applied_false(sim_and_faults):
    sim, faults, mcp = sim_and_faults
    faults.update_faults(
        {"timeout_on": {"device_ids": ["hp_living_1"], "applied": False, "timeout_ms": 50}}
    )

    params = {"setpoint_c": 26.0}
    tok = create_approval_token("p1", "h1", "hp_living_1", "set_temperature", params)
    res = await mcp.call_tool(
        "execute_operations",
        {
            "device_id": "hp_living_1",
            "operation_id": "set_temperature",
            "instruction_id": "set_setpoint",
            "params": params,
            "idempotency_key": "k_tout_false",
            "approval_token": tok,
        },
    )
    data = json.loads(res.content[0].text)
    assert data["status"] == "rejected"
    assert data["applied"] is False
    assert data["timeout"] is True
    # Verify state was NOT applied
    dev = sim.get_device("hp_living_1")
    assert dev.setpoint_c != 26.0


@pytest.mark.asyncio
async def test_fault_timeout_on_applied_true(sim_and_faults):
    sim, faults, mcp = sim_and_faults
    faults.update_faults(
        {"timeout_on": {"device_ids": ["hp_living_1"], "applied": True, "timeout_ms": 50}}
    )

    params = {"setpoint_c": 27.0}
    tok = create_approval_token("p1", "h1", "hp_living_1", "set_temperature", params)
    res = await mcp.call_tool(
        "execute_operations",
        {
            "device_id": "hp_living_1",
            "operation_id": "set_temperature",
            "instruction_id": "set_setpoint",
            "params": params,
            "idempotency_key": "k_tout_true",
            "approval_token": tok,
        },
    )
    data = json.loads(res.content[0].text)
    assert data["status"] == "rejected"
    assert data["applied"] is True
    assert data["timeout"] is True
    # Verify state WAS applied in background despite client timeout
    dev = sim.get_device("hp_living_1")
    assert dev.setpoint_c == 27.0


@pytest.mark.asyncio
async def test_fault_ignore_commands(sim_and_faults):
    sim, faults, mcp = sim_and_faults
    faults.update_faults({"ignore_commands": ["hp_living_1"]})

    dev = sim.get_device("hp_living_1")
    original_setpoint = dev.setpoint_c

    params = {"setpoint_c": 25.0}
    tok = create_approval_token("p1", "h1", "hp_living_1", "set_temperature", params)
    res = await mcp.call_tool(
        "execute_operations",
        {
            "device_id": "hp_living_1",
            "operation_id": "set_temperature",
            "instruction_id": "set_setpoint",
            "params": params,
            "idempotency_key": "k_ignore_1",
            "approval_token": tok,
        },
    )
    data = json.loads(res.content[0].text)
    assert data["status"] == "accepted"
    assert data["verified"] is False
    assert "ACCEPTED_UNVERIFIED" in data["note"]
    # Verify state was NOT updated
    assert dev.setpoint_c == original_setpoint


@pytest.mark.asyncio
async def test_fault_delayed_state(sim_and_faults):
    sim, faults, mcp = sim_and_faults
    faults.update_faults({"delayed_state": {"device_ids": ["hp_living_1"], "delay_s": 0.2}})

    dev = sim.get_device("hp_living_1")
    original_setpoint = dev.setpoint_c

    params = {"setpoint_c": 24.5}
    tok = create_approval_token("p1", "h1", "hp_living_1", "set_temperature", params)
    res = await mcp.call_tool(
        "execute_operations",
        {
            "device_id": "hp_living_1",
            "operation_id": "set_temperature",
            "instruction_id": "set_setpoint",
            "params": params,
            "idempotency_key": "k_delayed_1",
            "approval_token": tok,
        },
    )
    data = json.loads(res.content[0].text)
    assert data["status"] == "accepted"
    assert data["delayed_state"] is True
    # Immediately after, state is still old
    assert dev.setpoint_c == original_setpoint

    # Wait for delay to elapse
    await asyncio.sleep(0.3)
    assert dev.setpoint_c == 24.5


@pytest.mark.asyncio
async def test_fault_drift(sim_and_faults):
    sim, faults, mcp = sim_and_faults
    faults.update_faults(
        {"drift": {"device_id": "hp_living_1", "delta_temp_c": 4.0, "raise_alarm": True}}
    )

    dev = sim.get_device("hp_living_1")
    initial_temp = dev.current_temp_c

    params = {"setpoint_c": 21.0}
    tok = create_approval_token("p1", "h1", "hp_living_1", "set_temperature", params)
    await mcp.call_tool(
        "execute_operations",
        {
            "device_id": "hp_living_1",
            "operation_id": "set_temperature",
            "instruction_id": "set_setpoint",
            "params": params,
            "idempotency_key": "k_drift_1",
            "approval_token": tok,
        },
    )

    # Temperature drifted up
    assert dev.current_temp_c >= initial_temp + 3.0
    # Alarm raised
    alarms = await sim.get_alarms("hp_living_1")
    assert any("drift" in a.message.lower() for a in alarms)


@pytest.mark.asyncio
async def test_fault_flaky_reads(sim_and_faults):
    sim, faults, mcp = sim_and_faults
    faults.update_faults({"flaky_reads": 1.0})

    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool("get_telemetry_data", {"device_id": "hp_living_1"})
    cause = exc_info.value.__cause__
    assert cause is not None and "flaky_reads" in str(cause)


@pytest.mark.asyncio
async def test_fault_reset(sim_and_faults):
    sim, faults, mcp = sim_and_faults
    faults.update_faults({"error_rate": 1.0, "offline": ["hp_living_1"]})

    dev = sim.get_device("hp_living_1")
    assert dev.online is False

    faults.reset()
    assert faults.config.profile == "happy"
    assert faults.config.error_rate == 0.0
    dev_after = sim.get_device("hp_living_1")
    assert dev_after.online is True
