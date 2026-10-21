"""Local-only provider policy and fixed failure codes for the E3 chat client."""
import json
import socket

import httpx
import pytest

from app.services.extraction_provider import AccessToken, ChatProvider, ProviderError, check_endpoint

LOCAL = frozenset({"llm.lan", "localhost"})
ACCESS = frozenset({"llm-docflow.example.org"})
TOKEN = AccessToken("client-id-synthetic", "client-secret-synthetic")


def resolver(*addresses):
    def resolve(host, port, proto=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, proto, "", (address, port)) for address in addresses]
    return resolve


def reply(content='{"fields": []}', status=200, finish="stop", usage=None):
    def handler(request):
        handler.request = request
        if status != 200:
            return httpx.Response(status, json={"error": "x"})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": finish}],
                                         "usage": usage if usage is not None else {"prompt_tokens": 11,
                                                                                    "completion_tokens": 7}})
    return handler


def provider(handler, url="http://llm.lan:11434/v1", addresses=("192.168.1.10",), token=None):
    return ChatProvider(url, "qwen3:14b", LOCAL, ACCESS, token, transport=httpx.MockTransport(handler),
                        resolver=resolver(*addresses))


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.3.140", "100.101.102.103",
                                     "fd00::1", "::1"])
def test_private_loopback_and_tailscale_addresses_are_local(address):
    assert check_endpoint("http://llm.lan:11434/v1", LOCAL, resolver=resolver(address)) == "local"


@pytest.mark.parametrize("url,addresses", [
    ("http://llm.lan/v1", ("8.8.8.8",)),
    ("http://llm.lan/v1", ("192.168.1.10", "1.1.1.1")),
    ("http://llm.lan/v1", ("169.254.169.254",)),
    ("http://other.lan/v1", ("192.168.1.10",)),
    ("ftp://llm.lan/v1", ("192.168.1.10",)),
    ("http://user:pass@llm.lan/v1", ("192.168.1.10",)),
    ("http://llm-docflow.example.org/v1", ("192.168.1.10",)),
])
def test_public_or_unlisted_endpoints_are_refused(url, addresses):
    with pytest.raises(ProviderError, match="provider_policy"):
        check_endpoint(url, LOCAL, ACCESS, resolver=resolver(*addresses))


def test_access_host_requires_https_and_service_token():
    assert check_endpoint("https://llm-docflow.example.org/v1", LOCAL, ACCESS, TOKEN) == "access"
    for url, token in (("https://llm-docflow.example.org/v1", None), ("http://llm-docflow.example.org/v1", TOKEN)):
        with pytest.raises(ProviderError, match="provider_policy"):
            check_endpoint(url, LOCAL, ACCESS, token)


def test_access_call_sends_service_token_headers_and_never_repr_secret():
    handler = reply()
    provider(handler, "https://llm-docflow.example.org/v1", token=TOKEN).complete(
        [{"role": "user", "content": "x"}], max_tokens=10, timeout_seconds=5)
    assert handler.request.headers["CF-Access-Client-Id"] == TOKEN.client_id
    assert handler.request.headers["CF-Access-Client-Secret"] == TOKEN.client_secret
    assert "secret" not in repr(TOKEN)


def test_successful_call_reports_usage_and_requests_deterministic_json():
    handler = reply(usage={"prompt_tokens": 120, "completion_tokens": 30})
    completion = provider(handler).complete([{"role": "user", "content": "x"}], max_tokens=64, timeout_seconds=5)
    assert (completion.content, completion.prompt_tokens, completion.completion_tokens) == ('{"fields": []}', 120, 30)
    body = json.loads(handler.request.content)
    assert body["temperature"] == 0 and body["max_tokens"] == 64 and body["response_format"] == {
        "type": "json_object"} and body["stream"] is False
    assert handler.request.url.path == "/v1/chat/completions"


@pytest.mark.parametrize("handler,code", [
    (reply(status=429), "provider_quota"),
    (reply(status=500), "provider_error"),
    (reply(status=302), "provider_error"),
    (reply(finish="length"), "output_truncated"),
    (lambda request: httpx.Response(200, text="not json"), "invalid_output"),
    (lambda request: httpx.Response(200, json={"choices": []}), "invalid_output"),
    (lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow")), "provider_timeout"),
    (lambda request: (_ for _ in ()).throw(httpx.ConnectError("down")), "provider_unavailable"),
])
def test_failures_map_to_fixed_codes(handler, code):
    with pytest.raises(ProviderError) as caught:
        provider(handler).complete([{"role": "user", "content": "x"}], max_tokens=10, timeout_seconds=5)
    assert caught.value.code == code and str(caught.value) == code


def test_local_http_connects_to_the_validated_address_without_resolving_again():
    handler = reply()
    provider(handler, "http://llm.lan:11434/v1", addresses=("192.168.1.10", "10.0.0.9")).complete(
        [{"role": "user", "content": "x"}], max_tokens=10, timeout_seconds=5)
    assert (handler.request.url.host, handler.request.url.port) == ("10.0.0.9", 11434)
    assert handler.request.headers["host"] == "llm.lan:11434"
    handler = reply()
    provider(handler, "http://llm.lan/v1", addresses=("fd00::5",)).complete([], max_tokens=10, timeout_seconds=5)
    assert handler.request.url.host == "fd00::5" and handler.request.headers["host"] == "llm.lan"


def test_https_keeps_the_name_for_certificate_validation():
    handler = reply()
    provider(handler, "https://llm.lan/v1").complete([], max_tokens=10, timeout_seconds=5)
    assert handler.request.url.host == "llm.lan"


def test_policy_is_checked_before_every_call():
    handler = reply()
    with pytest.raises(ProviderError, match="provider_policy"):
        provider(handler, addresses=("93.184.216.34",)).complete([], max_tokens=10, timeout_seconds=5)
    assert not hasattr(handler, "request")
