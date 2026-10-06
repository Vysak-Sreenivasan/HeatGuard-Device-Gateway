"""Configuration settings for HeatGuard MCP Server."""

import os
from pathlib import Path
from pydantic import BaseModel, Field


def _load_env_file(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


_load_env_file()


class Settings(BaseModel):
    # Server binding
    host: str = Field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))
    port: int = Field(default_factory=lambda: int(os.getenv("PORT", "8001")))

    # Adapter selection
    adapter: str = Field(default_factory=lambda: os.getenv("ADAPTER", "simulator"))
    sim_seed_file: Path = Field(
        default_factory=lambda: Path(os.getenv("SIM_SEED_FILE", "seed/devices.yaml"))
    )

    # Authentication & security
    mcp_auth_token: str = Field(
        default_factory=lambda: os.getenv("MCP_AUTH_TOKEN", "dev-mcp-token-secret")
    )
    admin_token: str = Field(
        default_factory=lambda: os.getenv("ADMIN_TOKEN", "dev-admin-token-secret")
    )
    approval_signing_secret: str = Field(
        default_factory=lambda: os.getenv(
            "APPROVAL_SIGNING_SECRET", "dev-approval-signing-secret-key-32chars"
        )
    )
    token_max_age_s: int = Field(
        default_factory=lambda: int(os.getenv("TOKEN_MAX_AGE_S", "600"))
    )

    # Fault injection
    faults_enabled: bool = Field(
        default_factory=lambda: os.getenv("FAULTS_ENABLED", "true").lower()
        in ("1", "true", "yes")
    )
    fault_profile: str = Field(
        default_factory=lambda: os.getenv("FAULT_PROFILE", "happy")
    )
    fault_seed: int | None = Field(
        default_factory=lambda: (
            int(os.getenv("FAULT_SEED")) if os.getenv("FAULT_SEED") else None
        )
    )

    # Logging & telemetry
    log_level: str = Field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    idempotency_db_path: Path = Field(
        default_factory=lambda: Path(os.getenv("IDEMPOTENCY_DB_PATH", "idempotency.db"))
    )


settings = Settings()
