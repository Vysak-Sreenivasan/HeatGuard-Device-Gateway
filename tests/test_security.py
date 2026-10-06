"""Security test suite: Auth, token forgery, parameter tampering, and secret isolation."""

import json
import time
import pytest
from httpx import ASGITransport, AsyncClient

from gateway.adapters.simulator import SimulatorAdapter
from gateway.audit import audit_log
from gateway.config import settings
from gateway.idempotency import idempotency_store
from gateway.main import app
from gateway.tokens import create_approval_token
from gateway.tools.write import register_write_tools
from mcp.server.mcpserver import MCPServer


@pytest.fixture(autouse=True)
async def clean_stores():
    await idempotency_store.clear()
    await audit_log.clear()


@pytest.mark.asyncio
async def test_security_transport_auth_boundaries():
    """Verify endpoint authorization boundaries for open vs protected routes."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # /health is open
        res_health = await client.get("/health")
        assert res_health.status_code == 200

        # /mcp requires MCP_AUTH_TOKEN
        res_mcp_none = await client.post("/mcp", json={})
        assert res_mcp_none.status_code == 401

        res_mcp_bad = await client.post(
            "/mcp", json={}, headers={"Authorization": "Bearer bad-token"}
        )
        assert res_mcp_bad.status_code == 401

        # /admin requires ADMIN_TOKEN
        res_admin_none = await client.get("/admin/audit")
        assert res_admin_none.status_code == 401

        res_admin_bad = await client.get(
            "/admin/audit", headers={"Authorization": "Bearer wrong-admin"}
        )
        assert res_admin_bad.status_code == 401

        # Using MCP token on admin route is rejected
        res_admin_mcp_tok = await client.get(
            "/admin/audit", headers={"Authorization": f"Bearer {settings.mcp_auth_token}"}
        )
        assert res_admin_mcp_tok.status_code == 401


@pytest.mark.asyncio
async def test_security_approval_token_tampering():
    """Verify that tampering with any part of a signed approval token is rejected."""
    sim = SimulatorAdapter("seed/devices.yaml")
    mcp = MCPServer("test_mcp")
    register_write_tools(mcp, sim)

    async def call_exec(args: dict) -> dict:
        res = await mcp.call_tool("execute_operations", args)
        return json.loads(res.content[0].text)

    legit_params = {"setpoint_c": 21.0}
    token = create_approval_token("plan_sec", "sha256:orig", "hp_living_1", "set_temperature", legit_params)

    # 1. Parameter tampering (attacker changes setpoint_c from 21.0 to 28.0)
    res_tampered_param = await call_exec(
        {
            "device_id": "hp_living_1",
            "operation_id": "set_temperature",
            "instruction_id": "set_setpoint",
            "params": {"setpoint_c": 28.0},
            "idempotency_key": "k_tamper_1",
            "approval_token": token,
        }
    )
    assert res_tampered_param["status"] == "rejected"
    assert res_tampered_param["error_code"] == "INVALID_APPROVAL"

    # 2. Target device redirection (attacker replays token intended for hp_living_1 against hp_bed_1)
    res_tampered_dev = await call_exec(
        {
            "device_id": "hp_bed_1",
            "operation_id": "set_temperature",
            "instruction_id": "set_setpoint",
            "params": legit_params,
            "idempotency_key": "k_tamper_2",
            "approval_token": token,
        }
    )
    assert res_tampered_dev["status"] == "rejected"
    assert res_tampered_dev["error_code"] == "INVALID_APPROVAL"

    # 3. Operation substitution (attacker replays token intended for set_temperature on set_power)
    res_tampered_op = await call_exec(
        {
            "device_id": "hp_living_1",
            "operation_id": "set_power",
            "instruction_id": "set_power_state",
            "params": {"power": "off"},
            "idempotency_key": "k_tamper_3",
            "approval_token": token,
        }
    )
    assert res_tampered_op["status"] == "rejected"
    assert res_tampered_op["error_code"] == "INVALID_APPROVAL"


@pytest.mark.asyncio
async def test_security_expired_token_rejection():
    """Verify expired tokens are rejected with TOKEN_EXPIRED."""
    sim = SimulatorAdapter("seed/devices.yaml")
    mcp = MCPServer("test_mcp")
    register_write_tools(mcp, sim)

    params = {"power": "on"}
    past_time = int(time.time()) - 3600  # 1 hour ago
    expired_token = create_approval_token(
        "p_exp", "h_exp", "hp_living_1", "set_power", params, exp=past_time
    )

    res = await mcp.call_tool(
        "execute_operations",
        {
            "device_id": "hp_living_1",
            "operation_id": "set_power",
            "instruction_id": "set_power_state",
            "params": params,
            "idempotency_key": "k_sec_exp",
            "approval_token": expired_token,
        },
    )
    data = json.loads(res.content[0].text)
    assert data["status"] == "rejected"
    assert data["error_code"] == "TOKEN_EXPIRED"


@pytest.mark.asyncio
async def test_security_no_secret_leakage_in_logs():
    """Verify that audit logs and system logs never contain secrets or full tokens."""
    sim = SimulatorAdapter("seed/devices.yaml")
    mcp = MCPServer("test_mcp")
    register_write_tools(mcp, sim)

    sensitive_token = create_approval_token(
        "plan_secret_check", "sha256:sec", "hp_living_1", "set_power", {"power": "on"}
    )
    await mcp.call_tool(
        "execute_operations",
        {
            "device_id": "hp_living_1",
            "operation_id": "set_power",
            "instruction_id": "set_power_state",
            "params": {"power": "on"},
            "idempotency_key": "k_leak_check",
            "approval_token": sensitive_token,
        },
    )

    entries = await audit_log.get_entries(limit=10)
    assert len(entries) >= 1
    log_str = json.dumps(entries)

    # Neither the approval token nor the signing secret nor bearer tokens must appear in audit log
    assert sensitive_token not in log_str
    assert settings.approval_signing_secret not in log_str
    assert settings.mcp_auth_token not in log_str
    assert settings.admin_token not in log_str
