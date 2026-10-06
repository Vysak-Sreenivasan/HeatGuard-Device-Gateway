"""Unit test suite: Token verification, limits, idempotency, and simulator physics."""

import time
import pytest
from gateway.adapters.simulator import SimulatorAdapter
from gateway.idempotency import idempotency_store
from gateway.tokens import create_approval_token, verify_approval_token


# 1. Token Verification Unit Tests
def test_unit_token_valid():
    params = {"power": "on"}
    token = create_approval_token("p1", "h1", "dev_1", "set_power", params)
    res = verify_approval_token(token, "dev_1", "set_power", params)
    assert res.is_valid is True
    assert res.plan_id == "p1"


def test_unit_token_bad_signature():
    params = {"power": "on"}
    token = create_approval_token("p1", "h1", "dev_1", "set_power", params, secret="fake-secret")
    res = verify_approval_token(token, "dev_1", "set_power", params)
    assert res.is_valid is False
    assert res.error_code == "INVALID_APPROVAL"


def test_unit_token_expired():
    params = {"power": "on"}
    past = int(time.time()) - 200
    token = create_approval_token("p1", "h1", "dev_1", "set_power", params, exp=past)
    res = verify_approval_token(token, "dev_1", "set_power", params)
    assert res.is_valid is False
    assert res.error_code == "TOKEN_EXPIRED"


def test_unit_token_mismatches():
    params = {"power": "on"}
    token = create_approval_token("p1", "h1", "dev_1", "set_power", params)

    assert not verify_approval_token(token, "dev_2", "set_power", params).is_valid
    assert not verify_approval_token(token, "dev_1", "set_mode", params).is_valid
    assert not verify_approval_token(token, "dev_1", "set_power", {"power": "off"}).is_valid


# 2. Idempotency Unit Tests
@pytest.mark.asyncio
async def test_unit_idempotency_store():
    await idempotency_store.clear()
    key = "u-key-1"
    res = {"status": "accepted", "id": 123}
    await idempotency_store.set(key, res)

    fetched = await idempotency_store.get(key)
    assert fetched == res


# 3. Simulator Limits & Physics Unit Tests
def test_unit_simulator_physics():
    sim = SimulatorAdapter("seed/devices.yaml")
    dev = sim.get_device("hp_living_1")
    dev.power = "on"
    dev.current_temp_c = 18.0
    dev.setpoint_c = 25.0

    # Step physics forward
    sim.step_physics(dt_minutes=30.0)
    assert dev.current_temp_c > 18.0
    assert dev.current_temp_c < 25.0

    # Turn off and verify cooling toward ambient (18.0)
    dev.power = "off"
    dev.current_temp_c = 24.0
    sim.step_physics(dt_minutes=60.0)
    assert dev.current_temp_c < 24.0
    assert dev.current_temp_c >= 18.0


@pytest.mark.asyncio
async def test_unit_device_limits():
    sim = SimulatorAdapter("seed/devices.yaml")
    dev = sim.get_device("hp_living_1")
    assert dev.limits.min_setpoint_c == 16.0
    assert dev.limits.max_setpoint_c == 30.0
    assert "heat" in dev.limits.allowed_modes
