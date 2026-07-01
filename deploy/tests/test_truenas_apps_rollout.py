import importlib.util
import json
import sys
from argparse import Namespace
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "truenas-apps" / "scripts" / "rollout.py"

spec = importlib.util.spec_from_file_location("truenas_apps_rollout", SCRIPT)
rollout = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = rollout
spec.loader.exec_module(rollout)


class TrueNasAppsRolloutTests(TestCase):
    def test_default_public_url_uses_default_nodeport(self):
        self.assertEqual(rollout.DEFAULT_PUBLIC_URL, f"http://{rollout.DEFAULT_HOST}:{rollout.DEFAULT_WEB_PORT}")

    def test_release_image_reads_consolidated_ocr_config(self):
        release = {
            "config": {
                "ocr-recognizer": {
                    "image": {
                        "repository": "ghcr.io/andrevilas/microservices-ocr",
                        "tag": "sha-current",
                    }
                }
            }
        }

        self.assertEqual(
            rollout.release_image(release),
            {"repository": "ghcr.io/andrevilas/microservices-ocr", "tag": "sha-current"},
        )

    def test_release_image_reads_root_image_config(self):
        release = {
            "config": {
                "image": {
                    "repository": "ghcr.io/andrevilas/microservices-ocr",
                    "tag": "sha-current",
                }
            }
        }

        self.assertEqual(
            rollout.release_image(release),
            {"repository": "ghcr.io/andrevilas/microservices-ocr", "tag": "sha-current"},
        )

    def test_apply_stack_updates_only_ocr_image_values(self):
        original_config = {
            "ocr-recognizer": {
                "image": {
                    "repository": "ghcr.io/andrevilas/microservices-ocr",
                    "tag": "sha-old",
                    "pullPolicy": "IfNotPresent",
                },
                "secretKey": "must-stay",
            },
            "someOtherConfig": "preserved",
        }
        calls = []

        def fake_midclt(method, *args):
            calls.append((method, args))
            if method == "chart.release.query":
                return [{"name": "ocr-recognizer", "config": original_config}]
            if method == "chart.release.update":
                return 123
            raise AssertionError(method)

        args = Namespace(
            image_repository="ghcr.io/acme/ocr",
            image_pull_policy="IfNotPresent",
        )
        report = {}

        with patch.object(rollout, "midclt", side_effect=fake_midclt), patch.object(rollout, "wait_job") as wait_job:
            rollout.apply_stack(args, report, image_tag="sha-new", scope="app")

        update_call = calls[1]
        self.assertEqual(update_call[0], "chart.release.update")
        self.assertEqual(update_call[1][0], "ocr-recognizer")
        values = update_call[1][1]["values"]
        self.assertEqual(values["ocr-recognizer"]["image"]["repository"], "ghcr.io/acme/ocr")
        self.assertEqual(values["ocr-recognizer"]["image"]["tag"], "sha-new")
        self.assertEqual(values["ocr-recognizer"]["secretKey"], "must-stay")
        self.assertEqual(values["someOtherConfig"], "preserved")
        self.assertEqual(original_config["ocr-recognizer"]["image"]["tag"], "sha-old")
        wait_job.assert_called_once_with(123, report)

    def test_apply_stack_updates_root_image_values(self):
        original_config = {
            "image": {
                "repository": "ghcr.io/andrevilas/microservices-ocr",
                "tag": "sha-old",
                "pullPolicy": "IfNotPresent",
            },
            "env": {"JWT_SECRET": "preserved"},
        }
        calls = []

        def fake_midclt(method, *args):
            calls.append((method, args))
            if method == "chart.release.query":
                return [{"name": "ocr-recognizer", "config": original_config}]
            if method == "chart.release.update":
                return 123
            raise AssertionError(method)

        args = Namespace(image_repository="ghcr.io/acme/ocr", image_pull_policy="Always")
        report = {}

        with patch.object(rollout, "midclt", side_effect=fake_midclt), patch.object(rollout, "wait_job") as wait_job:
            rollout.apply_stack(args, report, image_tag="sha-new", scope="app")

        values = calls[1][1][1]["values"]
        self.assertEqual(values["image"]["repository"], "ghcr.io/acme/ocr")
        self.assertEqual(values["image"]["tag"], "sha-new")
        self.assertEqual(values["image"]["pullPolicy"], "Always")
        self.assertEqual(values["env"], {"JWT_SECRET": "preserved"})
        self.assertEqual(original_config["image"]["tag"], "sha-old")
        wait_job.assert_called_once_with(123, report)

    def test_apply_stack_rejects_infra_only_on_consolidated_app(self):
        args = Namespace(image_repository="repo", image_pull_policy="IfNotPresent")

        with self.assertRaises(rollout.RolloutError):
            rollout.apply_stack(args, {}, image_tag="sha-new", scope="infra")

    def test_deploy_rejects_latest_without_explicit_override(self):
        args = Namespace(image_tag="latest", allow_latest=False)

        with self.assertRaises(rollout.RolloutError):
            rollout.deploy(args)

    def test_validate_releases_accepts_single_active_release(self):
        report = {}
        release = {
            "name": "ocr-recognizer",
            "status": "ACTIVE",
            "pod_status": {"desired": 1, "available": 1},
            "config": {"image": {"repository": "repo", "tag": "sha"}},
        }

        with patch.object(rollout, "release_query", return_value=[release]):
            rollout.validate_releases(report)

        self.assertEqual(report["checks"][0]["name"], "release-active")

    def test_wait_http_status_retries_transient_curl_error(self):
        report = {}
        statuses = iter(["curl-error-7", "503", "200"])

        with patch.object(rollout, "http_status", side_effect=lambda *args, **kwargs: next(statuses)), patch.object(rollout.time, "sleep") as sleep:
            status = rollout.wait_http_status(
                report,
                "http-nodeport",
                "http://192.168.3.140:31800/health",
                expected={"200"},
                attempts=3,
                delay=0.1,
            )

        self.assertEqual(status, "200")
        self.assertEqual(report["checks"][0]["status"], "ok")
        self.assertIn("after 3 attempts", report["checks"][0]["detail"])
        self.assertEqual(sleep.call_count, 2)

    def test_wait_http_status_fails_after_retry_budget(self):
        report = {}

        with patch.object(rollout, "http_status", return_value="curl-error-7"), patch.object(rollout.time, "sleep"):
            with self.assertRaises(rollout.RolloutError):
                rollout.wait_http_status(report, "http-nodeport", "http://example.test", expected={"200"}, attempts=2, delay=0)

        self.assertEqual(report["checks"][0]["status"], "fail")

    def test_image_pull_secret_names_reads_root_and_nested_values(self):
        config = {
            "imagePullSecrets": [{"name": "ghcr-pull-secret"}, {"name": "already-seen"}],
            "ocr-recognizer": {"imagePullSecrets": [{"name": "already-seen"}, "secondary-secret"]},
        }

        self.assertEqual(
            rollout.image_pull_secret_names(config),
            ["ghcr-pull-secret", "already-seen", "secondary-secret"],
        )

    def test_validate_image_pull_uses_kubernetes_pod_when_secret_exists(self):
        report = {}
        commands = []

        def fake_log_command(report, label, args, **kwargs):
            commands.append((label, args, kwargs.get("input_text")))
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch.object(rollout, "release_config", return_value={"imagePullSecrets": [{"name": "ghcr-pull-secret"}]}), patch.object(
            rollout, "log_command", side_effect=fake_log_command
        ):
            rollout.validate_image_pull(report, "ghcr.io/acme/ocr:sha-test")

        labels = [label for label, _args, _input in commands]
        self.assertEqual(labels, ["image-pull-check-apply", "image-pull-check-ready", "image-pull-check-cleanup"])
        manifest = json.loads(commands[0][2])
        self.assertEqual(manifest["spec"]["imagePullSecrets"], [{"name": "ghcr-pull-secret"}])
        self.assertEqual(manifest["spec"]["containers"][0]["image"], "ghcr.io/acme/ocr:sha-test")
        self.assertEqual(report["checks"][0]["name"], "image-pull-check")

    def test_validate_image_pull_falls_back_to_ctr_without_secret(self):
        report = {}
        commands = []

        def fake_log_command(report, label, args, **kwargs):
            commands.append((label, args))
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch.object(rollout, "release_config", return_value={"ocr": {"image": {"repository": "repo", "tag": "sha"}}}), patch.object(
            rollout, "log_command", side_effect=fake_log_command
        ):
            rollout.validate_image_pull(report, "repo:sha")

        self.assertEqual(commands, [("image-pull-check", ["k3s", "ctr", "-n", "k8s.io", "images", "pull", "repo:sha"])])
        self.assertEqual(report["checks"][0]["status"], "ok")

    def test_deploy_runs_steps_in_correct_order(self):
        order = []
        args = Namespace(
            image_tag="sha-new",
            allow_latest=False,
            release_root="/tmp/releases",
            backup_root="/tmp/backups",
            image_repository="repo",
            image_pull_policy="IfNotPresent",
            only="app",
            skip_backup=False,
            confirm_skip_backup=False,
        )

        with patch.object(rollout.Path, "mkdir"), patch.object(rollout, "write_json"), patch.object(rollout, "write_report"), patch.object(
            rollout, "current_state", return_value={}
        ), patch.object(rollout, "preflight", side_effect=lambda *_: order.append("preflight")), patch.object(
            rollout, "create_backup", side_effect=lambda *_: order.append("backup")
        ), patch.object(
            rollout, "apply_stack", side_effect=lambda *_args, **_kwargs: order.append("apply")
        ), patch.object(rollout, "validate_stack", side_effect=lambda *_: order.append("validate")):
            report = rollout.deploy(args)

        self.assertEqual(order, ["preflight", "backup", "apply", "validate"])
        self.assertEqual(report["status"], "ok")
