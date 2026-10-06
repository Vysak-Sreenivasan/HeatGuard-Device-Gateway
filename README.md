# HeatGuard Device Gateway (MCP Server)

Production-grade Model Context Protocol (MCP) server providing a standardized tool interface for heat pump and HVAC devices, built with **Python 3.11**, **uv**, the official Python MCP SDK (`mcp`), and **Streamable HTTP** transport at `/mcp`.

---

## 1. Architecture Overview

```
+-------------------------------------------------------------+
|                 MCP Client / Agent App                     |
+-------------------------------------------------------------+
          |                                       |
    [Bearer Auth]                           [Admin Auth]
  POST /mcp (Streamable HTTP)             /admin/faults, /admin/audit
          |                                       |
+---------v---------------------------------------v-----------+
|               HeatGuard Gateway (Starlette / Uvicorn)       |
|  +-------------------------------------------------------+  |
|  | FastMCP Server (Tools: read x6, execute_operations)   |  |
|  +-------------------------------------------------------+  |
|  | Security Pipeline:                                    |  |
|  |  - Bearer Token Auth                                  |  |
|  |  - HMAC-SHA256 Approval Token Verification            |  |
|  |  - Parameter Limits & Critical Device Safeguard       |  |
|  |  - SQLite Idempotency Store (24h TTL)                 |  |
|  |  - Append-Only Command Audit Log                      |  |
|  |  - Fault Injection Engine (/admin/faults)             |  |
|  +-------------------------------------------------------+  |
|  | DeviceAdapter Interface                               |  |
|  |  └── SimulatorAdapter (seed fleet + physics + faults) |  |
|  +-------------------------------------------------------+  |
+-------------------------------------------------------------+
```

### Key Specifications
- **Transport:** Streamable HTTP at `/mcp` (session-managed bidirectional SSE/POST).
- **Open route:** `GET /health`
- **Admin routes:** `GET /admin/audit`, `GET /admin/faults`, `POST /admin/faults`, `POST /admin/faults/reset` (guarded by `ADMIN_TOKEN`).
- **MCP route:** `POST /mcp` (guarded by `MCP_AUTH_TOKEN`).

---

## 2. Seed Fleet (`seed/devices.yaml`)

The simulator is pre-seeded with 9 devices:
- **Living Room (x3):** `hp_living_1` (Main Heat Pump), `hp_living_2` (Secondary Heat Pump), `hp_living_3` (Radiant Booster).
- **Bedrooms (x2):** `hp_bed_1` (Master Bedroom), `hp_bed_2` (Guest Bedroom).
- **Kitchen (x1):** `hp_kitchen_1` (Kitchen Heat Pump).
- **Office (x2):** `hp_office_1` (Main Office, online), `hp_office_2` (Back Office, **offline**).
- **Outdoor Pool (x1):** `pump_pool_1` (Pool Heat Pump, marked `critical: true`).

---

## 3. Tools Reference & MCP Annotations

