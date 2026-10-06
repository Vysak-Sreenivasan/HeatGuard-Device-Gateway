"""Tests for Phase B: Tools, tokens, idempotency, audit log, execute_operations."""

import time
import pytest
from httpx import ASGITransport, AsyncClient

from gateway.adapters.simulator import SimulatorAdapter
from gateway.audit import audit_log
from gateway.config import settings
from gateway.idempotency import idempotency_store
from gateway.main import app
from gateway.tokens import create_approval_token, verify_approval_token
from gateway.tools.write import register_write_tools
from mcp.server.mcpserver import MCPServer


@pytest.fixture(autouse=True)
async def clean_stores():
    """Clear idempotency and audit stores before each test."""
    await idempotency_store.clear()
    await audit_log.clear()


# --- 1. Token Verification Unit Tests ---

def test_token_verify_valid():
    params = {"power": "on"}
    token = create_approval_token(
        plan_id="plan_123",
        plan_hash="sha256:dummy",
        device_id="hp_living_1",
        operation_id="set_power",
        params=params,
    )
    res = verify_approval_token(
        token=token,
        expected_device_id="hp_living_1",
        expected_operation_id="set_power",
        expected_params=params,
    )
    assert res.is_valid is True
    assert res.plan_id == "plan_123"
    assert res.error_code is None


def test_token_verify_bad_signature():
    params = {"power": "on"}
    token = create_approval_token(
        plan_id="plan_123",
        plan_hash="sha256:dummy",
        device_id="hp_living_1",
        operation_id="set_power",
        params=params,
        secret="different-secret-key",
    )
    res = verify_approval_token(
        token=token,
        expected_device_id="hp_living_1",
        expected_operation_id="set_power",
        expected_params=params,
    )
    assert res.is_valid is False
    assert res.error_code == "INVALID_APPROVAL"


def test_token_verify_expired():
    params = {"power": "on"}
    past_exp = int(time.time()) - 100
    token = create_approval_token(
        plan_id="plan_123",
        plan_hash="sha256:dummy",
        device_id="hp_living_1",
        operation_id="set_power",
        params=params,
        exp=past_exp,
    )
    res = verify_approval_token(
        token=token,
        expected_device_id="hp_living_1",
        expected_operation_id="set_power",
        expected_params=params,
    )
    assert res.is_valid is False
    assert res.error_code == "TOKEN_EXPIRED"


def test_token_verify_mismatches():
    params = {"power": "on"}
    token = create_approval_token(
        plan_id="plan_123",
        plan_hash="sha256:dummy",
        device_id="hp_living_1",
        operation_id="set_power",
        params=params,
    )
    # Device mismatch
    res_dev = verify_approval_token(
        token=token,
        expected_device_id="hp_bed_1",
        expected_operation_id="set_power",
        expected_params=params,
    )
    assert res_dev.is_valid is False
    assert res_dev.error_code == "INVALID_APPROVAL"

    # Operation mismatch
    res_op = verify_approval_token(
        token=token,
        expected_device_id="hp_living_1",
        expected_operation_id="set_temperature",
        expected_params=params,
    )
    assert res_op.is_valid is False
    assert res_op.error_code == "INVALID_APPROVAL"

    # Params mismatch / tampering
    res_params = verify_approval_token(
        token=token,
        expected_device_id="hp_living_1",
        expected_operation_id="set_power",
        expected_params={"power": "off"},
    )
    assert res_params.is_valid is False
    assert res_params.error_code == "INVALID_APPROVAL"


# --- 2. Idempotency Store Tests ---

@pytest.mark.asyncio
async def test_idempotency_store_replay():
    key = "idem-key-001"
    res1 = {"status": "accepted", "state": {"power": "on"}}
    await idempotency_store.set(key, res1, ttl_seconds=60)

    stored = await idempotency_store.get(key)
    assert stored == res1

    # Non-existent key
    assert await idempotency_store.get("non-existent") is None


@pytest.mark.asyncio
async def test_idempotency_ttl_expiration():
    key = "idem-key-exp"
    await idempotency_store.set(key, {"status": "accepted"}, ttl_seconds=-10)
    assert await idempotency_store.get(key) is None


# --- 3. Audit Log Tests & Never-Log-Secrets Rule ---

@pytest.mark.asyncio
async def test_audit_log_records_and_sanitizes():
    secret_param = {"power": "on", "some_secret": "my-secret", "auth_token": "foo"}
    await audit_log.log(
        request_id="req_001",
        client_id="client_test",
        device_id="hp_living_1",
        operation_id="set_power",
        instruction_id="set_power_state",
        params=secret_param,
        plan_id="plan_777",
        status="accepted",
    )

    entries = await audit_log.get_entries(limit=10)
    assert len(entries) == 1
    e = entries[0]
    assert e["request_id"] == "req_001"
    assert e["plan_id"] == "plan_777"
    assert e["params"] == {"power": "on"}  # secret and auth_token stripped!
    # Ensure no token values or secrets leaked
    assert "my-secret" not in str(e)
    assert "foo" not in str(e)


