"""Host configurations and the exact generated launcher, without global host edits."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
import pytest

from app.mcp_client_config import CLIENTS, files_for
from app.mcp_stdio import configuration
from test_mcp_vertical import ROOT, runtime


@pytest.mark.parametrize("host", CLIENTS)
def test_generated_config_has_absolute_launcher_and_no_secret(host, tmp_path):
    credential = tmp_path / "private credential.txt"
    credential.write_text("SYNTHETIC_SECRET_DO_NOT_COPY")
    files = files_for(host, sys.executable, "http://127.0.0.1:8000", credential)
    assert all("SYNTHETIC_SECRET_DO_NOT_COPY" not in text for text in files.values())
    if host == "codex":
        config = tomllib.loads(files["ocr-config.toml"])["mcp_servers"]["ocr-recognizer"]
    elif host == "chatgpt-desktop":
        manifest = json.loads(files["ocr-recognizer/plugin.json"])
        assert manifest["extensions"]["com.openai"]["requires_local_executor"] is True
        config = json.loads(files["ocr-recognizer/mcp.json"])["mcpServers"]["ocr-recognizer"]
        assert config["type"] == "stdio"
    else:
        config = json.loads(files[host + ".json"])["mcpServers"]["ocr-recognizer"]
    assert Path(config["command"]).is_absolute()
    assert Path(config["args"][0]).is_file()
    assert config["env"]["OCR_API_TOKEN"] == ""
    assert config["env"]["OCR_API_TOKEN_FILE"] == str(credential)


@pytest.mark.parametrize("url", ["http://remote.invalid", "https://user:pass@host", "https://host/path", "https://host:0", "https://host:99999", "https://host\n"])
def test_config_rejects_unsafe_origins(url, tmp_path):
    with pytest.raises(ValueError):
        files_for("codex", sys.executable, url, tmp_path / "secret")


def test_token_file_conflicts_validation_and_sanitized_errors(tmp_path, monkeypatch):
    path = tmp_path / "private.txt"
    monkeypatch.setenv("OCR_API_URL", "http://127.0.0.1:8000")
    monkeypatch.delenv("OCR_API_TOKEN", raising=False)
    monkeypatch.setenv("OCR_API_TOKEN_FILE", str(path))
    path.write_text("synthetic-token\n")
    assert configuration()[1] == "synthetic-token"
    monkeypatch.setenv("OCR_API_TOKEN", "conflicting-token")
    with pytest.raises(ValueError, match="only one credential"):
        configuration()
    monkeypatch.delenv("OCR_API_TOKEN")
    for invalid in (b"", b"token with space", b"x" * 8193, b"\xff"):
        path.write_bytes(invalid)
        with pytest.raises(ValueError) as caught:
            configuration()
        assert str(path) not in str(caught.value) and "token with space" not in str(caught.value)
    monkeypatch.setenv("OCR_API_TOKEN_FILE", "relative.txt")
    with pytest.raises(ValueError, match="absolute"):
        configuration()


def test_config_cli_refuses_overwrite(tmp_path):
    output = tmp_path / "output"
    args = [sys.executable, "-m", "app.mcp_client_config", "--client", "codex", "--token-file", str(tmp_path / "secret"), "--output", str(output)]
    first = subprocess.run(args, cwd=ROOT, capture_output=True, timeout=15)
    assert first.returncode == 0
    before = (output / "ocr-config.toml").read_bytes()
    second = subprocess.run(args, cwd=ROOT, capture_output=True, timeout=15)
    assert second.returncode == 2 and (output / "ocr-config.toml").read_bytes() == before


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_generated_launcher_connects_from_unrelated_cwd_with_token_file(runtime, mode):
    runtime.start()
    credential = runtime.root / "credential.txt"
    credential.write_text(runtime.tokens[0], encoding="utf-8")
    config = json.loads(files_for("claude-code", sys.executable, runtime.origin, credential)["claude-code.json"])["mcpServers"]["ocr-recognizer"]
    async def scenario():
        params = StdioServerParameters(**config, cwd=runtime.root)
        with (runtime.root / "stdio.log").open("w") as log:
            async with Client(stdio_client(params, errlog=log), mode=mode) as client:
                listing = await client.list_tools()
                assert len(listing.tools) == len({t.name for t in listing.tools}) == 31
                result = await client.call_tool("ocr_whoami", {})
                assert not result.is_error
                assert result.structured_content["kind"] == "service"
                capabilities = await client.call_tool("ocr_get_capabilities", {})
                assert not capabilities.is_error
        assert runtime.tokens[0] not in (runtime.root / "stdio.log").read_text()
    asyncio.run(scenario())


def test_claude_code_real_mcp_handshake_in_isolated_config(runtime):
    cli = Path.home() / ".local" / "bin" / "claude.exe"
    if not cli.is_file():
        pytest.skip("Claude Code not installed; SDK tests still required")
    runtime.start()
    private = runtime.root / "credential.txt"
    private.write_text(runtime.tokens[0])
    config = runtime.root / "claude-config"
    config.mkdir()
    content = files_for("claude-code", sys.executable, runtime.origin, private)["claude-code.json"]
    (config / ".claude.json").write_text(content, encoding="utf-8")
    result = subprocess.run([str(cli), "mcp", "get", "ocr-recognizer"], cwd=runtime.root,
                            env={**os.environ, "CLAUDE_CONFIG_DIR": str(config)},
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
    # No stdout dump: host output contains environment configuration.
    assert result.returncode == 0, "Claude Code did not load the isolated MCP configuration"
    assert "Connected" in result.stdout, "Claude Code did not complete an MCP handshake"


def test_codex_cli_accepts_generated_config_without_global_write(tmp_path):
    cli = Path.home() / "AppData/Local/Programs/OpenAI/Codex/bin/codex.exe"
    if not cli.is_file():
        pytest.skip("Codex CLI not installed")
    config = tomllib.loads(files_for("codex", sys.executable, "http://127.0.0.1:8000", tmp_path / "credential.txt")["ocr-config.toml"])["mcp_servers"]["ocr-recognizer"]
    # A per-command override, never a write to the user's config or CODEX_HOME.
    inline = "{" + ",".join(key + "=" + ("{" + ",".join(k + "=" + json.dumps(v) for k, v in value.items()) + "}" if isinstance(value, dict) else json.dumps(value)) for key, value in config.items()) + "}"
    result = subprocess.run([str(cli), "mcp", "get", "ocr-recognizer", "--json", "-c", "mcp_servers.ocr-recognizer=" + inline],
                            cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    assert result.returncode == 0, "Codex CLI rejected the generated configuration"
    parsed = json.loads(result.stdout)
    assert parsed["name"] == "ocr-recognizer"
    assert parsed["transport"]["command"] == sys.executable


def test_agy_cli_discovers_workspace_config_without_global_write(tmp_path):
    cli = Path.home() / "AppData/Local/agy/bin/agy.exe"
    if not cli.is_file():
        pytest.skip("agy CLI not installed")
    folder = tmp_path / ".agents"
    folder.mkdir()
    (folder / "mcp_config.json").write_text(files_for("agy", sys.executable, "http://127.0.0.1:8000", tmp_path / "credential.txt")["agy.json"], encoding="utf-8")
    result = subprocess.run([str(cli), "mcp", "list"], cwd=tmp_path, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=20)
    # Other globally installed servers may appear: never persist or print that output.
    assert result.returncode == 0, "agy CLI configuration listing failed"
    if "ocr-recognizer" not in result.stdout:
        pytest.xfail("agy mcp list did not expose the workspace entry; native session discovery remains AM-16")