All tools expose standard MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`).

### 3.1 Read Tools (`readOnlyHint = true`)

| Tool | Parameters | Description |
|---|---|---|
| `get_available_devices` | *none* | Returns list of `{id, name, type, room, zone, tags[], online, critical_hint}` |
| `list_parameters` | `device_id: str` | Returns list of measurable parameters `[{id, name, unit}]` |
| `get_telemetry_data` | `device_id: str`, `parameters?: list[str]` | Real-time telemetry `{timestamp, values:[{name, value, unit}], state:{power, mode, setpoint_c}}` |
| `get_device_alarms` | `device_id?: str` | Active alarms `[{id, device_id, severity, message, since}]` |
| `list_operations` | `device_id: str` | Supported operations, schemas, and limits `[{operation_id, name, instructions, limits}]` |
| `get_sensor_history` | `device_id: str`, `parameter: str`, `days: int` | Statistical summary `{window:{filter, avg}, stats:{count, mean, std, min, 25%, 50%, 75%, max}}` |

### 3.2 Write Tool (`readOnlyHint = false`, `idempotentHint = true`)

`execute_operations`:
- **Arguments:** `device_id`, `operation_id`, `instruction_id`, `params`, `idempotency_key`, `approval_token`.
- **Absolute Commands Only:** Only absolute commands (`power: on|off`, `setpoint_c: 21.0`, `mode: heat|cool|auto`) are supported. Toggle and relative increment commands are rejected by design.
- **Server-Side Verification Chain:**
  1. Bearer Auth (`/mcp` transport level)
  2. Approval Token Verification (`INVALID_APPROVAL` / `TOKEN_EXPIRED`)
  3. Device existence check (`DEVICE_NOT_FOUND`)
  4. Device online status check (`DEVICE_OFFLINE`)
  5. Operation and instruction support check (`UNSUPPORTED_OPERATION`)
  6. Parameter limits validation (`LIMIT_EXCEEDED`)
  7. Critical device safety rule (rejects normal power off of critical pool pump without `force_power_off_critical`)
  8. Idempotency store lookup (`DUPLICATE_REQUEST` returns cached result without re-executing)
  9. Fault injection hooks (if enabled)
  10. Hardware adapter execution (`INTERNAL` on failure)
  11. Store result in SQLite cache
  12. Append-only audit log entry

---

## 4. Security & Safety Boundaries

### 4.1 Approval Token Verification
The approval token is an HMAC-SHA256 signature generated over canonical JSON:
```json
{
  "plan_id": "pl_8f2",
  "plan_hash": "sha256:...",
  "device_id": "hp_living_1",
  "operation_id": "set_temperature",
  "params_hash": "sha256:...",
  "exp": 1760000000
}
```
Signed using `APPROVAL_SIGNING_SECRET`. The token is rejected if:
- Signature is invalid.
- Expiration (`exp`) has elapsed (default lifetime: 10 minutes).
- `device_id`, `operation_id`, or `params_hash` (canonical SHA256 of parameters) does not match the command payload.

### 4.2 Critical Device Safety Rule
The pool pump (`pump_pool_1`) is marked `critical: true`. Attempting to power it off via the standard `set_power_state` instruction is rejected with `LIMIT_EXCEEDED`. Power-off is only permitted when using the dedicated `force_power_off_critical` instruction with an `override_reason`.

### 4.3 Never-Log-Secrets Rule
The gateway strictly isolates credentials:
- Bearer tokens (`MCP_AUTH_TOKEN`, `ADMIN_TOKEN`) are never logged.
- Full `approval_token` and `APPROVAL_SIGNING_SECRET` are never written to disk or audit logs.
- Audit logs record only `plan_id` and sanitized parameter values.

---

## 5. Fault Injection (`/admin/faults`)

Enabled when `FAULTS_ENABLED=true`. Supports reproducible test runs via random seed (`FAULT_SEED` or payload `{"seed": 42}`).

### Available Fault Profiles
- `happy`: Normal operation, all faults cleared.
- `partial_failure`: Injects 30% error rate on writes and partial batch failures.
- `timeouts`: Configures device `hp_living_2` to timeout on write operations.
- `slow_network`: Adds 250ms–600ms latency to all calls.
- `chaos`: Combines latency, 25% write errors, 30% flaky reads, ignored commands on `hp_bed_2`, and timeouts on `hp_living_3`.

### Supported Fault Types
| Fault | Description |
|---|---|
| `latency` | Adds fixed (`{"ms": 300}`) or ranged (`{"min_ms": 100, "max_ms": 500}`) delay to calls. |
| `error_rate` | Float probability (0.0 to 1.0) returning `INTERNAL` errors on write calls. |
| `timeout_on` | Specific devices timeout on writes (`applied: true` mutates state despite client timeout; `applied: false` does not mutate state). |
| `offline` | Forces specified device IDs to report offline (`DEVICE_OFFLINE`). |
| `ignore_commands` | Accepts write command but does not change state (`ACCEPTED_UNVERIFIED`). |
| `delayed_state` | State changes after `delay_s` seconds, simulating eventual consistency. |
| `drift` | Shifts temperature by `delta_temp_c` and raises unexpected warning alarm. |
| `partial` | Fails specific devices in a batch. |
| `flaky_reads` | Telemetry reads fail intermittently (`flaky_reads`). |
| `reset` | Clears all active faults and restores simulator seed fleet state. |

---

## 6. Running with Docker

### 6.1 Build the Docker Image
```bash
docker build -t heatguard-mcp-server:latest .
```

### 6.2 Run the Container
```bash
docker run -d \
  --name heatguard-mcp \
  -p 8001:8001 \
  -e MCP_AUTH_TOKEN="dev-mcp-token-secret" \
  -e ADMIN_TOKEN="dev-admin-token-secret" \
  -e APPROVAL_SIGNING_SECRET="dev-approval-signing-secret-key-32chars" \
  -e FAULTS_ENABLED="true" \
  -e FAULT_PROFILE="happy" \
  heatguard-mcp-server:latest
