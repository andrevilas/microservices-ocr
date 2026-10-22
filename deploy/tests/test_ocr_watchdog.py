import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "truenas-apps" / "scripts" / "ocr-watchdog.py"

spec = importlib.util.spec_from_file_location("ocr_watchdog", SCRIPT)
watchdog = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = watchdog
spec.loader.exec_module(watchdog)

NOW = 1_800_000_000.0


def iso(seconds_ago):
    return (datetime.fromtimestamp(NOW, timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def database():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE jobs (job_id TEXT, status TEXT, updated_at TEXT)")
    conn.execute("CREATE TABLE extraction_runs (run_id TEXT, state TEXT, outcome TEXT, created_at TEXT, result TEXT)")
    conn.execute("CREATE TABLE rag_outbox (id TEXT, state TEXT, created REAL, payload BLOB)")
    return conn


class ChecksTest(unittest.TestCase):
    def test_pods(self):
        found = watchdog.check_pods({"ocr": {"ready": True, "restarts": 2}, "rag": {"ready": False, "restarts": 0},
                                     "gone": None}, {"pod:ocr": {"restarts": 1}})
        self.assertEqual({f["check"]: f["alert"] for f in found},
                         {"pod:gone": "pod_missing", "pod:ocr": "pod_restarted", "pod:rag": "pod_not_ready"})
        steady = watchdog.check_pods({"ocr": {"ready": True, "restarts": 2}}, {"pod:ocr": {"restarts": 2}})
        self.assertIsNone(steady[0]["alert"])

    def test_sync(self):
        self.assertEqual(watchdog.check_sync([], NOW, 26)["alert"], "sync_never_ran")
        self.assertIsNone(watchdog.check_sync([(NOW - 3600, {"status": "unchanged"})], NOW, 26)["alert"])
        self.assertEqual(watchdog.check_sync([(NOW - 30 * 3600, {"status": "imported"})], NOW, 26)["alert"], "sync_stale")
        self.assertEqual(watchdog.check_sync([(NOW - 60, {"status": "attention"})], NOW, 26)["alert"], "sync_attention")
        self.assertEqual(watchdog.check_sync([(NOW - 60, {"status": "failed"})], NOW, 26)["alert"], "sync_failed")

    def test_llm(self):
        self.assertIsNone(watchdog.check_llm(lambda: 200)["alert"])
        self.assertEqual(watchdog.check_llm(lambda: 0)["alert"], "llm_unreachable")

    def test_extraction_failures_need_volume_and_majority(self):
        conn = database()
        for n, outcome in enumerate(["provider_error", "run_timeout", "invalid_output"]):
            conn.execute("INSERT INTO extraction_runs VALUES (?, 'failed', ?, ?, NULL)", (str(n), outcome, iso(60)))
        conn.execute("INSERT INTO extraction_runs VALUES ('old', 'failed', 'provider_error', ?, NULL)", (iso(2 * 86400),))
        runs = {f["check"]: f for f in watchdog.check_database(conn, NOW)}["extraction_runs_24h"]
        self.assertEqual((runs["alert"], runs["runs"], runs["failed"]), ("extraction_failing", 3, 3))
        for n in range(4):
            conn.execute("INSERT INTO extraction_runs VALUES (?, 'completed', 'completed', ?, NULL)", (f"ok{n}", iso(60)))
        self.assertIsNone({f["check"]: f for f in watchdog.check_database(conn, NOW)}["extraction_runs_24h"]["alert"])

    def test_outbox_and_stuck_jobs(self):
        conn = database()
        conn.execute("INSERT INTO rag_outbox VALUES ('a', 'retry', ?, NULL)", (NOW - 7200,))
        conn.execute("INSERT INTO jobs VALUES ('j', 'processing', ?)", (iso(3600),))
        found = {f["check"]: f["alert"] for f in watchdog.check_database(conn, NOW)}
        self.assertEqual((found["rag_outbox"], found["jobs_processing"]), ("rag_delivery_stuck", "jobs_stuck"))
        conn.execute("INSERT INTO rag_outbox VALUES ('b', 'resolution', ?, NULL)", (NOW,))
        self.assertEqual({f["check"]: f["alert"] for f in watchdog.check_database(conn, NOW)}["rag_outbox"],
                         "rag_delivery_resolution")

    def test_capacity(self):
        found = watchdog.check_capacity(5, 200, 10, 300)
        self.assertEqual([f["alert"] for f in found], ["disk_low", "memory_low"])

    def test_transitions(self):
        previous = {"disk": {"alert": None}, "memory": {"alert": "memory_low"}}
        events = watchdog.transitions(previous, [{"check": "disk", "alert": "disk_low"},
                                                 {"check": "memory", "alert": None},
                                                 {"check": "new", "alert": None}], "t")
        self.assertEqual([(e["event"], e["check"], e["alert"]) for e in events],
                         [("raised", "disk", "disk_low"), ("cleared", "memory", "memory_low")])


class MainTest(unittest.TestCase):
    def test_run_records_status_transitions_and_notifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            db = tmp / "jobs.sqlite3"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE jobs (job_id TEXT, status TEXT, updated_at TEXT)")
            conn.commit(); conn.close()
            sync = tmp / "sync"; sync.mkdir()
            (sync / "report-1.json").write_text(json.dumps({"status": "unchanged", "content_sha256": "x"}))
            notified = tmp / "notified.json"
            args = ["--state-dir", str(tmp / "monitor"), "--ocr-db", str(db), "--sync-dir", str(sync),
                    "--min-disk-gb", "0", "--min-mem-mb", "0",
                    "--notify-command", f'{sys.executable} -c "import sys; open(r\'{notified}\', \'w\').write(sys.stdin.read())"']
            pods = {"ix-ocr-recognizer": {"ready": True, "restarts": 0}, "document-rag": {"ready": True, "restarts": 0}}
            with patch.object(watchdog, "_pods", return_value=pods), \
                    patch.object(watchdog, "_http_status", return_value=lambda: 0),                     patch.object(watchdog, "_mem_available_mb", return_value=4096):
                self.assertEqual(watchdog.main(args), 1)
                self.assertEqual(watchdog.main(args), 1)
            status = json.loads((tmp / "monitor" / "status.json").read_text())
            self.assertEqual(status["active"], ["llm_unreachable"])
            lines = (tmp / "monitor" / "alerts.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 1, "a persisting alert is recorded once")
            self.assertEqual(json.loads(notified.read_text())[0]["alert"], "llm_unreachable")
            self.assertNotIn("content_sha256", (tmp / "monitor" / "status.json").read_text())
            with patch.object(watchdog, "_pods", return_value=pods), \
                    patch.object(watchdog, "_http_status", return_value=lambda: 200),                     patch.object(watchdog, "_mem_available_mb", return_value=4096):
                self.assertEqual(watchdog.main(args), 0)
            self.assertEqual(json.loads((tmp / "monitor" / "alerts.jsonl").read_text().splitlines()[-1])["event"], "cleared")


if __name__ == "__main__":
    unittest.main()
