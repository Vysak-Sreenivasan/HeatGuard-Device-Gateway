"""Approval token creation and verification using HMAC-SHA256."""

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from pydantic import BaseModel

from gateway.config import settings


def canonical_json_bytes(data: Any) -> bytes:
    """Format JSON with sorted keys and minimal separators for deterministic hashing."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def compute_params_hash(params: dict[str, Any]) -> str:
    """Compute sha256:hash of canonical JSON params."""
    h = hashlib.sha256(canonical_json_bytes(params)).hexdigest()
    return f"sha256:{h}"


def create_approval_token(
    plan_id: str,
    plan_hash: str,
    device_id: str,
    operation_id: str,
    params: dict[str, Any],
    exp: int | None = None,
    secret: str | None = None,
) -> str:
    """
    Generate an HMAC-SHA256 approval token over canonical JSON payload.
    Format: base64url(canonical_payload_json).hmac_sha256_hex
    """
    secret_key = (secret or settings.approval_signing_secret).encode("utf-8")
    if exp is None:
        exp = int(time.time()) + settings.token_max_age_s

    params_hash = compute_params_hash(params)
    payload = {
        "plan_id": plan_id,
        "plan_hash": plan_hash,
        "device_id": device_id,
        "operation_id": operation_id,
        "params_hash": params_hash,
        "exp": exp,
    }

    payload_bytes = canonical_json_bytes(payload)
    sig = hmac.new(secret_key, payload_bytes, hashlib.sha256).hexdigest()
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")
    return f"{payload_b64}.{sig}"


class TokenVerificationResult(BaseModel):
    is_valid: bool
    error_code: str | None = None
    error_message: str | None = None
    plan_id: str | None = None
    payload: dict[str, Any] | None = None


def verify_approval_token(
    token: str | None,
    expected_device_id: str,
    expected_operation_id: str,
    expected_params: dict[str, Any],
    secret: str | None = None,
    current_time: int | None = None,
) -> TokenVerificationResult:
    """
    Verify approval token signature, expiration, and bound parameters.
    Returns TokenVerificationResult with error_code:
      - INVALID_APPROVAL
      - TOKEN_EXPIRED
    """
    if not token or not isinstance(token, str):
        return TokenVerificationResult(
            is_valid=False,
            error_code="INVALID_APPROVAL",
            error_message="Missing or invalid approval token",
        )

    parts = token.strip().split(".")
    if len(parts) != 2:
        return TokenVerificationResult(
            is_valid=False,
            error_code="INVALID_APPROVAL",
            error_message="Malformed approval token format",
        )

    payload_b64, signature = parts
    # Add padding if needed
    rem = len(payload_b64) % 4
    if rem:
        payload_b64 += "=" * (4 - rem)

    try:
        payload_bytes = base64.urlsafe_b64decode(payload_b64.encode("ascii"))
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return TokenVerificationResult(
            is_valid=False,
            error_code="INVALID_APPROVAL",
            error_message="Could not decode token payload",
        )

    # 1. Verify HMAC signature using constant-time comparison
    secret_key = (secret or settings.approval_signing_secret).encode("utf-8")
    canonical_bytes = canonical_json_bytes(payload)
    expected_sig = hmac.new(secret_key, canonical_bytes, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(signature.lower(), expected_sig.lower()):
        return TokenVerificationResult(
            is_valid=False,
            error_code="INVALID_APPROVAL",
            error_message="Invalid approval token signature",
            plan_id=payload.get("plan_id"),
        )

    # 2. Check expiration
    now = int(time.time()) if current_time is None else current_time
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or now > exp:
        return TokenVerificationResult(
            is_valid=False,
            error_code="TOKEN_EXPIRED",
            error_message="Approval token has expired",
            plan_id=payload.get("plan_id"),
        )

    # 3. Check bound device_id
    if payload.get("device_id") != expected_device_id:
        return TokenVerificationResult(
            is_valid=False,
            error_code="INVALID_APPROVAL",
            error_message=f"Approval token is for device '{payload.get('device_id')}', not '{expected_device_id}'",
            plan_id=payload.get("plan_id"),
        )

    # 4. Check bound operation_id
    if payload.get("operation_id") != expected_operation_id:
        return TokenVerificationResult(
            is_valid=False,
            error_code="INVALID_APPROVAL",
            error_message=f"Approval token is for operation '{payload.get('operation_id')}', not '{expected_operation_id}'",
            plan_id=payload.get("plan_id"),
        )

    # 5. Check bound params_hash
    expected_hash = compute_params_hash(expected_params)
    if payload.get("params_hash") != expected_hash:
        return TokenVerificationResult(
            is_valid=False,
            error_code="INVALID_APPROVAL",
            error_message="Approval token params_hash does not match command parameters",
            plan_id=payload.get("plan_id"),
        )

    return TokenVerificationResult(
        is_valid=True,
        plan_id=payload.get("plan_id"),
        payload=payload,
    )