```

### 6.3 Verify Health
```bash
curl http://localhost:8001/health
```
Response:
```json
{
  "status": "ok",
  "adapter": "simulator",
  "devices_count": 9,
  "faults_enabled": true,
  "fault_profile": "happy"
}
```

---

## 7. Connecting with MCP Inspector

You can inspect all tools, run queries, and test schemas using the official MCP Inspector.

### 7.1 Start the Server Locally
```bash
uv run uvicorn gateway.main:app --host 127.0.0.1 --port 8001
```

### 7.2 Launch MCP Inspector
Use `npx @modelcontextprotocol/inspector`:
```bash
npx @modelcontextprotocol/inspector http://localhost:8001/mcp
```

### 7.3 Configuration in Inspector UI
When prompted in the MCP Inspector interface:
1. **Transport Type:** Streamable HTTP (or SSE)
2. **URL:** `http://localhost:8001/mcp`
3. **Custom Headers:**
   ```
   Authorization: Bearer dev-mcp-token-secret
   ```
4. Click **Connect**. You will see:
   - 6 read tools (`get_available_devices`, `list_parameters`, `get_telemetry_data`, `get_device_alarms`, `list_operations`, `get_sensor_history`).
   - 1 write tool (`execute_operations`).

---

## 8. Development & Testing

### 8.1 Setup with `uv`
```bash
uv venv --python 3.11 .venv
source .venv/bin/activate
uv sync --extra dev
```

### 8.2 Run All Tests
```bash
uv run pytest -v
```

### 8.3 Run Individual Test Suites
- **Unit tests:** `uv run pytest tests/test_unit.py -v`
- **Contract tests:** `uv run pytest tests/test_contract.py -v`
- **Integration tests:** `uv run pytest tests/test_integration.py -v`
- **Fault tests:** `uv run pytest tests/test_faults.py -v`
- **Security tests:** `uv run pytest tests/test_security.py -v`

### 8.4 Schema Contract Snapshot & Hash
The active contract snapshot is preserved in [`contract/tools_schema.json`](contract/tools_schema.json).
The pinned SHA256 schema hash is:
```
38ee7cbd8d0a667d4b3e929e93f3df1fb31c40635d137c9eb8f3aab483332bb1
```
Any schema modification intentionally triggers contract failure in `test_contract.py` until the snapshot is updated.

---

## 9. Assumptions

The following design decisions were made in accordance with the spec:
1. **Transport Implementation:** FastMCP's Streamable HTTP session manager is mounted directly in Starlette at `/mcp`, managing persistent SSE streams and JSON-RPC dispatching.
2. **Token Format:** Approval tokens are encoded as `<base64url(canonical_payload)>.<hmac_sha256_hex>`, ensuring zero-dependency canonical verification.
3. **Idempotency Storage:** Backed by SQLite (`idempotency.db`) with automatic table creation and 24-hour TTL expiration.
4. **Critical Device Protection:** Rejection of normal power-off on critical devices returns `LIMIT_EXCEEDED` with a descriptive safety instruction prompt, requiring the explicit `force_power_off_critical` instruction and reason.
5. **Fault Injection Scoping:** Faults can be configured globally or targeted at specific device IDs via `POST /admin/faults`.