@pytest.mark.asyncio
async def test_admin_audit_endpoint():
    await audit_log.log(
        request_id="req_adm_1",
        client_id="client_test",
        device_id="hp_living_1",
        operation_id="set_power",
        instruction_id="set_power_state",
        params={"power": "on"},
        plan_id="plan_999",
        status="accepted",
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # Without admin token -> 401
        r_unauth = await client.get("/admin/audit")
        assert r_unauth.status_code == 401

        # With admin token -> 200
        headers = {"Authorization": f"Bearer {settings.admin_token}"}
        r_auth = await client.get("/admin/audit", headers=headers)
        assert r_auth.status_code == 200
        data = r_auth.json()
        assert data["count"] >= 1
        assert data["audit_logs"][0]["request_id"] == "req_adm_1"


# --- 4. Write Tool execute_operations Validation Pipeline ---

@pytest.mark.asyncio
async def test_execute_operations_validation_chain():
    sim = SimulatorAdapter("seed/devices.yaml")
    mcp_test = MCPServer("test_mcp")
    register_write_tools(mcp_test, sim)

    async def invoke_exec(args: dict) -> dict:
        import json
        res = await mcp_test.call_tool("execute_operations", args)
        return json.loads(res.content[0].text)

    # A. Write without approval token is rejected
    res_no_tok = await invoke_exec(
        {
            "device_id": "hp_living_1",
            "operation_id": "set_power",
            "instruction_id": "set_power_state",
            "params": {"power": "on"},
            "idempotency_key": "k_1",
            "approval_token": "",
        }
    )
    assert res_no_tok["status"] == "rejected"
    assert res_no_tok["error_code"] == "INVALID_APPROVAL"

    # B. Write with invalid approval token is rejected
    res_bad_tok = await invoke_exec(
        {
            "device_id": "hp_living_1",
            "operation_id": "set_power",
            "instruction_id": "set_power_state",
            "params": {"power": "on"},
            "idempotency_key": "k_2",
            "approval_token": "bad.token",
        }
    )
    assert res_bad_tok["status"] == "rejected"
    assert res_bad_tok["error_code"] == "INVALID_APPROVAL"

    # C. Write with non-existent device
    tok_nonexistent = create_approval_token("p1", "h1", "ghost_device", "set_power", {"power": "on"})
    res_nonexistent = await invoke_exec(
        {
            "device_id": "ghost_device",
            "operation_id": "set_power",
            "instruction_id": "set_power_state",
            "params": {"power": "on"},
            "idempotency_key": "k_3",
            "approval_token": tok_nonexistent,
        }
    )
    assert res_nonexistent["status"] == "rejected"
    assert res_nonexistent["error_code"] == "DEVICE_NOT_FOUND"

    # D. Write to offline device
    tok_offline = create_approval_token("p2", "h2", "hp_office_2", "set_power", {"power": "on"})
    res_offline = await invoke_exec(
        {
            "device_id": "hp_office_2",
            "operation_id": "set_power",
            "instruction_id": "set_power_state",
            "params": {"power": "on"},
            "idempotency_key": "k_4",
            "approval_token": tok_offline,
        }
    )
    assert res_offline["status"] == "rejected"
    assert res_offline["error_code"] == "DEVICE_OFFLINE"

    # E. Limit exceeded (setpoint_c = 50.0 > max 30.0)
    tok_limits = create_approval_token("p3", "h3", "hp_living_1", "set_temperature", {"setpoint_c": 50.0})
    res_limits = await invoke_exec(
        {
            "device_id": "hp_living_1",
            "operation_id": "set_temperature",
            "instruction_id": "set_setpoint",
            "params": {"setpoint_c": 50.0},
            "idempotency_key": "k_5",
            "approval_token": tok_limits,
        }
    )
    assert res_limits["status"] == "rejected"
    assert res_limits["error_code"] == "LIMIT_EXCEEDED"

    # F. Critical device power off rejected without force_power_off_critical instruction
    tok_crit = create_approval_token("p4", "h4", "pump_pool_1", "set_power", {"power": "off"})
    res_crit = await invoke_exec(
        {
            "device_id": "pump_pool_1",
            "operation_id": "set_power",
            "instruction_id": "set_power_state",
            "params": {"power": "off"},
            "idempotency_key": "k_6",
            "approval_token": tok_crit,
        }
    )
    assert res_crit["status"] == "rejected"
    assert res_crit["error_code"] == "LIMIT_EXCEEDED"

    # G. Valid execution accepted
    valid_params = {"setpoint_c": 22.5}
    tok_valid = create_approval_token("p5", "h5", "hp_living_1", "set_temperature", valid_params)
    res_valid = await invoke_exec(
        {
            "device_id": "hp_living_1",
            "operation_id": "set_temperature",
            "instruction_id": "set_setpoint",
            "params": valid_params,
            "idempotency_key": "k_idem_success",
            "approval_token": tok_valid,
        }
    )
    assert res_valid["status"] == "accepted"
    assert res_valid["device_state_hint"]["setpoint_c"] == 22.5

    # H. Idempotent replay with same key returns stored result with DUPLICATE_REQUEST
    res_replay = await invoke_exec(
        {
            "device_id": "hp_living_1",
            "operation_id": "set_temperature",
            "instruction_id": "set_setpoint",
            "params": valid_params,
            "idempotency_key": "k_idem_success",
            "approval_token": tok_valid,
        }
    )
    assert res_replay["status"] == "accepted"
    assert res_replay["duplicate"] is True
    assert res_replay["duplicate_code"] == "DUPLICATE_REQUEST"

