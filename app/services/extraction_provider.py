"""OpenAI-compatible chat client for the owner's local model runtime (Ollama, llama.cpp, vLLM).

Owner decisions of 2026-10-21: document text only goes to the owner's own model
runtime. Two endpoint classes are accepted, checked before every call:

* hosts in EXTRACTION_ALLOWED_HOSTS whose every resolved address is loopback,
  private (RFC 1918/ULA) or Tailscale CGNAT;
* hostnames in EXTRACTION_ACCESS_HOSTS published through the owner's Cloudflare
  Tunnel + Access (the same route the LicitaFlow uses), HTTPS only and always
  with a Cloudflare Access service token.

Errors carry fixed codes only; logs never contain prompts, completions or credentials.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import socket
import time
from urllib.parse import urlsplit, urlunsplit

import httpx2

_CGNAT = ipaddress.ip_network("100.64.0.0/10")


class ProviderError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Completion:
    content: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


@dataclass(frozen=True)
class AccessToken:
    """Cloudflare Access service token; repr never shows the secret."""
    client_id: str
    client_secret: str

    def __repr__(self) -> str:
        return "AccessToken(<redacted>)"


def _local_address(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return False
    return ip.is_loopback or ip.is_private or (ip.version == 4 and ip in _CGNAT)


def check_endpoint(base_url: str, allowed_hosts: frozenset[str], access_hosts: frozenset[str] = frozenset(),
                   access_token: AccessToken | None = None, resolver=socket.getaddrinfo) -> str:
    """Return the endpoint class ('local' or 'access') or raise provider_policy."""
    return _checked(base_url, allowed_hosts, access_hosts, access_token, resolver)[0]


def _checked(base_url, allowed_hosts, access_hosts, access_token, resolver) -> tuple[str, list[str]]:
    """Endpoint class plus the validated addresses of a local endpoint (sorted, for pinning)."""
    parts = urlsplit(base_url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        raise ProviderError("provider_policy")
    if host in access_hosts:
        if parts.scheme != "https" or access_token is None:
            raise ProviderError("provider_policy")
        return "access", []
    if host not in allowed_hosts:
        raise ProviderError("provider_policy")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addresses = {info[4][0] for info in resolver(host, port, proto=socket.IPPROTO_TCP)}
    except OSError:
        raise ProviderError("provider_unavailable") from None
    if not addresses or not all(_local_address(address) for address in addresses):
        raise ProviderError("provider_policy")
    return "local", sorted(addresses)


def _pinned(base_url: str, address: str) -> tuple[str, str]:
    """Plain-HTTP URL aimed at the validated address, plus the original Host header.

    The client never resolves the name again, so a DNS answer changed after the
    check cannot redirect document text (no rebinding window). HTTPS keeps the
    name, because certificate validation already binds the connection to it.
    """
    parts = urlsplit(base_url)
    literal = f"[{address}]" if ":" in address else address
    netloc = literal + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, "")), parts.netloc.rsplit("@", 1)[-1]


class ChatProvider:
    """One synchronous chat-completions call per invocation, JSON-object mode, temperature 0."""

    def __init__(self, base_url: str, model: str, allowed_hosts: frozenset[str],
                 access_hosts: frozenset[str] = frozenset(), access_token: AccessToken | None = None,
                 api_key: str | None = None, transport: httpx2.BaseTransport | None = None,
                 resolver=socket.getaddrinfo, reasoning_effort: str | None = "none"):
        self.base_url = base_url.rstrip("/")
        # Reasoning models (qwen3) otherwise spend the output budget thinking, and Ollama does
        # not report those tokens in usage; "none" keeps answers direct and usage accurate.
        self.reasoning_effort = reasoning_effort
        self.model = model
        self.allowed_hosts = allowed_hosts
        self.access_hosts = access_hosts
        self._access_token = access_token
        self._api_key = api_key
        self._transport = transport
        self._resolver = resolver

    def complete(self, messages: list[dict], *, max_tokens: int, timeout_seconds: float) -> Completion:
        kind, addresses = _checked(self.base_url, self.allowed_hosts, self.access_hosts, self._access_token,
                                   self._resolver)
        url = self.base_url
        headers = {"Content-Type": "application/json"}
        if kind == "local" and urlsplit(url).scheme == "http":
            url, headers["Host"] = _pinned(url, addresses[0])
        if self._api_key:
            headers["Authorization"] = "Bearer " + self._api_key
        if kind == "access":
            headers["CF-Access-Client-Id"] = self._access_token.client_id
            headers["CF-Access-Client-Secret"] = self._access_token.client_secret
        body = {"model": self.model, "messages": messages, "temperature": 0, "max_tokens": max_tokens,
                "stream": False, "response_format": {"type": "json_object"}}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        started = time.monotonic()
        try:
            with httpx2.Client(transport=self._transport, timeout=timeout_seconds, trust_env=False,
                              follow_redirects=False) as client:
                response = client.post(url + "/chat/completions", json=body, headers=headers)
        except httpx2.TimeoutException:
            raise ProviderError("provider_timeout") from None
        except httpx2.HTTPError:
            raise ProviderError("provider_unavailable") from None
        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code == 429:
            raise ProviderError("provider_quota")
        if response.status_code != 200:
            # Access denials (302 to login, 403) land here too: never followed.
            raise ProviderError("provider_error")
        try:
            payload = response.json()
            choice = payload["choices"][0]
            content = choice["message"]["content"]
            usage = payload.get("usage") or {}
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
        except (ValueError, KeyError, IndexError, TypeError):
            raise ProviderError("invalid_output") from None
        if type(content) is not str:
            raise ProviderError("invalid_output")
        if choice.get("finish_reason") == "length":
            # A cut answer is never parsed as if it were complete.
            raise ProviderError("output_truncated")
        return Completion(content, max(0, prompt_tokens), max(0, completion_tokens), latency_ms)
