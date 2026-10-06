# Spec: HeatGuard Device Gateway (MCP Server)

**Repo:** `heatguard-mcp-server` (separate from `heatguard-ai`)
**Purpose:** One standard tool interface for heat pump devices. First backend is a **simulator**. Real vendor adapters come later.

---

## 1. Goals

1. Give the agent app a generic device API through MCP, so the app has no vendor code.
2. Run fully local in Docker with no real hardware.
3. Be a realistic test bed: **fault injection** (timeouts, 5xx, offline, partial success, delayed state, drift).
4. Be safe on its own: validate every command and require a signed approval token for writes.
5. Be reusable by other MCP clients later.

## 2. Non-Goals

- Natural-language understanding (that is the app's job).
- Risk policy and human approval (app's job). The server only **checks** the approval token and enforces hard limits.
- A UI (a small admin API is enough).

## 3. Tech Choices

| Item | Choice |
|------|--------|
| Language | Python 3.11+ with `uv` |
| MCP | Official Python MCP SDK (FastMCP) |
| Transport | **Streamable HTTP** at `/mcp` (works across Docker containers) |
| Web server | Uvicorn (the MCP app mounted in a small Starlette/FastAPI app for `/health` and `/admin`) |
| State | In-memory for the simulator with optional SQLite file; idempotency store in SQLite or Postgres |
| Config | Env vars + seed YAML |
| Tests | `pytest`, MCP SDK test client |

## 4. Repo Layout

```
heatguard-mcp-server/
  pyproject.toml  Dockerfile  README.md
  seed/devices.yaml            # sample fleet
  src/gateway/
    main.py                    # app, MCP mount, /health, /admin
    config.py
    auth.py                    # bearer token check
    tokens.py                  # approval token verify (HMAC)
    idempotency.py
    tools/
      read.py                  # read tools
      write.py                 # execute_operations
    adapters/
      base.py                  # DeviceAdapter interface
      simulator.py             # in-memory devices + physics-lite + faults
      # vendor_x.py            # later
    faults.py                  # fault profiles and admin control
    audit.py                   # command-level audit log
  tests/
```

## 5. Adapter Interface

```python
class DeviceAdapter(Protocol):
    async def list_devices(self) -> list[Device]: ...
    async def list_parameters(self, device_id: str) -> list[Parameter]: ...
    async def get_telemetry(self, device_id: str, parameters: list[str] | None) -> Telemetry: ...
    async def get_alarms(self, device_id: str | None) -> list[Alarm]: ...
    async def list_operations(self, device_id: str) -> list[Operation]: ...
    async def get_history(self, device_id: str, parameter: str, days: int) -> HistoryStats: ...
    async def execute(self, cmd: Command) -> CommandResult: ...
```

`ADAPTER=simulator` selects the simulator. A real vendor adapter implements the same interface, so the tools and the app do not change.

## 6. Tools

Each tool has MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`). They help clients but are **not** a trust boundary.

### 6.1 Read tools (`readOnlyHint = true`)

| Tool | Args | Returns |
|------|------|---------|
| `get_available_devices` | none | list of `{id, name, type, room, zone, tags[], online, critical_hint}` |
| `list_parameters` | `device_id` | `[{id, name, unit}]` |
| `get_telemetry_data` | `device_id`, `parameters?` | `{timestamp, values:[{name, value, unit}], state:{power, mode, setpoint_c}}` |
| `get_device_alarms` | `device_id?` | `[{id, device_id, severity, message, since}]` |
| `list_operations` | `device_id` | `[{operation_id, name, instructions:[{instruction_id, name, params_schema}], limits}]` |
| `get_sensor_history` | `device_id`, `parameter`, `days` | `{window:{filter, avg}, stats:{count, mean, std, min, 25%, 50%, 75%, max}}` (stats only; windows as in the app design) |

### 6.2 Write tool (`readOnlyHint = false`, `idempotentHint = true`)

`execute_operations`

| Arg | Notes |
|-----|-------|
| `device_id` | required |
| `operation_id`, `instruction_id` | must exist for the device |
| `params` | validated against `params_schema` and limits |
| `idempotency_key` | required; same key returns the stored result |
| `approval_token` | required; see section 7 |

Returns `{status: "accepted"|"rejected", executed_at, request_id, device_state_hint?}`.

Error codes: `INVALID_APPROVAL`, `TOKEN_EXPIRED`, `DEVICE_NOT_FOUND`, `DEVICE_OFFLINE`, `UNSUPPORTED_OPERATION`, `LIMIT_EXCEEDED`, `DUPLICATE_REQUEST` (returns stored result), `INTERNAL`.

Server-side checks, in order: auth → token → device exists → online → operation supported → params in limits → critical-device rule → idempotency lookup → execute → store result → audit.

## 7. Approval Token

HMAC-SHA256 over a canonical JSON payload, signed with `APPROVAL_SIGNING_SECRET` (shared with the app).

```json
{
  "plan_id": "pl_8f2",
  "plan_hash": "sha256:ab12...",
  "device_id": "dev_1",
  "operation_id": "power",
  "params_hash": "sha256:cd34...",
  "exp": 1760000000
}
```

The server rejects the call if the signature is wrong, `exp` has passed, or `device_id`, `operation_id`, or `params_hash` do not match the command. Tokens are short-lived (default 10 min). This is defense in depth: it stops any client without the secret from changing hardware, even if it can reach the server.

## 8. Auth and Network

- `Authorization: Bearer <MCP_AUTH_TOKEN>` on every `/mcp` request.
- `/health` is open. `/admin` needs a separate `ADMIN_TOKEN`.
- Bind inside the Docker network. Publish port 8001 only for local debugging (MCP Inspector).
- Log request ids, never tokens or secrets.

## 9. Simulator Adapter

### 9.1 Device model
- Fields: `id`, `name`, `type`, `room`, `zone`, `tags`, `online`, `critical`, `power` (on/off), `mode` (heat/cool/auto), `setpoint_c`, `current_temp_c`, `limits` (min and max setpoint, allowed modes), `alarms`.
- Seed (`seed/devices.yaml`): living room ×3, bedroom ×2, kitchen ×1, office ×2 (one offline), pool pump ×1 (`critical: true`).
- Light physics: current temperature moves toward the setpoint over time (simple first-order model) so read-back and history look real.
- Generated history for `get_sensor_history`.

### 9.2 Fault injection (`FAULTS_ENABLED=true` only)

Controlled by `POST /admin/faults` (and optional env profile at startup).

| Fault | Effect |
|-------|--------|
| `latency` | Add delay (fixed or random range) to all or some calls |
| `error_rate` | Return 5xx/`INTERNAL` for a share of write calls |
| `timeout_on` | Specific devices never respond to writes (command may or may not be applied: `applied: true/false`) |
| `offline` | Mark devices offline |
| `ignore_commands` | Accept the command but do not change state (tests `ACCEPTED_UNVERIFIED`) |
| `delayed_state` | State changes after N seconds (eventual consistency; tests read-back polling) |
| `drift` | Change a device state or raise an alarm between preview and execute |
| `partial` | Fail N of M devices in a batch |
| `flaky_reads` | Telemetry reads fail sometimes (tests `UNKNOWN`) |
| `reset` | Clear faults and restore seed state |

Profiles for demos: `happy`, `partial_failure`, `timeouts`, `slow_network`, `chaos`.

## 10. Idempotency

- Store `idempotency_key → result` (TTL 24 h).
- A repeat returns the same result and `DUPLICATE_REQUEST` metadata, without re-executing.
- Absolute commands (set value, power on/off) are naturally idempotent. Toggle-style operations are **not** offered by the simulator by default.

## 11. Audit

Append-only command log: time, request id, client id, device, operation, params, token plan_id, result, error. Exposed at `GET /admin/audit` (admin token).

## 12. Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install uv && uv sync --frozen --no-dev
COPY src ./src
COPY seed ./seed
ENV PORT=8001
EXPOSE 8001
CMD ["uv", "run", "uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8001"]
```

Env vars: `ADAPTER`, `SIM_SEED_FILE`, `FAULTS_ENABLED`, `MCP_AUTH_TOKEN`, `ADMIN_TOKEN`, `APPROVAL_SIGNING_SECRET`, `TOKEN_MAX_AGE_S`, `LOG_LEVEL`.

## 13. Tests

| Level | What |
|-------|------|
| Unit | Token verify (valid, bad signature, expired, mismatch), limits, idempotency, simulator physics |
| Contract | Tool names, input schemas, and annotations stay stable (snapshot test; the app pins the same hash) |
| Integration | MCP client → server: all tools; write without token is rejected |
| Fault | Each fault produces the expected behavior and error code |
| Security | No token, wrong bearer, replay with changed params, expired token |

## 14. Versioning

- Semantic version for the tool contract. Any change to a tool name, description, or schema bumps the version and updates the schema snapshot.
- The app pins the schema hash, so a contract change forces a deliberate update on both sides.

## 15. Task List (server project)

**Phase A: Skeleton (P0)**
- [ ] Project setup, FastMCP app with Streamable HTTP at `/mcp`, `/health`
- [ ] Bearer auth, config, Dockerfile, health check
- [ ] `DeviceAdapter` interface and simulator with seed fleet

**Phase B: Tools (P0)**
- [ ] Six read tools with typed schemas and annotations
- [ ] `execute_operations` with validation chain
- [ ] Approval token verify and idempotency store
- [ ] Command-level audit log

**Phase C: Simulator realism (P1)**
- [ ] First-order temperature model and generated history
- [ ] Alarms (for example high temperature, low flow)
- [ ] Critical device rules (reject bulk power-off of critical devices without a special instruction)

**Phase D: Fault injection (P0 for app tests)**
- [ ] `/admin/faults` API and profiles
- [ ] All faults in section 9.2
- [ ] `reset` endpoint and seeded determinism (random seed option)

**Phase E: Quality (P1)**
- [ ] Contract snapshot test and schema hash output
- [ ] Unit, integration, fault, and security tests
- [ ] README with `docker run` and MCP Inspector instructions

**Phase F: Later (P2)**
- [ ] First real vendor adapter
- [ ] Per-client credentials and scopes (read-only vs write)
- [ ] Rate limiting and metrics endpoint
- [ ] MCP resources for device docs

## 16. Open Questions

1. Which real vendor adapter comes first?
2. Should the server hold room and zone labels, or only the app registry overlay?
3. Do other agents or teams need access (affects scopes and auth)?
4. Are toggle or increment operations ever needed? (If yes, they need no-retry handling in the app.)
