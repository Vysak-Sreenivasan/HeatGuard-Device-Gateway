"""Simulator device adapter with in-memory devices and light physics."""

import copy
import datetime
import math
from pathlib import Path
from typing import Any
import yaml

from gateway.adapters.base import (
    Alarm,
    Command,
    CommandResult,
    Device,
    DeviceLimits,
    HistoryStats,
    Instruction,
    Operation,
    Parameter,
    Telemetry,
    TelemetryValue,
    DeviceState,
)


class SimulatorAdapter:
    def __init__(self, seed_file: Path | str = "seed/devices.yaml") -> None:
        self.seed_file = Path(seed_file)
        self._seed_data: list[dict[str, Any]] = []
        self._devices: dict[str, Device] = {}
        self._alarms: dict[str, list[Alarm]] = {}
        self.load_seed()

    def load_seed(self) -> None:
        if not self.seed_file.is_file():
            # Fallback default seed if file not found
            self._devices = {}
            return

        with open(self.seed_file, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        dev_list = raw.get("devices", [])
        self._seed_data = copy.deepcopy(dev_list)
        self._devices.clear()
        self._alarms.clear()

        for d in dev_list:
            limits_data = d.get("limits", {})
            limits = DeviceLimits(
                min_setpoint_c=limits_data.get("min_setpoint_c", 16.0),
                max_setpoint_c=limits_data.get("max_setpoint_c", 30.0),
                allowed_modes=limits_data.get("allowed_modes", ["heat", "cool", "auto"]),
            )
            device = Device(
                id=d["id"],
                name=d["name"],
                type=d["type"],
                room=d["room"],
                zone=d["zone"],
                tags=d.get("tags", []),
                online=d.get("online", True),
                critical=d.get("critical", False),
                critical_hint=d.get("critical", False),
                power=d.get("power", "off"),
                mode=d.get("mode", "heat"),
                setpoint_c=float(d.get("setpoint_c", 21.0)),
                current_temp_c=float(d.get("current_temp_c", 20.0)),
                limits=limits,
                alarms=d.get("alarms", []),
            )
            self._devices[device.id] = device

            # Initialize alarms if present
            dev_alarms = []
            for idx, a in enumerate(d.get("alarms", [])):
                dev_alarms.append(
                    Alarm(
                        id=a.get("id", f"alm_{device.id}_{idx}"),
                        device_id=device.id,
                        severity=a.get("severity", "warning"),
                        message=a.get("message", "Simulated alarm"),
                        since=a.get("since", datetime.datetime.now(datetime.timezone.utc).isoformat()),
                    )
                )
            self._alarms[device.id] = dev_alarms

    def reset(self) -> None:
        """Reset simulator state back to seed data."""
        self.load_seed()

    def get_device(self, device_id: str) -> Device | None:
        return self._devices.get(device_id)

    async def list_devices(self) -> list[Device]:
        return list(self._devices.values())

    async def list_parameters(self, device_id: str) -> list[Parameter]:
        if device_id not in self._devices:
            raise KeyError(f"Device {device_id} not found")

        dev = self._devices[device_id]
        params = [
            Parameter(id="current_temp_c", name="Current Temperature", unit="°C"),
            Parameter(id="setpoint_c", name="Temperature Setpoint", unit="°C"),
            Parameter(id="power", name="Power State", unit="state"),
            Parameter(id="mode", name="Operating Mode", unit="mode"),
            Parameter(id="compressor_freq_hz", name="Compressor Frequency", unit="Hz"),
        ]
        if "pool" in dev.type or "circulation" in dev.tags or "pool" in dev.tags:
            params.append(Parameter(id="flow_rate_lpm", name="Flow Rate", unit="L/min"))
        return params

    async def get_telemetry(
        self, device_id: str, parameters: list[str] | None = None
    ) -> Telemetry:
        if device_id not in self._devices:
            raise KeyError(f"Device {device_id} not found")

        dev = self._devices[device_id]
        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # Update first-order temperature simulation
        self._simulate_physics_step(dev)

        freq = 45.0 if dev.power == "on" else 0.0
        all_values = [
            TelemetryValue(name="current_temp_c", value=round(dev.current_temp_c, 2), unit="°C"),
            TelemetryValue(name="setpoint_c", value=round(dev.setpoint_c, 2), unit="°C"),
            TelemetryValue(name="power", value=dev.power, unit="state"),
            TelemetryValue(name="mode", value=dev.mode, unit="mode"),
            TelemetryValue(name="compressor_freq_hz", value=freq, unit="Hz"),
        ]
        if "pool" in dev.type or "circulation" in dev.tags or "pool" in dev.tags:
            flow = 85.0 if dev.power == "on" else 0.0
            all_values.append(TelemetryValue(name="flow_rate_lpm", value=flow, unit="L/min"))

        if parameters:
            selected = [v for v in all_values if v.name in parameters]
        else:
            selected = all_values

        return Telemetry(
            timestamp=now_str,
            values=selected,
            state=DeviceState(
                power=dev.power,
                mode=dev.mode,
                setpoint_c=round(dev.setpoint_c, 2),
            ),
        )

    def step_physics(self, dt_minutes: float = 1.0) -> None:
        """
        Advance simulator physics by dt_minutes using exact first-order linear dynamics:
        dT/dt = (1 / tau) * (T_target - T)
        T(t + dt) = T_target + (T(t) - T_target) * exp(-dt / tau)
        """
        ambient_temp = 18.0
        tau = 30.0  # time constant in minutes

        for dev in self._devices.values():
            if dev.power == "on":
                target = dev.setpoint_c
            else:
                target = ambient_temp

            decay = math.exp(-dt_minutes / tau)
            dev.current_temp_c = target + (dev.current_temp_c - target) * decay

            # Update alarms based on condition
            self._update_device_alarms(dev)

    def _simulate_physics_step(self, dev: Device) -> None:
        """Simulate small step when telemetry is polled."""
        self.step_physics(dt_minutes=0.5)

    def _update_device_alarms(self, dev: Device) -> None:
        """Synchronize dynamic alarms for device based on state and physics."""
        current_alarms = self._alarms.get(dev.id, [])
        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # Filter out dynamic auto-alarms to re-evaluate
        persisted = [
            a for a in current_alarms
            if a.id not in (f"alm_{dev.id}_offline", f"alm_{dev.id}_hitemp", f"alm_{dev.id}_lowflow")
        ]

        if not dev.online:
            persisted.append(
                Alarm(
                    id=f"alm_{dev.id}_offline",
                    device_id=dev.id,
                    severity="critical",
                    message="Device unreachable / communication offline",
                    since=now_str,
                )
            )

        if dev.current_temp_c >= 32.0:
            persisted.append(
                Alarm(
                    id=f"alm_{dev.id}_hitemp",
                    device_id=dev.id,
                    severity="critical",
                    message=f"High temperature threshold exceeded: {round(dev.current_temp_c, 1)}°C",
                    since=now_str,
                )
            )

        if dev.critical and dev.power == "on" and getattr(dev, "simulated_low_flow", False):
            persisted.append(
                Alarm(
                    id=f"alm_{dev.id}_lowflow",
                    device_id=dev.id,
                    severity="warning",
                    message="Low water flow rate detected on critical circulation loop",
                    since=now_str,
                )
            )

        self._alarms[dev.id] = persisted

    def add_alarm(self, alarm: Alarm) -> None:
        """Manually inject an alarm."""
        if alarm.device_id not in self._alarms:
            self._alarms[alarm.device_id] = []
        self._alarms[alarm.device_id].append(alarm)

    def clear_alarms(self, device_id: str | None = None) -> None:
        """Clear alarms for a device or all devices."""
        if device_id:
            self._alarms[device_id] = []
        else:
            self._alarms.clear()

    async def get_alarms(self, device_id: str | None = None) -> list[Alarm]:
        if device_id:
            if device_id not in self._devices:
                raise KeyError(f"Device {device_id} not found")
            dev = self._devices[device_id]
            self._update_device_alarms(dev)
            return list(self._alarms.get(device_id, []))

        all_alarms = []
        for dev in self._devices.values():
            self._update_device_alarms(dev)
            all_alarms.extend(self._alarms.get(dev.id, []))
        return all_alarms

    async def list_operations(self, device_id: str) -> list[Operation]:
        if device_id not in self._devices:
            raise KeyError(f"Device {device_id} not found")

        dev = self._devices[device_id]
        limits = dev.limits

        # Operation 1: set_power (Absolute only, no toggle!)
        power_instructions = [
            Instruction(
                instruction_id="set_power_state",
                name="Set Power State",
                params_schema={
                    "type": "object",
                    "properties": {
                        "power": {
                            "type": "string",
                            "enum": ["on", "off"],
                            "description": "Target absolute power state",
                        }
                    },
                    "required": ["power"],
                    "additionalProperties": False,
                },
            )
        ]
        if dev.critical:
            power_instructions.append(
                Instruction(
                    instruction_id="force_power_off_critical",
                    name="Force Power Off Critical Device",
                    params_schema={
                        "type": "object",
                        "properties": {
                            "power": {
                                "type": "string",
                                "enum": ["off"],
                                "description": "Confirmed power off for critical device",
                            },
                            "override_reason": {
                                "type": "string",
                                "description": "Reason for shutting down critical device",
                            },
                        },
                        "required": ["power", "override_reason"],
                        "additionalProperties": False,
                    },
                )
            )

        set_power_op = Operation(
            operation_id="set_power",
            name="Set Device Power",
            instructions=power_instructions,
            limits={"allowed_states": ["on", "off"], "critical_device": dev.critical},
        )

        # Operation 2: set_temperature (Absolute setpoint only, no increment/decrement!)
        set_temp_op = Operation(
            operation_id="set_temperature",
            name="Set Target Temperature",
            instructions=[
                Instruction(
                    instruction_id="set_setpoint",
                    name="Set Target Setpoint",
                    params_schema={
                        "type": "object",
                        "properties": {
                            "setpoint_c": {
                                "type": "number",
                                "minimum": limits.min_setpoint_c,
                                "maximum": limits.max_setpoint_c,
                                "description": f"Target temperature in °C ({limits.min_setpoint_c}-{limits.max_setpoint_c})",
                            }
                        },
                        "required": ["setpoint_c"],
                        "additionalProperties": False,
                    },
                )
            ],
            limits={
                "min_setpoint_c": limits.min_setpoint_c,
                "max_setpoint_c": limits.max_setpoint_c,
            },
        )

        # Operation 3: set_mode (Absolute mode only)
        set_mode_op = Operation(
            operation_id="set_mode",
            name="Set Operating Mode",
            instructions=[
                Instruction(
                    instruction_id="set_mode",
                    name="Set Mode",
                    params_schema={
                        "type": "object",
                        "properties": {
                            "mode": {
                                "type": "string",
                                "enum": limits.allowed_modes,
                                "description": f"Operating mode: {limits.allowed_modes}",
                            }
                        },
                        "required": ["mode"],
                        "additionalProperties": False,
                    },
                )
            ],
            limits={"allowed_modes": limits.allowed_modes},
        )

        return [set_power_op, set_temp_op, set_mode_op]

    async def get_history(
        self, device_id: str, parameter: str, days: int
    ) -> HistoryStats:
        if device_id not in self._devices:
            raise KeyError(f"Device {device_id} not found")

        dev = self._devices[device_id]
        days = max(1, min(days, 90))

        # Synthetic history generation based on current setpoint and parameter
        base = dev.setpoint_c if parameter == "setpoint_c" else dev.current_temp_c
        count = days * 24  # hourly readings

        # Generate a deterministic pseudo-series based on device id and days
        seed_offset = sum(ord(c) for c in device_id + parameter)
        points: list[float] = []
        for i in range(count):
            cycle = math.sin((i + seed_offset) / 12.0 * math.pi) * 1.5
            val = round(base + cycle, 2)
            points.append(val)

        points.sort()
        mean_val = round(sum(points) / count, 2)
        variance = sum((p - mean_val) ** 2 for p in points) / count
        std_val = round(math.sqrt(variance), 2)
        min_val = points[0]
        max_val = points[-1]
        p25 = points[int(count * 0.25)]
        p50 = points[int(count * 0.50)]
        p75 = points[int(count * 0.75)]

        return HistoryStats(
            window={"filter": f"{days}d", "avg": mean_val},
            stats={
                "count": count,
                "mean": mean_val,
                "std": std_val,
                "min": min_val,
                "25%": p25,
                "50%": p50,
                "75%": p75,
                "max": max_val,
            },
        )

    async def execute(self, cmd: Command) -> CommandResult:
        if cmd.device_id not in self._devices:
            return CommandResult(
                status="rejected",
                executed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                request_id=cmd.request_id,
                error=f"Device '{cmd.device_id}' not found",
                error_code="DEVICE_NOT_FOUND",
            )

        dev = self._devices[cmd.device_id]
        if not dev.online:
            return CommandResult(
                status="rejected",
                executed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                request_id=cmd.request_id,
                error=f"Device '{cmd.device_id}' is offline",
                error_code="DEVICE_OFFLINE",
            )

        # Apply state changes based on operation
        if cmd.operation_id == "set_power":
            power_val = cmd.params.get("power")
            if power_val in ("on", "off"):
                dev.power = power_val
        elif cmd.operation_id == "set_temperature":
            setpoint = cmd.params.get("setpoint_c")
            if setpoint is not None:
                dev.setpoint_c = float(setpoint)
        elif cmd.operation_id == "set_mode":
            mode_val = cmd.params.get("mode")
            if mode_val:
                dev.mode = str(mode_val)

        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return CommandResult(
            status="accepted",
            executed_at=now_str,
            request_id=cmd.request_id,
            device_state_hint={
                "id": dev.id,
                "power": dev.power,
                "mode": dev.mode,
                "setpoint_c": dev.setpoint_c,
                "current_temp_c": round(dev.current_temp_c, 2),
            },
        )
