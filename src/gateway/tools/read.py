"""Read-only tools for HeatGuard Device Gateway."""

from typing import Any
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from gateway.adapters.base import DeviceAdapter


def register_read_tools(
    mcp: MCPServer, adapter: DeviceAdapter, fault_manager: Any = None
) -> None:
    """Register all 6 read tools with readOnlyHint=True annotation."""

    read_annotations = ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
    )

    @mcp.tool(
        name="get_available_devices",
        description="List all available heat pump and HVAC devices in the fleet with status and location.",
        annotations=read_annotations,
    )
    async def get_available_devices() -> list[dict[str, Any]]:
        if fault_manager:
            await fault_manager.pre_call_hook("read")
        devices = await adapter.list_devices()
        return [d.to_available_dict() for d in devices]

    @mcp.tool(
        name="list_parameters",
        description="List measurable parameters, sensors, and telemetry points available for a specific device.",
        annotations=read_annotations,
    )
    async def list_parameters(device_id: str) -> list[dict[str, Any]]:
        if fault_manager:
            await fault_manager.pre_call_hook("read", device_id)
        params = await adapter.list_parameters(device_id)
        return [p.model_dump() for p in params]

    @mcp.tool(
        name="get_telemetry_data",
        description="Retrieve current real-time telemetry data, state, and sensor readings for a device.",
        annotations=read_annotations,
    )
    async def get_telemetry_data(
        device_id: str, parameters: list[str] | None = None
    ) -> dict[str, Any]:
        if fault_manager:
            await fault_manager.pre_call_hook("read", device_id)
        telem = await adapter.get_telemetry(device_id, parameters)
        return telem.model_dump()

    @mcp.tool(
        name="get_device_alarms",
        description="Retrieve active alarms and warnings for a specific device, or all devices if device_id is omitted.",
        annotations=read_annotations,
    )
    async def get_device_alarms(
        device_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if fault_manager:
            await fault_manager.pre_call_hook("read", device_id)
        alarms = await adapter.get_alarms(device_id)
        return [a.model_dump() for a in alarms]

    @mcp.tool(
        name="list_operations",
        description="List supported control operations, instructions, parameter schemas, and limits for a device.",
        annotations=read_annotations,
    )
    async def list_operations(device_id: str) -> list[dict[str, Any]]:
        if fault_manager:
            await fault_manager.pre_call_hook("read", device_id)
        ops = await adapter.list_operations(device_id)
        return [o.model_dump() for o in ops]

    @mcp.tool(
        name="get_sensor_history",
        description="Retrieve historical statistical telemetry aggregation (count, mean, std, percentiles) for a parameter over specified days.",
        annotations=read_annotations,
    )
    async def get_sensor_history(
        device_id: str, parameter: str, days: int
    ) -> dict[str, Any]:
        if fault_manager:
            await fault_manager.pre_call_hook("read", device_id)
        history = await adapter.get_history(device_id, parameter, days)
        return history.model_dump()
