"""Tests for Phase A: Skeleton, seed fleet, adapter interface, and auth."""

import pytest
from httpx import ASGITransport, AsyncClient

from gateway.adapters.base import DeviceAdapter
from gateway.adapters.simulator import SimulatorAdapter
from gateway.config import settings
from gateway.main import app


@pytest.mark.asyncio
async def test_seed_fleet_structure():
    """Verify seed fleet meets all requirements from section 9.1 and 15."""
    adapter = SimulatorAdapter("seed/devices.yaml")
    devices = await adapter.list_devices()

    assert len(devices) == 9

    # Living room x3
    living_room = [d for d in devices if "living_room" in d.room]
    assert len(living_room) == 3

    # Bedroom x2
    bedrooms = [d for d in devices if "bedroom" in d.room]
    assert len(bedrooms) == 2

    # Kitchen x1
    kitchen = [d for d in devices if "kitchen" in d.room]
    assert len(kitchen) == 1

    # Office x2 (one offline)
    offices = [d for d in devices if "office" in d.room]
    assert len(offices) == 2
    offline_offices = [d for d in offices if not d.online]
    assert len(offline_offices) == 1
    assert offline_offices[0].id == "hp_office_2"

    # Pool pump x1 marked critical
    pool_pumps = [d for d in devices if d.critical]
    assert len(pool_pumps) == 1
    assert pool_pumps[0].id == "pump_pool_1"
    assert pool_pumps[0].critical_hint is True


@pytest.mark.asyncio
async def test_device_adapter_interface():
    """Verify simulator adapter conforms to DeviceAdapter protocol."""
    adapter = SimulatorAdapter("seed/devices.yaml")
    assert isinstance(adapter, DeviceAdapter)

    # Test basic queries
    devs = await adapter.list_devices()
    first_id = devs[0].id

    params = await adapter.list_parameters(first_id)
    assert len(params) >= 4
    param_ids = [p.id for p in params]
    assert "current_temp_c" in param_ids
    assert "setpoint_c" in param_ids
    assert "power" in param_ids

    telem = await adapter.get_telemetry(first_id)
    assert telem.timestamp is not None
    assert telem.state.power in ("on", "off")

    ops = await adapter.list_operations(first_id)
    assert len(ops) >= 3
    op_ids = [o.operation_id for o in ops]
    assert "set_power" in op_ids
    assert "set_temperature" in op_ids
    assert "set_mode" in op_ids

    hist = await adapter.get_history(first_id, "current_temp_c", 7)
    assert hist.window.filter == "7d"
    assert hist.stats["count"] == 7 * 24


@pytest.mark.asyncio
async def test_health_endpoint_open():
    """Verify /health is open with no authentication required."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["devices_count"] == 9
        assert data["adapter"] == "simulator"


@pytest.mark.asyncio
async def test_mcp_auth_rejection():
    """Verify /mcp rejects requests without valid Bearer token."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # No header
        resp_no_token = await client.post("/mcp", json={})
        assert resp_no_token.status_code == 401

        # Bad token
        resp_bad_token = await client.post(
            "/mcp",
            json={},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp_bad_token.status_code == 401


@pytest.mark.asyncio
async def test_admin_auth_rejection():
    """Verify /admin rejects requests without valid ADMIN_TOKEN."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get("/admin/anything")
        assert resp.status_code == 401

        resp_bad = await client.get(
            "/admin/anything",
            headers={"Authorization": "Bearer wrong-admin-token"},
        )
        assert resp_bad.status_code == 401
