"""JWT authentication service using stdlib only (HMAC-SHA256 / HS256)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


def _b64url_encode(data: bytes) -> str:
    """Base64-URL encode bytes, stripping padding '=' characters."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    """Base64-URL decode a string, restoring padding as needed."""
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def create_token(
    user_id: int,
    email: str,
    role: str,
    secret: str,
    exp_minutes: int = 60,
) -> str:
    """Create a signed JWT (HS256) with the given claims."""
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "exp": now + exp_minutes * 60,
        "iat": now,
    }

    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    signing_input = f"{header_b64}.{payload_b64}"
    signature = hmac.new(
        secret.encode("utf-8"),
        signing_input.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    signature_b64 = _b64url_encode(signature)

    return f"{header_b64}.{payload_b64}.{signature_b64}"


def decode_token(token: str, secret: str) -> dict | None:
    """Decode and verify a JWT. Returns the payload dict or None on failure."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None

        header_b64, payload_b64, signature_b64 = parts

        # Verify signature
        signing_input = f"{header_b64}.{payload_b64}"
        expected_sig = hmac.new(
            secret.encode("utf-8"),
            signing_input.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        actual_sig = _b64url_decode(signature_b64)

        if not hmac.compare_digest(expected_sig, actual_sig):
            return None

        # Decode header and verify algorithm
        header = json.loads(_b64url_decode(header_b64))
        if header.get("alg") != "HS256":
            return None

        # Decode payload
        payload = json.loads(_b64url_decode(payload_b64))

        # Check expiration
        exp = payload.get("exp")
        if exp is not None and time.time() > exp:
            return None

        return payload

    except Exception:
        return None
