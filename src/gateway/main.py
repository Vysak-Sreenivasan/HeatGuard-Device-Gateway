"""HeatGuard Device Gateway FastAPI / Starlette application."""

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from mcp.server.mcpserver import MCPServer

from gateway.adapters.simulator import SimulatorAdapter
from gateway.audit import audit_log
from gateway.auth import BearerAuthMiddleware
from gateway.config import settings
from gateway.faults import FaultManager
from gateway.tools.read import register_read_tools
from gateway.tools.write import register_write_tools

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("heatguard.gateway")

# Initialize MCP server, Adapter, and Fault Manager
mcp_server = MCPServer("heatguard")
adapter = SimulatorAdapter(settings.sim_seed_file)
fault_manager = FaultManager(adapter)

# Register MCP tools with fault hooks
register_read_tools(mcp_server, adapter, fault_manager)
register_write_tools(mcp_server, adapter, fault_manager)


async def health_endpoint(request: Request) -> JSONResponse:
    """Open health check endpoint returning adapter status."""
    devices = await adapter.list_devices()
    return JSONResponse(
        {
            "status": "ok",
            "adapter": settings.adapter,
            "devices_count": len(devices),
            "faults_enabled": settings.faults_enabled,
            "fault_profile": fault_manager.config.profile,
        }
    )


async def audit_endpoint(request: Request) -> JSONResponse:
    """Admin endpoint to retrieve command audit log."""
    limit = int(request.query_params.get("limit", 100))
    entries = await audit_log.get_entries(limit=limit)
    return JSONResponse({"count": len(entries), "audit_logs": entries})


async def get_faults_endpoint(request: Request) -> JSONResponse:
    """Admin endpoint to retrieve current fault configuration."""
    return JSONResponse(fault_manager.config.model_dump())


async def post_faults_endpoint(request: Request) -> JSONResponse:
    """Admin endpoint to update fault configuration or switch profiles."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    try:
        updated = fault_manager.update_faults(body)
        return JSONResponse(
            {
                "status": "updated",
                "profile": updated.profile,
                "config": updated.model_dump(),
            }
        )
    except PermissionError as pe:
        return JSONResponse({"error": str(pe)}, status_code=403)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


async def reset_faults_endpoint(request: Request) -> JSONResponse:
    """Admin endpoint to reset all faults and restore seed adapter state."""
    fault_manager.reset()
    return JSONResponse(
        {
            "status": "reset_completed",
            "message": "All faults cleared and simulator seed fleet restored",
            "profile": fault_manager.config.profile,
        }
    )


# Build MCP Streamable HTTP Starlette sub-app
mcp_http_app = mcp_server.streamable_http_app(streamable_http_path="/mcp")


@asynccontextmanager
async def app_lifespan(app: Starlette) -> AsyncGenerator[None, None]:
    """Manage application lifespan including MCP streamable session manager."""
    logger.info("Starting HeatGuard Gateway with adapter: %s", settings.adapter)
    async with mcp_http_app.router.lifespan_context(mcp_http_app):
        yield
    logger.info("HeatGuard Gateway stopped.")


def create_app() -> Starlette:
    """Create and configure the main Starlette application."""
    routes = [
        Route("/health", health_endpoint, methods=["GET"]),
        Route("/admin/audit", audit_endpoint, methods=["GET"]),
        Route("/admin/faults", get_faults_endpoint, methods=["GET"]),
        Route("/admin/faults", post_faults_endpoint, methods=["POST"]),
        Route("/admin/faults/reset", reset_faults_endpoint, methods=["POST"]),
        Mount("", app=mcp_http_app),
    ]

    app = Starlette(
        routes=routes,
        lifespan=app_lifespan,
    )
    app.add_middleware(BearerAuthMiddleware)
    return app


app = create_app()
