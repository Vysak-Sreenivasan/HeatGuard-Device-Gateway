"""Device adapter interface and data models."""

from typing import Any, Protocol, runtime_checkable
from pydantic import BaseModel, Field


class DeviceLimits(BaseModel):
    min_setpoint_c: float = 16.0
    max_setpoint_c: float = 30.0
    allowed_modes: list[str] = Field(default_factory=lambda: ["heat", "cool", "auto"])


class Device(BaseModel):
    id: str
    name: str
    type: str
    room: str
    zone: str
    tags: list[str] = Field(default_factory=list)
    online: bool = True
    critical: bool = False
    critical_hint: bool = False
    power: str = "off"  # "on" | "off"
    mode: str = "heat"  # "heat" | "cool" | "auto"
    setpoint_c: float = 21.0
    current_temp_c: float = 20.0
    limits: DeviceLimits = Field(default_factory=DeviceLimits)
    alarms: list[dict[str, Any]] = Field(default_factory=list)

    def to_available_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "room": self.room,
            "zone": self.zone,
            "tags": list(self.tags),
            "online": self.online,
            "critical_hint": self.critical or self.critical_hint,
        }


class Parameter(BaseModel):
    id: str
    name: str
    unit: str


class TelemetryValue(BaseModel):
    name: str
    value: float | str
    unit: str


class DeviceState(BaseModel):
    power: str
    mode: str
    setpoint_c: float


class Telemetry(BaseModel):
    timestamp: str
    values: list[TelemetryValue]
    state: DeviceState


class Alarm(BaseModel):
    id: str
    device_id: str
    severity: str  # "warning" | "critical" | "info"
    message: str
    since: str


class Instruction(BaseModel):
    instruction_id: str
    name: str
    params_schema: dict[str, Any]


class Operation(BaseModel):
    operation_id: str
    name: str
    instructions: list[Instruction]
    limits: dict[str, Any] = Field(default_factory=dict)


class HistoryStatsDetail(BaseModel):
    count: int
    mean: float
    std: float
    min: float
    pct_25: float = Field(alias="25%")
    pct_50: float = Field(alias="50%")
    pct_75: float = Field(alias="75%")
    max: float

    model_config = {"populate_by_name": True}


class HistoryWindow(BaseModel):
    filter: str
    avg: float


class HistoryStats(BaseModel):
    window: HistoryWindow
    stats: dict[str, Any]  # count, mean, std, min, 25%, 50%, 75%, max


class Command(BaseModel):
    device_id: str
    operation_id: str
    instruction_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str
    approval_token: str
    request_id: str = ""
    client_id: str = "mcp-client"


class CommandResult(BaseModel):
    status: str  # "accepted" | "rejected"
    executed_at: str
    request_id: str
    device_state_hint: dict[str, Any] | None = None
    error: str | None = None
    error_code: str | None = None  # e.g. INVALID_APPROVAL, LIMIT_EXCEEDED, etc.


@runtime_checkable
class DeviceAdapter(Protocol):
    async def list_devices(self) -> list[Device]: ...
    async def list_parameters(self, device_id: str) -> list[Parameter]: ...
    async def get_telemetry(
        self, device_id: str, parameters: list[str] | None
    ) -> Telemetry: ...
    async def get_alarms(self, device_id: str | None) -> list[Alarm]: ...
    async def list_operations(self, device_id: str) -> list[Operation]: ...
    async def get_history(
        self, device_id: str, parameter: str, days: int
    ) -> HistoryStats: ...
    async def execute(self, cmd: Command) -> CommandResult: ...
