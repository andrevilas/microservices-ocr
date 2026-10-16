"""Generate reviewable host configuration; never read credentials or edit host settings."""
import argparse
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

CLIENTS = ("claude-desktop", "claude-code", "antigravity", "agy", "codex", "chatgpt-desktop")


def files_for(client, python, api_url, token_file):
    executable, credential = Path(python), Path(token_file)
    if not executable.is_absolute() or not executable.is_file() or not credential.is_absolute():
        raise ValueError("Use an existing absolute Python executable and an absolute credential-file path.")
    url = urlsplit(api_url)
    if (any(ord(c) < 33 for c in api_url) or url.port == 0
            or url.scheme not in ("http", "https") or not url.hostname or url.username or url.password
            or url.path not in ("", "/") or url.query or url.fragment
            or (url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"))):
        raise ValueError("API origin must be loopback HTTP or HTTPS, without credentials or a path.")
    if client not in CLIENTS:
        raise ValueError("Unsupported client.")
    launcher = Path(__file__).resolve().parents[1] / "scripts" / "ocr_mcp.py"
    server = {"command": str(executable), "args": [str(launcher)],
              "env": {"OCR_API_URL": api_url.rstrip("/"), "OCR_API_TOKEN": "", "OCR_API_TOKEN_FILE": str(credential), "PYTHONUTF8": "1"}}
    encode = lambda value: json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if client == "codex":
        lines = ["[mcp_servers.ocr-recognizer]", "command = " + json.dumps(server["command"]),
                 "args = " + json.dumps(server["args"]), "startup_timeout_sec = 30", "tool_timeout_sec = 120",
                 "[mcp_servers.ocr-recognizer.env]"]
        lines.extend(key + " = " + json.dumps(value) for key, value in server["env"].items())
        return {"ocr-config.toml": "\n".join(lines) + "\n"}
    if client == "chatgpt-desktop":
        manifest = {"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                    "name": "ocr-recognizer", "version": "0.1.0", "description": "Process caller-owned PDFs using the local OCR API.",
                    "extensions": {"com.openai": {"requires_local_executor": True,
                        "interface": {"displayName": "OCR Recognizer", "shortDescription": "PDFs, texto e resultados OCR", "capabilities": []}}}}
        return {"ocr-recognizer/plugin.json": encode(manifest),
                "ocr-recognizer/mcp.json": encode({"$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json", "mcpServers": {"ocr-recognizer": {"type": "stdio", **server}}})}
    return {client + ".json": encode({"mcpServers": {"ocr-recognizer": server}})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=CLIENTS, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        files = files_for(args.client, args.python, args.api_url, args.token_file)
        # New directory only: no implicit merge/overwrite of any client's settings.
        args.output.mkdir(parents=True, exist_ok=False)
        for name, content in files.items():
            target = args.output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    except (ValueError, OSError):
        parser.exit(2, "Configuration could not be generated. Check arguments and use a new output directory.\n")
    print("Configuration generated; review and install through the target client.")


if __name__ == "__main__":
    main()
