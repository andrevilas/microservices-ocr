#!/usr/bin/env python3
"""Configure the Nginx Proxy Manager host for ocr.andre.goiania.br."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path


DOMAIN = os.environ.get("OCR_PUBLIC_DOMAIN", "ocr.andre.goiania.br")
FORWARD_HOST = os.environ.get("OCR_NPM_FORWARD_HOST", "192.168.3.140")
FORWARD_PORT = int(os.environ.get("OCR_NPM_FORWARD_PORT", "31800"))
NPM_NAMESPACE = os.environ.get("OCR_NPM_NAMESPACE", "ix-nginxproxymanager")
NPM_INSTANCE = os.environ.get("OCR_NPM_INSTANCE", "nginxproxymanager")
DB = Path(os.environ.get("OCR_NPM_DB", "/mnt/NVME/docker/volumes/nginx-proxy-manager/data/database.sqlite"))
NGINX_DIR = Path(os.environ.get("OCR_NPM_PROXY_HOST_DIR", "/mnt/NVME/docker/volumes/nginx-proxy-manager/data/nginx/proxy_host"))


def run(args: list[str]) -> str:
    return subprocess.check_output(args, text=True).strip()


def backup_db() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DB.with_name(f"{DB.name}.bak-ocr-subdomain-{stamp}")
    shutil.copy2(DB, backup)
    return backup


def configure_db() -> int:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    domain_names = json.dumps([DOMAIN], separators=(",", ":"))
    existing = None
    for row in conn.execute("select id, domain_names from proxy_host where is_deleted=0"):
        try:
            domains = json.loads(row["domain_names"] or "[]")
        except json.JSONDecodeError:
            domains = []
        if DOMAIN in domains:
            existing = row
            break
    advanced = 'if ($http_x_forwarded_proto = "http") { return 301 https://$host$request_uri; }\n'
    meta = '{"nginx_online":true,"nginx_err":null}'
    if existing:
        host_id = int(existing["id"])
        conn.execute(
            """
            update proxy_host
               set modified_on=datetime('now'),
                   forward_scheme='http',
                   forward_host=?,
                   forward_port=?,
                   block_exploits=1,
                   allow_websocket_upgrade=1,
                   enabled=1,
                   advanced_config=?,
                   meta=?
             where id=?
            """,
            (FORWARD_HOST, FORWARD_PORT, advanced, meta, host_id),
        )
    else:
        cursor = conn.execute(
            """
            insert into proxy_host
                (created_on, modified_on, owner_user_id, is_deleted, domain_names,
                 forward_host, forward_port, access_list_id, certificate_id, ssl_forced,
                 caching_enabled, block_exploits, advanced_config, meta,
                 allow_websocket_upgrade, http2_support, forward_scheme, enabled,
                 locations, hsts_enabled, hsts_subdomains, trust_forwarded_proto)
            values
                (datetime('now'), datetime('now'), 1, 0, ?, ?, ?, 0, 0, 0,
                 0, 1, ?, ?, 1, 0, 'http', 1, '[]', 0, 0, 0)
            """,
            (domain_names, FORWARD_HOST, FORWARD_PORT, advanced, meta),
        )
        host_id = int(cursor.lastrowid)
    conn.commit()
    conn.close()
    return host_id


def write_nginx_conf(host_id: int) -> Path:
    conf = NGINX_DIR / f"{host_id}.conf"
    conf.write_text(
        f"""# ------------------------------------------------------------
# {DOMAIN}
# ------------------------------------------------------------

map $scheme $hsts_header {{
    https   "max-age=63072000; preload";
}}

server {{
  set $forward_scheme http;
  set $server         "{FORWARD_HOST}";
  set $port           {FORWARD_PORT};

  listen 80;
listen [::]:80;


  server_name {DOMAIN};
http2 off;

  # Block Exploits
  include conf.d/include/block-exploits.conf;

proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection $http_connection;
proxy_http_version 1.1;

  access_log /data/logs/proxy-host-{host_id}_access.log proxy;
  error_log /data/logs/proxy-host-{host_id}_error.log warn;

if ($http_x_forwarded_proto = "http") {{ return 301 https://$host$request_uri; }}

  location / {{
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $http_connection;
    proxy_http_version 1.1;

    # Proxy!
    include conf.d/include/proxy.conf;
  }}

  # Custom
  include /data/nginx/custom/server_proxy[.]conf;
}}
""",
        encoding="utf-8",
    )
    return conf


def reload_nginx() -> None:
    pod = run(
        [
            "k3s",
            "kubectl",
            "-n",
            NPM_NAMESPACE,
            "get",
            "pod",
            "-l",
            f"app.kubernetes.io/instance={NPM_INSTANCE}",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ]
    )
    run(["k3s", "kubectl", "-n", NPM_NAMESPACE, "exec", pod, "--", "nginx", "-t"])
    run(["k3s", "kubectl", "-n", NPM_NAMESPACE, "exec", pod, "--", "nginx", "-s", "reload"])


def main() -> None:
    backup = backup_db()
    host_id = configure_db()
    conf = write_nginx_conf(host_id)
    reload_nginx()
    print(f"backup={backup}")
    print(f"proxy_host_id={host_id}")
    print(f"conf={conf}")


if __name__ == "__main__":
    main()
