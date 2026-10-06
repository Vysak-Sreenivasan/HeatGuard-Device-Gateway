"""Write tool execute_operations with complete validation chain."""

import datetime
from typing import Any
import uuid
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from gateway.adapters.base import Command, DeviceAdapter
from gateway.audit import audit_log
from gateway.idempotency import idempotency_store
from gateway.tokens import verify_approval_token


def register_write_tools(
    mcp: MCPServer, adapter: DeviceAdapter, fault_manager: Any = None
) -> None:
    """Register execute_operations write tool with idempotentHint=True, readOnlyHint=False."""

    write_annotations = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
    )

    @mcp.tool(
        name="execute_operations",
        description="Execute an approved, idempotent absolute control operation on a device.",
        annotations=write_annotations,
    )
    async def execute_operations(
        device_id: str,
        operation_id: str,
        instruction_id: str,
        params: dict[str, Any],
        idempotency_key: str,
        approval_token: str,
    ) -> dict[str, Any]:
        """
        Server-side checks in order:
        auth -> token -> device exists -> online -> operation supported ->
        params in limits -> critical-device rule -> idempotency lookup ->
        execute -> store result -> audit.
        """
        request_id = f"req_{uuid.uuid4().hex[:12]}"
        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
        client_id = "mcp-client"

        # Apply write pre-call latency if configured
        if fault_manager:
            await fault_manager.pre_call_hook("write", device_id)

        # 0. Validate required fields
        if not idempotency_key or not isinstance(idempotency_key, str) or not idempotency_key.strip():
            result = {
                "status": "rejected",
                "executed_at": now_str,
                "request_id": request_id,
                "error": "Missing required idempotency_key",
                "error_code": "INVALID_REQUEST",
            }
            await audit_log.log(
                request_id=request_id,
                client_id=client_id,
                device_id=device_id,
                operation_id=operation_id,
                instruction_id=instruction_id,
                params=params,
                plan_id=None,
                status="rejected",
                error=result["error"],
                error_code="INVALID_REQUEST",
            )
            return result

        # 1. Token check: verify approval token signature, expiry, and parameter binding
        token_res = verify_approval_token(
            token=approval_token,
            expected_device_id=device_id,
            expected_operation_id=operation_id,
            expected_params=params,
        )
        if not token_res.is_valid:
            result = {
                "status": "rejected",
                "executed_at": now_str,
                "request_id": request_id,
                "error": token_res.error_message,
                "error_code": token_res.error_code,
            }
            await audit_log.log(
                request_id=request_id,
                client_id=client_id,
                device_id=device_id,
                operation_id=operation_id,
                instruction_id=instruction_id,
                params=params,
                plan_id=token_res.plan_id,
                status="rejected",
                error=result["error"],
                error_code=token_res.error_code,
            )
            return result

        plan_id = token_res.plan_id

        # 2. Device exists
        device = getattr(adapter, "get_device", lambda did: None)(device_id)
        if not device:
            # Fallback to searching list_devices
            devs = await adapter.list_devices()
            device = next((d for d in devs if d.id == device_id), None)

        if not device:
            result = {
                "status": "rejected",
                "executed_at": now_str,
                "request_id": request_id,
                "error": f"Device '{device_id}' not found",
                "error_code": "DEVICE_NOT_FOUND",
            }
            await audit_log.log(
                request_id=request_id,
                client_id=client_id,
                device_id=device_id,
                operation_id=operation_id,
                instruction_id=instruction_id,
                params=params,
                plan_id=plan_id,
                status="rejected",
                error=result["error"],
                error_code="DEVICE_NOT_FOUND",
            )
            return result

        # 3. Device online
        if not device.online:
            result = {
                "status": "rejected",
                "executed_at": now_str,
                "request_id": request_id,
                "error": f"Device '{device_id}' is offline",
                "error_code": "DEVICE_OFFLINE",
            }
            await audit_log.log(
                request_id=request_id,
                client_id=client_id,
                device_id=device_id,
                operation_id=operation_id,
                instruction_id=instruction_id,
                params=params,
                plan_id=plan_id,
                status="rejected",
                error=result["error"],
                error_code="DEVICE_OFFLINE",
            )
            return result

        # 4. Operation supported
        supported_ops = await adapter.list_operations(device_id)
        matched_op = next((o for o in supported_ops if o.operation_id == operation_id), None)
        if not matched_op:
            result = {
                "status": "rejected",
                "executed_at": now_str,
                "request_id": request_id,
                "error": f"Operation '{operation_id}' is unsupported on device '{device_id}'",
                "error_code": "UNSUPPORTED_OPERATION",
            }
            await audit_log.log(
                request_id=request_id,
                client_id=client_id,
                device_id=device_id,
                operation_id=operation_id,
                instruction_id=instruction_id,
                params=params,
                plan_id=plan_id,
                status="rejected",
                error=result["error"],
                error_code="UNSUPPORTED_OPERATION",
            )
            return result

        matched_inst = next(
            (inst for inst in matched_op.instructions if inst.instruction_id == instruction_id),
            None,
        )
        if not matched_inst:
            result = {
                "status": "rejected",
                "executed_at": now_str,
                "request_id": request_id,
                "error": f"Instruction '{instruction_id}' is unsupported for operation '{operation_id}'",
                "error_code": "UNSUPPORTED_OPERATION",
            }
            await audit_log.log(
                request_id=request_id,
                client_id=client_id,
                device_id=device_id,
                operation_id=operation_id,
                instruction_id=instruction_id,
                params=params,
                plan_id=plan_id,
                status="rejected",
                error=result["error"],
                error_code="UNSUPPORTED_OPERATION",
            )
            return result

        # 5. Params in limits & schemas
        limits = device.limits
        if operation_id == "set_temperature":
            setpoint = params.get("setpoint_c")
            if setpoint is None or not isinstance(setpoint, (int, float)):
                err = "Parameter 'setpoint_c' must be a number"
                return await _reject_and_audit(
                    request_id, client_id, device_id, operation_id, instruction_id,
                    params, plan_id, err, "LIMIT_EXCEEDED"
                )
            if float(setpoint) < limits.min_setpoint_c or float(setpoint) > limits.max_setpoint_c:
                err = f"Setpoint {setpoint}°C exceeds device limits [{limits.min_setpoint_c}, {limits.max_setpoint_c}]°C"
                return await _reject_and_audit(
                    request_id, client_id, device_id, operation_id, instruction_id,
                    params, plan_id, err, "LIMIT_EXCEEDED"
                )

        elif operation_id == "set_power":
            power_val = params.get("power")
            if power_val not in ("on", "off"):
                err = "Parameter 'power' must be 'on' or 'off'"
                return await _reject_and_audit(
                    request_id, client_id, device_id, operation_id, instruction_id,
                    params, plan_id, err, "LIMIT_EXCEEDED"
                )

        elif operation_id == "set_mode":
            mode_val = params.get("mode")
            if mode_val not in limits.allowed_modes:
                err = f"Mode '{mode_val}' not allowed for device. Allowed: {limits.allowed_modes}"
                return await _reject_and_audit(
                    request_id, client_id, device_id, operation_id, instruction_id,
                    params, plan_id, err, "LIMIT_EXCEEDED"
                )

        # 6. Critical device safety rule:
        # Reject bulk/standard power-off of critical devices without force_power_off_critical instruction
        if device.critical and operation_id == "set_power" and params.get("power") == "off":
            if instruction_id != "force_power_off_critical":
                err = (
                    f"Device '{device_id}' is marked CRITICAL. Standard power-off is rejected. "
                    "Must use instruction 'force_power_off_critical' with override reason."
                )
                return await _reject_and_audit(
                    request_id, client_id, device_id, operation_id, instruction_id,
                    params, plan_id, err, "LIMIT_EXCEEDED"
                )

        # 7. Idempotency store lookup
        existing = await idempotency_store.get(idempotency_key)
        if existing is not None:
            # Return cached result with DUPLICATE_REQUEST indicator
            cached_result = dict(existing)
            cached_result["duplicate"] = True
            cached_result["duplicate_code"] = "DUPLICATE_REQUEST"
            # Audit the duplicate retrieval
            await audit_log.log(
                request_id=request_id,
                client_id=client_id,
                device_id=device_id,
                operation_id=operation_id,
                instruction_id=instruction_id,
                params=params,
                plan_id=plan_id,
                status="accepted",
                error=None,
                error_code="DUPLICATE_REQUEST",
            )
            return cached_result

        # 8. Check write fault injection hooks
        if fault_manager:
            fault_res = await fault_manager.check_write_faults(
                device_id=device_id,
                operation_id=operation_id,
                instruction_id=instruction_id,
                params=params,
            )
            if fault_res is not None:
                # Fault injected
                fault_res["executed_at"] = now_str
                fault_res["request_id"] = request_id
                await idempotency_store.set(idempotency_key, fault_res)
                await audit_log.log(
                    request_id=request_id,
                    client_id=client_id,
                    device_id=device_id,
                    operation_id=operation_id,
                    instruction_id=instruction_id,
                    params=params,
                    plan_id=plan_id,
                    status=fault_res.get("status", "rejected"),
                    error=fault_res.get("error"),
                    error_code=fault_res.get("error_code"),
                )
                return fault_res

        # 9. Execute command via adapter
        cmd = Command(
            device_id=device_id,
            operation_id=operation_id,
            instruction_id=instruction_id,
            params=params,
            idempotency_key=idempotency_key,
            approval_token=approval_token,
            request_id=request_id,
            client_id=client_id,
        )

        try:
            cmd_result = await adapter.execute(cmd)
            result = cmd_result.model_dump()
        except Exception as e:
            result = {
                "status": "rejected",
                "executed_at": now_str,
                "request_id": request_id,
                "error": f"Internal execution failure: {str(e)}",
                "error_code": "INTERNAL",
            }

        # 10. Store result in idempotency cache
        await idempotency_store.set(idempotency_key, result)

        # 11. Write audit log
        await audit_log.log(
            request_id=request_id,
            client_id=client_id,
            device_id=device_id,
            operation_id=operation_id,
            instruction_id=instruction_id,
            params=params,
            plan_id=plan_id,
            status=result["status"],
            error=result.get("error"),
            error_code=result.get("error_code"),
        )

        return result


async def _reject_and_audit(
    request_id: str,
    client_id: str,
    device_id: str,
    operation_id: str,
    instruction_id: str,
    params: dict[str, Any],
    plan_id: str | None,
    error: str,
    error_code: str,
) -> dict[str, Any]:
    now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
    result = {
        "status": "rejected",
        "executed_at": now_str,
        "request_id": request_id,
        "error": error,
        "error_code": error_code,
    }
    await audit_log.log(
        request_id=request_id,
        client_id=client_id,
        device_id=device_id,
        operation_id=operation_id,
        instruction_id=instruction_id,
        params=params,
        plan_id=plan_id,
        status="rejected",
        error=error,
        error_code=error_code,
    )
    return result
