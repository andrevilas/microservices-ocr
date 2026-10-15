"""Local MCP transport backed exclusively by the authenticated HTTP API.

Run: python -m app.mcp_stdio. Configuration comes from process environment,
never tool arguments. This module does not import server storage or workers.
"""
import asyncio
import json
import os
import re
import sys
from datetime import datetime
from urllib.parse import quote, urlsplit

import httpx2
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp_tools import INSTRUCTIONS, OcrMCPServer, register_tools

MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def configuration():
    origin = os.environ.get("OCR_API_URL", "http://127.0.0.1:8000").rstrip("/")
    token = os.environ.get("OCR_API_TOKEN", "")
    try:
        parsed = urlsplit(origin)
        valid = (not any(ord(c) < 33 for c in origin) and parsed.scheme in ("http", "https") and parsed.hostname
                 and not parsed.username and not parsed.password and not parsed.path
                 and not parsed.query and not parsed.fragment and parsed.port != 0)
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            valid = False
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("OCR_API_URL must be an HTTPS origin or a loopback HTTP origin, without credentials, path, query or fragment.")
    if not token or len(token) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError("OCR_API_TOKEN must contain a valid Bearer credential in the process environment.")
    return origin, token


class ApiClient:
    def __init__(self, origin, http):
        self.origin, self.http = origin, http

    async def invoke(self, _ctx, operation, *args):
        method, path, query, body, headers = "GET", "", None, None, None
        if operation in ("whoami", "capabilities", "metrics"):
            path = {"whoami": "/me", "capabilities": "/capabilities", "metrics": "/metrics"}[operation]
        elif operation == "list_jobs":
            keys = ("limit", "cursor", "status", "created_from", "created_to", "filename", "quality")
            query = {key: value.isoformat() if isinstance(value, datetime) else value
                     for key, value in zip(keys, args) if value is not None}
            path = "/jobs"
        elif operation in ("get_job", "report", "text", "artifact", "get_upload"):
            # IDs are a single path segment, never a client-provided URL.
            if not re.fullmatch(r"[0-9a-f]{32}", args[0]):
                code = "upload_not_found" if operation == "get_upload" else "job_not_found"
                raise ToolError(f"{code}: Resource not found.")
            path = ("/uploads/" if operation == "get_upload" else "/jobs/") + quote(args[0], safe="")
            if operation in ("report", "text", "artifact"):
                path += "/" + operation
            if operation == "text":
                query = dict(zip(("page", "offset", "max_chars"), args[1:]))
        elif operation == "prepare_upload":
            method, path = "POST", "/upload-sessions"
            body = dict(zip(("filename", "size_bytes", "sha256"), args))
        elif operation == "submit_job":
            method, path = "POST", "/jobs"
            body = {"upload_id": args[0], "options": args[2].model_dump(mode="json")}
            headers = {"Idempotency-Key": args[1]}
        else:
            raise ToolError("unknown_operation: Operation is not supported.")
        try:
            # No redirect, proxy inheritance, automatic retry or arbitrary URL fetch.
            # A timeout may occur after admission: reuse the original key.
            async with asyncio.timeout(120):
                async with self.http.stream(method, self.origin + "/api/v1" + path,
                                            params=query, json=body, headers=headers) as response:
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ToolError("upstream_response_limit: API response exceeds the client limit.")
                        data.extend(chunk)
                    if response.is_redirect:
                        raise ToolError("upstream_redirect: Configure the final API origin; redirects are not followed.")
                    payload = json.loads(data)
                    if response.is_error:
                        code = payload.get("error", {}).get("code", "upstream_error")
                        if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,64}", code):
                            code = "upstream_error"
                        # Do not echo upstream HTML, debug errors, URLs or credentials.
                        raise ToolError(f"{code}: API rejected the operation (HTTP {response.status_code}); retry submissions with the same key.")
                    if not isinstance(payload, dict):
                        raise ValueError("Expected an object")
                    return payload
        except (httpx2.HTTPError, TimeoutError):
            raise ToolError("api_unavailable: API connection failed or timed out; retry submissions with the same key.") from None
        except (ValueError, AttributeError, TypeError):
            raise ToolError("invalid_api_response: API returned an invalid response.") from None


async def run():
    origin, token = configuration()
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + token},
                                 follow_redirects=False, trust_env=False,
                                 timeout=120, limits=httpx2.Limits(max_connections=8)) as http:
        server = OcrMCPServer("OCR Recognizer", version="0.2.0", instructions=INSTRUCTIONS, log_level="WARNING")
        register_tools(server, ApiClient(origin, http).invoke)
        await server.run_stdio_async()


def main():
    try:
        asyncio.run(run())
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
