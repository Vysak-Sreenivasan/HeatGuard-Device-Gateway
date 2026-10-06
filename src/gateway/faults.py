"""Fault injection engine and admin control."""

import asyncio
import copy
import logging
import random
from typing import Any
from pydantic import BaseModel, Field

from gateway.adapters.base import Alarm
from gateway.adapters.simulator import SimulatorAdapter
from gateway.config import settings

logger = logging.getLogger("heatguard.faults")


class FaultConfig(BaseModel):
    profile: str = "happy"
    seed: int | None = None
    latency: dict[str, Any] | None = None  # {"ms": 300} or {"min_ms": 100, "max_ms": 500}
    error_rate: float = 0.0  # 0.0 to 1.0 probability of 5xx/INTERNAL on write
    timeout_on: dict[str, Any] | None = None  # {"device_ids": [...], "applied": False, "timeout_ms": 500}
    offline: list[str] = Field(default_factory=list)  # device IDs to force offline
    ignore_commands: list[str] = Field(default_factory=list)  # device IDs to ignore write state change
    delayed_state: dict[str, Any] | None = None  # {"delay_s": 2.0, "device_ids": [...]}
    drift: dict[str, Any] | None = None  # {"device_id": ..., "delta_temp_c": 3.0, "raise_alarm": True}
    partial: dict[str, Any] | None = None  # {"fail_nth": 2, "failed_device_ids": [...]}
    flaky_reads: float = 0.0  # 0.0 to 1.0 probability of read failures


