"""Bearer authentication for MCP and Admin endpoints."""

import hmac
import logging
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from gateway.config import settings

logger = logging.getLogger("heatguard.auth")


def constant_time_compare(val1: str, val2: str) -> bool:
    """Secure constant-time comparison to prevent timing attacks."""
    return hmac.compare_digest(val1.encode("utf-8"), val2.encode("utf-8"))


def extract_bearer_token(header_value: str | None) -> str | None:
    """Extract token from 'Bearer <token>' format."""
    if not header_value:
        return None
    parts = header_value.strip().split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware verifying Bearer tokens on /mcp and /admin routes.
    /health remains open. Never logs tokens or secrets.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path

        # /health is always open
        if path == "/health" or path.startswith("/health/"):
            return await call_next(request)

        auth_header = request.headers.get("Authorization")
        token = extract_bearer_token(auth_header)

        # /admin route protection
        if path.startswith("/admin"):
            if not token or not constant_time_compare(token, settings.admin_token):
                logger.warning(
                    "Unauthorized admin access attempt",
                    extra={"path": path, "client_ip": request.client.host if request.client else "unknown"},
                )
                return JSONResponse(
                    {"error": "Unauthorized", "message": "Invalid or missing admin token"},
                    status_code=401,
                )
            return await call_next(request)

        # /mcp route protection
        if path.startswith("/mcp"):
            if not token or not constant_time_compare(token, settings.mcp_auth_token):
                logger.warning(
                    "Unauthorized MCP access attempt",
                    extra={"path": path, "client_ip": request.client.host if request.client else "unknown"},
                )
                return JSONResponse(
                    {"error": "Unauthorized", "message": "Invalid or missing MCP bearer token"},
                    status_code=401,
                )
            return await call_next(request)

        return await call_next(request)
