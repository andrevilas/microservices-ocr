from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "truenas-apps" / "scripts" / "configure-npm-ocr-host.py"

spec = importlib.util.spec_from_file_location("configure_npm_ocr_host", SCRIPT)
npm = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = npm
spec.loader.exec_module(npm)


SCHEMA = """
create table proxy_host (
    id integer primary key autoincrement,
    created_on text,
    modified_on text,
    owner_user_id integer,
    is_deleted integer,
    domain_names text,
    forward_host text,
    forward_port integer,
    access_list_id integer,
    certificate_id integer,
    ssl_forced integer,
    caching_enabled integer,
    block_exploits integer,
    advanced_config text,
    meta text,
    allow_websocket_upgrade integer,
    http2_support integer,
    forward_scheme text,
    enabled integer,
    locations text,
    hsts_enabled integer,
    hsts_subdomains integer,
    trust_forwarded_proto integer
)
"""


class ConfigureNpmOcrHostTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = root / "database.sqlite"
        self.nginx_dir = root / "nginx" / "proxy_host"
        self.nginx_dir.mkdir(parents=True)
        with sqlite3.connect(self.db) as conn:
            conn.execute(SCHEMA)
        self.originals = {
            "DOMAIN": npm.DOMAIN,
            "FORWARD_HOST": npm.FORWARD_HOST,
            "FORWARD_PORT": npm.FORWARD_PORT,
            "DB": npm.DB,
            "NGINX_DIR": npm.NGINX_DIR,
        }
        npm.DOMAIN = "ocr.andre.goiania.br"
        npm.FORWARD_HOST = "192.168.3.140"
        npm.FORWARD_PORT = 31800
        npm.DB = self.db
        npm.NGINX_DIR = self.nginx_dir

    def tearDown(self) -> None:
        for name, value in self.originals.items():
            setattr(npm, name, value)

    def test_configure_db_inserts_proxy_host(self) -> None:
        host_id = npm.configure_db()

        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("select * from proxy_host where id=?", (host_id,)).fetchone()

        self.assertEqual(row["domain_names"], '["ocr.andre.goiania.br"]')
        self.assertEqual(row["forward_scheme"], "http")
        self.assertEqual(row["forward_host"], "192.168.3.140")
        self.assertEqual(row["forward_port"], 31800)
        self.assertEqual(row["block_exploits"], 1)
        self.assertEqual(row["allow_websocket_upgrade"], 1)
        self.assertEqual(row["enabled"], 1)

    def test_configure_db_updates_existing_proxy_host_with_spaced_json(self) -> None:
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                """
                insert into proxy_host
                    (id, is_deleted, domain_names, forward_host, forward_port, block_exploits,
                     allow_websocket_upgrade, enabled, forward_scheme)
                values
                    (42, 0, ?, 'old-host', 1234, 0, 0, 0, 'https')
                """,
                ('[ "ocr.andre.goiania.br" ]',),
            )

        host_id = npm.configure_db()

        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("select * from proxy_host where id=42").fetchone()
            count = conn.execute("select count(*) from proxy_host").fetchone()[0]

        self.assertEqual(host_id, 42)
        self.assertEqual(count, 1)
        self.assertEqual(row["forward_host"], "192.168.3.140")
        self.assertEqual(row["forward_port"], 31800)
        self.assertEqual(row["forward_scheme"], "http")
        self.assertEqual(row["enabled"], 1)

    def test_write_nginx_conf_points_to_nodeport(self) -> None:
        conf = npm.write_nginx_conf(8)

        content = conf.read_text(encoding="utf-8")
        self.assertIn("server_name ocr.andre.goiania.br;", content)
        self.assertIn('set $server         "192.168.3.140";', content)
        self.assertIn("set $port           31800;", content)
        self.assertIn("proxy-host-8_access.log", content)


if __name__ == "__main__":
    unittest.main()