class FaultManager:
    """Manages active faults and applies hooks to adapter and gateway operations."""

    def __init__(self, adapter: SimulatorAdapter) -> None:
        self.adapter = adapter
        self.config = FaultConfig()
        self._rng = random.Random()
        if settings.fault_seed is not None:
            self.set_seed(settings.fault_seed)
        if settings.fault_profile:
            self.apply_profile(settings.fault_profile)

    def set_seed(self, seed: int | None) -> None:
        self.config.seed = seed
        self._rng = random.Random(seed)

    def apply_profile(self, profile: str) -> None:
        """Apply a standard demo profile."""
        profile = profile.lower()
        self.config = FaultConfig(profile=profile, seed=self.config.seed)

        if profile == "happy":
            # Normal clean operations
            pass

        elif profile == "partial_failure":
            self.config.partial = {
                "failed_device_ids": ["hp_living_2"],
                "note": "1 of living room devices fails in batch",
            }
            self.config.error_rate = 0.3

        elif profile == "timeouts":
            self.config.timeout_on = {
                "device_ids": ["hp_living_2"],
                "applied": False,
                "timeout_ms": 200,
            }

        elif profile == "slow_network":
            self.config.latency = {"min_ms": 250, "max_ms": 600}

        elif profile == "chaos":
            self.config.latency = {"min_ms": 100, "max_ms": 300}
            self.config.error_rate = 0.25
            self.config.flaky_reads = 0.3
            self.config.ignore_commands = ["hp_bed_2"]
            self.config.timeout_on = {
                "device_ids": ["hp_living_3"],
                "applied": True,
                "timeout_ms": 150,
            }
        else:
            self.config.profile = "custom"

        self._sync_to_adapter()

    def update_faults(self, data: dict[str, Any]) -> FaultConfig:
        """Configure explicit fault settings or activate a profile."""
        if not settings.faults_enabled:
            raise PermissionError("Fault injection is disabled (FAULTS_ENABLED=false)")

        if "seed" in data:
            self.set_seed(data["seed"])

        if "profile" in data:
            self.apply_profile(data["profile"])

        # Override individual fields if provided
        for field in [
            "latency",
            "error_rate",
            "timeout_on",
            "offline",
            "ignore_commands",
            "delayed_state",
            "drift",
            "partial",
            "flaky_reads",
        ]:
            if field in data:
                setattr(self.config, field, data[field])
                self.config.profile = "custom"

        self._sync_to_adapter()
        return self.config

    def _sync_to_adapter(self) -> None:
        """Apply current fault rules to adapter devices."""
        # Sync offline devices
        if self.config.offline:
            for dev_id in self.config.offline:
                dev = self.adapter.get_device(dev_id)
                if dev:
                    dev.online = False

    def reset(self) -> None:
        """Clear faults and restore seed adapter state."""
        self.config = FaultConfig(profile="happy", seed=self.config.seed)
        self.adapter.reset()
        if self.config.seed is not None:
            self._rng = random.Random(self.config.seed)

    async def pre_call_hook(self, operation: str, device_id: str | None = None) -> None:
        """Check and inject latency or flaky read faults."""
        if not settings.faults_enabled:
            return

        # 1. Latency injection
        if self.config.latency:
            lat = self.config.latency
            delay_s = 0.0
            if "ms" in lat:
                delay_s = float(lat["ms"]) / 1000.0
            elif "min_ms" in lat and "max_ms" in lat:
                min_ms = float(lat["min_ms"])
                max_ms = float(lat["max_ms"])
                delay_s = self._rng.uniform(min_ms, max_ms) / 1000.0
            if delay_s > 0:
                await asyncio.sleep(delay_s)

        # 2. Flaky reads injection
        if operation == "read" and self.config.flaky_reads > 0:
            if self._rng.random() < self.config.flaky_reads:
                raise RuntimeError("Fault injection: Telemetry read failed (flaky_reads)")

    async def check_write_faults(
        self, device_id: str, operation_id: str, instruction_id: str, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        """
        Evaluate faults on write calls.
        Returns:
          - dict with error/result if short-circuiting or simulating fault
          - None if execution should proceed normally
        """
        if not settings.faults_enabled:
            return None

        # 1. Error rate fault
        if self.config.error_rate > 0:
            if self._rng.random() < self.config.error_rate:
                return {
                    "status": "rejected",
                    "error": "Fault injection: Simulated internal failure (error_rate)",
                    "error_code": "INTERNAL",
                }

        # 2. Partial failure fault
        if self.config.partial:
            failed_ids = self.config.partial.get("failed_device_ids", [])
            if device_id in failed_ids:
                return {
                    "status": "rejected",
                    "error": f"Fault injection: Simulated partial batch failure on '{device_id}'",
                    "error_code": "INTERNAL",
                }

        # 3. Timeout fault
        if self.config.timeout_on:
            tout_devs = self.config.timeout_on.get("device_ids", [])
            if device_id in tout_devs:
                applied = bool(self.config.timeout_on.get("applied", False))
                tout_ms = self.config.timeout_on.get("timeout_ms", 100)
                await asyncio.sleep(tout_ms / 1000.0)

                dev = self.adapter.get_device(device_id)
                if applied and dev:
                    # Device state changed behind the scenes, but request times out
                    if operation_id == "set_power" and "power" in params:
                        dev.power = params["power"]
                    elif operation_id == "set_temperature" and "setpoint_c" in params:
                        dev.setpoint_c = float(params["setpoint_c"])

                return {
                    "status": "rejected",
                    "error": f"Device '{device_id}' timed out responding to command",
                    "error_code": "INTERNAL",
                    "applied": applied,
                    "timeout": True,
                }

        # 4. Ignore commands fault (ACCEPTED_UNVERIFIED)
        if device_id in self.config.ignore_commands:
            dev = self.adapter.get_device(device_id)
            # Accept the command but DO NOT mutate device state
            return {
                "status": "accepted",
                "device_state_hint": {
                    "id": device_id,
                    "power": dev.power if dev else "off",
                    "mode": dev.mode if dev else "heat",
                    "setpoint_c": dev.setpoint_c if dev else 21.0,
                    "current_temp_c": round(dev.current_temp_c, 2) if dev else 20.0,
                },
                "verified": False,
                "note": "ACCEPTED_UNVERIFIED: device accepted command without state transition",
            }

        # 5. Delayed state fault
        if self.config.delayed_state:
            delay_s = float(self.config.delayed_state.get("delay_s", 2.0))
            d_devs = self.config.delayed_state.get("device_ids", [device_id])
            if device_id in d_devs:
                dev = self.adapter.get_device(device_id)
                old_state = {
                    "id": device_id,
                    "power": dev.power if dev else "off",
                    "mode": dev.mode if dev else "heat",
                    "setpoint_c": dev.setpoint_c if dev else 21.0,
                }

                # Schedule state change in background
                async def _delayed_apply():
                    await asyncio.sleep(delay_s)
                    if dev:
                        if operation_id == "set_power" and "power" in params:
                            dev.power = params["power"]
                        elif operation_id == "set_temperature" and "setpoint_c" in params:
                            dev.setpoint_c = float(params["setpoint_c"])
                        elif operation_id == "set_mode" and "mode" in params:
                            dev.mode = params["mode"]

                asyncio.create_task(_delayed_apply())

                return {
                    "status": "accepted",
                    "device_state_hint": old_state,
                    "delayed_state": True,
                    "delay_seconds": delay_s,
                }

        # 6. Drift fault
        if self.config.drift and self.config.drift.get("device_id") == device_id:
            dev = self.adapter.get_device(device_id)
            if dev:
                delta = float(self.config.drift.get("delta_temp_c", 2.0))
                dev.current_temp_c += delta
                if self.config.drift.get("raise_alarm"):
                    self.adapter.add_alarm(
                        Alarm(
                            id=f"alm_drift_{device_id}",
                            device_id=device_id,
                            severity="warning",
                            message=f"State drift detected on {device_id}",
                            since="now",
                        )
                    )

        return None
