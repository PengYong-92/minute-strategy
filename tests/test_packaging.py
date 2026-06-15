import tarfile
import tempfile
import unittest
import zipfile
import sys
import os
from pathlib import Path
from subprocess import TimeoutExpired, run


ROOT = Path(__file__).resolve().parents[1]


class PackagingTest(unittest.TestCase):
    def test_dashboard_exposes_current_strategy_and_short_extension_status(self):
        index_html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
        app_js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        styles_css = (ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")

        self.assertIn("WD-02/WD-23 SHORT扩展", index_html)
        self.assertIn("SHORT扩展", index_html)
        self.assertIn("short-extension-status", index_html)
        self.assertIn("NORMAL_DOWN_SHORT_EXTENSION", app_js)
        self.assertIn("status-good", styles_css)
        self.assertIn("status-risk", styles_css)

    def test_dashboard_uses_single_analysis_card_and_server_side_order_filters(self):
        index_html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
        app_js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")

        self.assertNotIn('id="signals"', index_html)
        self.assertIn('id="order-filters"', index_html)
        self.assertIn('id="page-size-filter"', index_html)
        self.assertIn('id="direction-filter"', index_html)
        self.assertIn('id="level-filter"', index_html)
        self.assertIn('id="segment-filter"', index_html)
        self.assertIn('id="result-filter"', index_html)
        self.assertIn('id="pagination"', index_html)
        self.assertIn("/api/orders", app_js)
        self.assertIn("page_size", app_js)
        self.assertIn("loadOrders", app_js)
        self.assertNotIn("function renderSignals", app_js)

    def test_run_script_exposes_help_without_starting_monitor(self):
        try:
            result = run(
                ["bash", str(ROOT / "scripts" / "run.sh"), "--help"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )
        except TimeoutExpired as exc:
            self.fail(f"run.sh --help should exit instead of starting the monitor: {exc}")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("--symbol", result.stdout)
        self.assertIn("--port", result.stdout)
        self.assertIn("--stake", result.stdout)
        self.assertIn("--win-return", result.stdout)
        self.assertIn("--no-stake-progression", result.stdout)
        self.assertIn("--stake-progression-max-orders", result.stdout)

    def test_run_script_handles_empty_extra_args_on_macos_bash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            log_path = temp_path / "fake-python-args.txt"
            fake_python = temp_path / "python3"
            fake_python.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env bash",
                        "set -euo pipefail",
                        'if [ "${1:-}" = "-" ]; then',
                        "  cat >/dev/null",
                        "  exit 0",
                        "fi",
                        'printf "%s\\n" "$@" > "$FAKE_PYTHON_LOG"',
                    ]
                ),
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "PYTHON_BIN": str(fake_python),
                    "FAKE_PYTHON_LOG": str(log_path),
                    "HOST": "0.0.0.0",
                    "PORT": "8001",
                    "NO_WARMUP": "0",
                    "NO_PERSISTENCE": "0",
                    "NO_WEBHOOK": "0",
                    "WARMUP_CURRENT_MONTH_DAILY": "1",
                    "STAKE": "20",
                    "WIN_RETURN": "36",
                    "STAKE_PROGRESSION": "0",
                    "STAKE_PROGRESSION_MAX_ORDERS": "5",
                }
            )

            result = run(
                ["bash", str(ROOT / "scripts" / "run.sh")],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )
            args = log_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn("unbound variable", result.stderr)
        self.assertEqual(args[:3], ["-m", "app.server", "--symbol"])
        self.assertNotIn("--no-warmup", args)
        self.assertNotIn("--no-persistence", args)
        self.assertNotIn("--no-webhook", args)
        self.assertIn("--stake", args)
        self.assertEqual(args[args.index("--stake") + 1], "20")
        self.assertIn("--win-return", args)
        self.assertEqual(args[args.index("--win-return") + 1], "36")
        self.assertIn("--no-stake-progression", args)
        self.assertIn("--stake-progression-max-orders", args)
        self.assertEqual(args[args.index("--stake-progression-max-orders") + 1], "5")

    def test_package_script_creates_portable_archives_with_runtime_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run(
                ["bash", str(ROOT / "scripts" / "package.sh"), "--output-dir", temp_dir],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            archives = sorted(Path(temp_dir).glob("event-contract-monitor-*"))
            tarballs = [path for path in archives if path.suffixes[-2:] == [".tar", ".gz"]]
            zipballs = [path for path in archives if path.suffix == ".zip"]
            self.assertEqual(len(tarballs), 1)
            self.assertEqual(len(zipballs), 1)

            with tarfile.open(tarballs[0], "r:gz") as archive:
                tar_names = archive.getnames()
            with zipfile.ZipFile(zipballs[0]) as archive:
                zip_names = archive.namelist()

            for names in (tar_names, zip_names):
                self.assertTrue(any(name.endswith("/app/server.py") for name in names))
                self.assertTrue(any(name.endswith("/app/history.py") for name in names))
                self.assertTrue(any(name.endswith("/app/storage.py") for name in names))
                self.assertTrue(any(name.endswith("/app/session_profiles.py") for name in names))
                self.assertTrue(any(name.endswith("/app/webhook.py") for name in names))
                self.assertTrue(any(name.endswith("/app/static/index.html") for name in names))
                self.assertTrue(any(name.endswith("/scripts/run.sh") for name in names))
                self.assertTrue(any(name.endswith("/README.md") for name in names))
                self.assertFalse(any("/.venv/" in name for name in names))
                self.assertFalse(any("/data/" in name for name in names))
                self.assertFalse(any("/__pycache__/" in name for name in names))

            with tempfile.TemporaryDirectory() as extract_dir:
                with tarfile.open(tarballs[0], "r:gz") as archive:
                    archive.extractall(extract_dir)
                package_root = next(Path(extract_dir).iterdir())
                help_result = run(
                    [sys.executable, "-m", "app.server", "--help"],
                    cwd=package_root,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(help_result.returncode, 0, help_result.stderr + help_result.stdout)
                self.assertIn("--symbol", help_result.stdout)
                self.assertIn("--stake", help_result.stdout)
                self.assertIn("--stake-progression-max-orders", help_result.stdout)


if __name__ == "__main__":
    unittest.main()
