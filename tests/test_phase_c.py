"""Tests for Phase C: Simulator realism, physics, history stats, alarms, critical safety."""

import pytest
from gateway.adapters.base import Alarm
from gateway.adapters.simulator import SimulatorAdapter
from gateway.tokens import create_approval_token
from gateway.tools.write import register_write_tools
from mcp.server.mcpserver import MCPServer
import json


@pytest.fixture
def sim():
    return SimulatorAdapter("seed/devices.yaml")


def test_first_order_physics_heating(sim):
    """Verify first-order temperature model moves toward setpoint when ON."""
    dev = sim.get_device("hp_living_1")
    dev.power = "on"
    dev.current_temp_c = 19.0
    dev.setpoint_c = 24.0

    # Step forward 15 minutes
    sim.step_physics(dt_minutes=15.0)
    assert dev.current_temp_c > 19.0
    assert dev.current_temp_c < 24.0

    # Step forward another 90 minutes
    sim.step_physics(dt_minutes=90.0)
    assert abs(dev.current_temp_c - 24.0) < 0.5


def test_first_order_physics_cooling_when_off(sim):
    """Verify temperature decays toward ambient (18.0°C) when power is OFF."""
    dev = sim.get_device("hp_living_1")
    dev.power = "off"
    dev.current_temp_c = 25.0

    # Step forward 30 minutes
    sim.step_physics(dt_minutes=30.0)
    assert dev.current_temp_c < 25.0
    assert dev.current_temp_c > 18.0

    # Step forward another 120 minutes
    sim.step_physics(dt_minutes=120.0)
    assert abs(dev.current_temp_c - 18.0) < 0.5


@pytest.mark.asyncio
async def test_generated_history_statistics(sim):
    """Verify generated history window and full summary statistics."""
    hist = await sim.get_history("hp_living_1", "current_temp_c", days=7)
    assert hist.window.filter == "7d"
    assert isinstance(hist.window.avg, float)

    stats = hist.stats
    assert stats["count"] == 7 * 24
    assert "mean" in stats
    assert "std" in stats
    assert "min" in stats
    assert "25%" in stats
    assert "50%" in stats
    assert "75%" in stats
    assert "max" in stats

    assert stats["min"] <= stats["25%"]
    assert stats["25%"] <= stats["50%"]
    assert stats["50%"] <= stats["75%"]
    assert stats["75%"] <= stats["max"]


@pytest.mark.asyncio
async def test_dynamic_and_injected_alarms(sim):
    """Verify alarms for offline devices, high temperatures, and manual alarms."""
    # 1. Offline device should automatically report offline alarm
    alarms_offline = await sim.get_alarms("hp_office_2")
    assert any(a.severity == "critical" and "offline" in a.message.lower() for a in alarms_offline)

    # 2. High temperature threshold alarm
    dev_living = sim.get_device("hp_living_1")
    dev_living.current_temp_c = 33.5
    sim.step_physics(dt_minutes=0.1)
    living_alarms = await sim.get_alarms("hp_living_1")
    assert any(a.severity == "critical" and "high temperature" in a.message.lower() for a in living_alarms)

    # 3. Manual alarm injection
    custom_alarm = Alarm(
        id="alm_custom_1",
        device_id="hp_living_1",
        severity="warning",
        message="Filter maintenance required",
        since="2026-10-06T12:00:00Z",
    )
    sim.add_alarm(custom_alarm)
    updated_alarms = await sim.get_alarms("hp_living_1")
    assert any(a.id == "alm_custom_1" for a in updated_alarms)


@pytest.mark.asyncio
async def test_critical_device_power_off_safeguard(sim):
    """Verify critical device rejects standard power-off, accepts force_power_off_critical."""
    mcp = MCPServer("test_mcp")
    register_write_tools(mcp, sim)

    async def call_exec(args: dict) -> dict:
        res = await mcp.call_tool("execute_operations", args)
        return json.loads(res.content[0].text)

    # 1. Attempt standard power-off on critical pool pump -> REJECTED
    tok_standard = create_approval_token(
        "p_crit_1", "h1", "pump_pool_1", "set_power", {"power": "off"}
    )
    res_rejected = await call_exec(
        {
            "device_id": "pump_pool_1",
            "operation_id": "set_power",
            "instruction_id": "set_power_state",
            "params": {"power": "off"},
            "idempotency_key": "k_crit_reject",
            "approval_token": tok_standard,
        }
    )
    assert res_rejected["status"] == "rejected"
    assert res_rejected["error_code"] == "LIMIT_EXCEEDED"
    assert "CRITICAL" in res_rejected["error"]

    # 2. Attempt power-off with force_power_off_critical instruction -> ACCEPTED
    force_params = {"power": "off", "override_reason": "Emergency maintenance"}
    tok_forced = create_approval_token(
        "p_crit_2", "h2", "pump_pool_1", "set_power", force_params
    )
    res_accepted = await call_exec(
        {
            "device_id": "pump_pool_1",
            "operation_id": "set_power",
            "instruction_id": "force_power_off_critical",
            "params": force_params,
            "idempotency_key": "k_crit_accept",
            "approval_token": tok_forced,
        }
    )
    assert res_accepted["status"] == "accepted"
    pool_dev = sim.get_device("pump_pool_1")
    assert pool_dev.power == "off"
