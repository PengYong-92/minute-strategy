import tarfile
import tempfile
import unittest
import zipfile
import json
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

        self.assertIn("每日画像选策", index_html)
        self.assertIn("SHORT扩展", index_html)
        self.assertIn("short-extension-status", index_html)
        self.assertIn("strategy-profile", index_html)
        self.assertIn('id="stake-progression-badge"', index_html)
        self.assertIn("未结${amount}订单", app_js)
        self.assertIn("状态数据不完整", app_js)
        self.assertIn("观察信号", index_html)
        self.assertIn("观察画像统计", index_html)
        self.assertIn("订单弱点画像", index_html)
        self.assertIn("current-risk-profile", index_html)
        self.assertIn("profile-guard-shadow", index_html)
        self.assertIn("daily-profile-status", index_html)
        self.assertIn("result-sequence-guard-status", index_html)
        self.assertIn("wave-state-status", index_html)
        self.assertIn("wave-batch-guard-status", index_html)
        self.assertIn("profile-degradation-guard-status", index_html)
        self.assertIn("fmtWaveState", app_js)
        self.assertIn("fmtWaveBatchGuard", app_js)
        self.assertIn("fmtProfileDegradationGuard", app_js)
        self.assertIn("profile_degradation_probe", app_js)
        self.assertIn("wave_guard_status", app_js)
        self.assertIn("wave_guard_reason", app_js)
        self.assertIn("daily-profile-list", index_html)
        self.assertIn(".layout > *", styles_css)
        self.assertIn("max-width: 100%", styles_css)
        self.assertIn("profile-guard-summary", index_html)
        self.assertIn("profile-guard-shadow-summary", index_html)
        self.assertIn("profile-guard-policy-summary", index_html)
        self.assertIn("profile-guard-compare-summary", index_html)
        self.assertIn("NORMAL_DOWN_SHORT_EXTENSION", app_js)
        self.assertIn("SAMPLE_WEAK_HIGH_RSI_REBOUND", app_js)
        self.assertIn("fmtReplayGuard", app_js)
        self.assertIn("fmtProfileGuardShadow", app_js)
        self.assertIn("renderProfileGuardShadowSummary", app_js)
        self.assertIn("renderProfileGuardPolicySummary", app_js)
        self.assertIn("renderProfileGuardCompareSummary", app_js)
        self.assertIn("renderDailyProfileSelection", app_js)
        self.assertIn("fmtResultSequenceGuard", app_js)
        self.assertIn("selection_state", app_js)
        self.assertIn('active.length ? "ACTIVE" : item.selection_state', app_js)
        self.assertIn('PROMOTE_WATCH: "重点观察"', app_js)
        self.assertIn("守卫对照", app_js)
        self.assertIn("仅默认会拦", app_js)
        self.assertIn("对照升级建议", app_js)
        self.assertIn("PROMOTE_RECOMMENDED_GUARD", app_js)
        self.assertIn("策略版本表现", app_js)
        self.assertIn("开单阈值", index_html)
        self.assertIn("calculated_threshold", app_js)
        self.assertIn("选中key", app_js)
        self.assertIn("READY_TO_BLOCK", app_js)
        self.assertIn("升级建议", app_js)
        self.assertIn("回放升级建议", app_js)
        self.assertIn("回放拦截贡献", app_js)
        self.assertIn("blocked_key_contribution", app_js)
        self.assertIn("候选子集", app_js)
        self.assertIn("稳定性", app_js)
        self.assertIn("稳定带", app_js)
        self.assertIn("recommended_key_subset", app_js)
        self.assertIn("recommended_walk_forward", app_js)
        self.assertIn("state.profile_guard", app_js)
        self.assertIn("/api/observations", app_js)
        self.assertIn("/api/observation-summary", app_js)
        self.assertIn("/api/order-profile", app_js)
        self.assertIn("status-good", styles_css)
        self.assertIn("status-risk", styles_css)
        self.assertIn("profile-guard-good", styles_css)

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

    def test_dashboard_formats_two_stage_runtime_states(self):
        script = """
const fs = require("fs");
const elements = new Map();
global.document = {
  getElementById(id) {
    if (!elements.has(id)) {
      elements.set(id, {
        addEventListener() {},
        className: "",
        disabled: false,
        innerHTML: "",
        textContent: "",
        value: "20",
      });
    }
    return elements.get(id);
  },
};
global.fetch = () => new Promise(() => {});
global.setInterval = () => 0;
const source = fs.readFileSync(process.argv[1], "utf8");
eval(source + `\nprocess.stdout.write(JSON.stringify([
  fmtStakeProgression({enabled: true, second_stake: 18, active_second_orders: 0, max_active: 1, pending_credits: 1}),
  fmtStakeProgression({enabled: false, second_stake: 18, active_second_orders: 1}),
  fmtStakeProgression({enabled: false, second_stake: 18, active_second_orders: 0}),
  fmtStakeProgression({enabled: false}),
  fmtStakeProgression({enabled: true, active_second_orders: 0, max_active: 1, pending_credits: 0}),
]));`);
"""
        result = run(
            ["node", "-e", script, str(ROOT / "app" / "static" / "app.js")],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            [
                "两单叠加 · 18U订单 0/1 · 待用资格 1",
                "两单叠加 OFF · 未结18U订单 1",
                "两单叠加 OFF",
                "两单叠加 · 状态数据不完整",
                "两单叠加 · 状态数据不完整",
            ],
        )

    def test_dashboard_formats_profile_degradation_guard_and_marks_probe_orders(self):
        script = """
const fs = require("fs");
const elements = new Map();
global.document = {
  getElementById(id) {
    if (!elements.has(id)) {
      elements.set(id, {
        addEventListener() {},
        className: "",
        disabled: false,
        innerHTML: "",
        textContent: "",
        value: "20",
      });
    }
    return elements.get(id);
  },
};
global.fetch = () => new Promise(() => {});
global.setInterval = () => 0;
const source = fs.readFileSync(process.argv[1], "utf8");
eval(source + `\n
const guards = [
  null,
  {enabled: false, status: "NORMAL"},
  {enabled: true, status: "DISABLED"},
  {enabled: true, status: "NORMAL"},
  {enabled: true, status: "NOT_APPLICABLE"},
  {enabled: true, status: "COOLDOWN", profile_key: "WD-02", consecutive_losses: 3},
  {enabled: true, status: "COOLDOWN", profile_key: "", consecutive_losses: 2},
  {enabled: true, status: "RECOVERY_READY", profile_key: "WD-23"},
  {enabled: true, status: "RECOVERY_READY", profile_key: ""},
  {enabled: true, status: "RECOVERY_PENDING", probe_order_id: 42},
];

function makeOrder(id, probe) {
  return {
    id,
    direction: "LONG",
    timeframe_minutes: 10,
    level: "A",
    threshold_segment: "WD-02",
    strategy_tag: probe ? "probe-tag" : "regular-tag",
    strategy_family: "base-family",
    wave_state: "NORMAL",
    wave_guard_status: "PASS",
    wave_guard_mode: "NORMAL",
    session_win_rate: 0.5,
    session_ev: 0.2,
    threshold: 10,
    score: 11,
    calculated_threshold: 10,
    stake: 10,
    entry_price: 100,
    opened_at: 1,
    exit_price: 101,
    settled_at: 2,
    status: "SETTLED",
    result: "WIN",
    pnl: 8,
    reason: "ok",
    regime: "NORMAL",
    profile_degradation_probe: probe,
  };
}

renderOrders([makeOrder(101, true), makeOrder(102, false)]);
const rows = elements.get("orders").innerHTML.split("</tr>");
renderOrders([makeOrder(103, false)]);
const regularOnlyHtml = elements.get("orders").innerHTML;
const probeLabel = '<span class="order-probe-label">基础试探</span>';

process.stdout.write(JSON.stringify({
  guards: guards.map((guard) => [
    fmtProfileDegradationGuard(guard),
    profileDegradationGuardClass(guard),
  ]),
  probeRowLabelCount: rows[0].split(probeLabel).length - 1,
  regularRowHasLabel: rows[1].includes(probeLabel),
  regularOnlyHasLabel: regularOnlyHtml.includes(probeLabel),
}));`);
"""
        result = run(
            ["node", "-e", script, str(ROOT / "app" / "static" / "app.js")],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "guards": [
                    ["-", "status-muted"],
                    ["关闭", "status-muted"],
                    ["关闭", "status-muted"],
                    ["正常", "status-good"],
                    ["正常", "status-good"],
                    ["冷却 · WD-02 · 连亏3", "status-risk"],
                    ["冷却 · - · 连亏2", "status-risk"],
                    ["待试探 · WD-23", "status-warn"],
                    ["待试探 · -", "status-warn"],
                    ["试探待结算 · 订单#42", "status-warn"],
                ],
                "probeRowLabelCount": 1,
                "regularRowHasLabel": False,
                "regularOnlyHasLabel": False,
            },
        )

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
        self.assertIn("--no-rolling-edge-guard", result.stdout)
        self.assertIn("--no-result-sequence-guard", result.stdout)
        self.assertIn("--result-sequence-loss-streak", result.stdout)
        self.assertIn("--result-sequence-cooldown-minutes", result.stdout)
        self.assertIn("--result-sequence-scope", result.stdout)
        self.assertIn("同方向连续已结算亏损", result.stdout)
        self.assertIn("--stake-progression-max-orders", result.stdout)
        self.assertIn("--stake-progression-max-active", result.stdout)
        self.assertIn("--stake-progression-base-only-segments", result.stdout)
        self.assertIn("两阶段固定为 2 级", result.stdout)
        self.assertIn("最多并行第二级订单数", result.stdout)
        self.assertIn("所有已入选时段均可参与", result.stdout)
        self.assertIn("STAKE_PROGRESSION_MAX_ACTIVE", result.stdout)
        self.assertIn("--profile-guard", result.stdout)
        self.assertIn("--profile-guard-min-history", result.stdout)
        self.assertIn("--profile-guard-min-group-size", result.stdout)
        self.assertIn("--no-observation-profile-promotion", result.stdout)
        self.assertIn("--observation-profile-lookback-days", result.stdout)
        self.assertIn("--observation-profile-min-samples", result.stdout)
        self.assertIn("--observation-profile-min-win-rate", result.stdout)
        self.assertIn("--observation-profile-min-ev", result.stdout)
        self.assertIn("--observation-profile-min-edge", result.stdout)
        self.assertIn("--live-short-segments", result.stdout)
        self.assertIn("--no-daily-profile-selector", result.stdout)
        self.assertIn("--daily-profile-lookback-days", result.stdout)
        self.assertIn("--daily-profile-min-samples", result.stdout)
        self.assertIn("--daily-profile-weekend-min-samples", result.stdout)
        self.assertIn("--daily-profile-min-win-rate", result.stdout)
        self.assertIn("--daily-profile-min-ev", result.stdout)
        self.assertIn("--daily-profile-exit-win-rate", result.stdout)
        self.assertIn("--daily-profile-exit-ev", result.stdout)
        self.assertIn("--daily-profile-degraded-runs", result.stdout)
        self.assertIn("--daily-profile-max-active", result.stdout)
        self.assertIn("--daily-profile-evaluation-time", result.stdout)
        self.assertIn("--daily-profile-activation-time", result.stdout)
        self.assertIn("--profile-degradation-cooldown-minutes", result.stdout)
        self.assertIn(
            "完整画像连续亏损3单后的冷却分钟数，0关闭，默认: 60",
            result.stdout,
        )
        self.assertIn("每天北京时间", result.stdout)
        self.assertIn("观察画像", result.stdout)

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
                    "MAX_OPEN_ORDERS": "4",
                    "MIN_ORDER_GAP_MINUTES": "3",
                    "STAKE_PROGRESSION": "0",
                    "ROLLING_EDGE_GUARD": "0",
                    "RESULT_SEQUENCE_GUARD": "0",
                    "RESULT_SEQUENCE_LOSS_STREAK": "4",
                    "RESULT_SEQUENCE_COOLDOWN_MINUTES": "30",
                    "RESULT_SEQUENCE_SCOPE": "GLOBAL",
                    "STAKE_PROGRESSION_MAX_ORDERS": "5",
                    "STAKE_PROGRESSION_MAX_ACTIVE": "3",
                    "STAKE_PROGRESSION_BASE_ONLY_SEGMENTS": "WD-08,WD-12",
                    "PROFILE_GUARD": "1",
                    "PROFILE_GUARD_MIN_HISTORY": "15",
                    "PROFILE_GUARD_MIN_GROUP_SIZE": "2",
                    "OBSERVATION_PROFILE_PROMOTION": "0",
                    "OBSERVATION_PROFILE_LOOKBACK_DAYS": "9",
                    "OBSERVATION_PROFILE_MIN_SAMPLES": "11",
                    "OBSERVATION_PROFILE_MIN_WIN_RATE": "0.72",
                    "OBSERVATION_PROFILE_MIN_EV": "4",
                    "OBSERVATION_PROFILE_MIN_EDGE": "9",
                    "LIVE_SHORT_SEGMENTS": "WD-23",
                    "DAILY_PROFILE_SELECTOR": "0",
                    "DAILY_PROFILE_LOOKBACK_DAYS": "8",
                    "DAILY_PROFILE_MIN_SAMPLES": "25",
                    "DAILY_PROFILE_WEEKEND_MIN_SAMPLES": "9",
                    "DAILY_PROFILE_MIN_WIN_RATE": "0.61",
                    "DAILY_PROFILE_MIN_EV": "1.2",
                    "DAILY_PROFILE_EXIT_WIN_RATE": "0.57",
                    "DAILY_PROFILE_EXIT_EV": "0.1",
                    "DAILY_PROFILE_DEGRADED_RUNS": "3",
                    "DAILY_PROFILE_MAX_ACTIVE": "2",
                    "DAILY_PROFILE_EVALUATION_TIME": "07:45",
                    "DAILY_PROFILE_ACTIVATION_TIME": "08:05",
                    "PROFILE_DEGRADATION_COOLDOWN_MINUTES": "75",
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
        self.assertEqual(args[args.index("--max-open-orders") + 1], "4")
        self.assertEqual(args[args.index("--min-order-gap-minutes") + 1], "3")
        self.assertIn("--no-stake-progression", args)
        self.assertIn("--no-rolling-edge-guard", args)
        self.assertIn("--no-result-sequence-guard", args)
        self.assertEqual(args[args.index("--result-sequence-loss-streak") + 1], "4")
        self.assertEqual(args[args.index("--result-sequence-cooldown-minutes") + 1], "30")
        self.assertEqual(args[args.index("--result-sequence-scope") + 1], "GLOBAL")
        self.assertIn("--stake-progression-max-orders", args)
        self.assertEqual(args[args.index("--stake-progression-max-orders") + 1], "5")
        self.assertIn("--stake-progression-max-active", args)
        self.assertEqual(args[args.index("--stake-progression-max-active") + 1], "3")
        self.assertIn("--stake-progression-base-only-segments", args)
        self.assertEqual(args[args.index("--stake-progression-base-only-segments") + 1], "WD-08,WD-12")
        self.assertIn("--profile-guard", args)
        self.assertIn("--profile-guard-min-history", args)
        self.assertEqual(args[args.index("--profile-guard-min-history") + 1], "15")
        self.assertIn("--profile-guard-min-group-size", args)
        self.assertEqual(args[args.index("--profile-guard-min-group-size") + 1], "2")
        self.assertIn("--no-observation-profile-promotion", args)
        self.assertEqual(args[args.index("--observation-profile-lookback-days") + 1], "9")
        self.assertEqual(args[args.index("--observation-profile-min-samples") + 1], "11")
        self.assertEqual(args[args.index("--observation-profile-min-win-rate") + 1], "0.72")
        self.assertEqual(args[args.index("--observation-profile-min-ev") + 1], "4")
        self.assertEqual(args[args.index("--observation-profile-min-edge") + 1], "9")
        self.assertEqual(args[args.index("--live-short-segments") + 1], "WD-23")
        self.assertIn("--no-daily-profile-selector", args)
        self.assertEqual(args[args.index("--daily-profile-lookback-days") + 1], "8")
        self.assertEqual(args[args.index("--daily-profile-min-samples") + 1], "25")
        self.assertEqual(args[args.index("--daily-profile-weekend-min-samples") + 1], "9")
        self.assertEqual(args[args.index("--daily-profile-min-win-rate") + 1], "0.61")
        self.assertEqual(args[args.index("--daily-profile-min-ev") + 1], "1.2")
        self.assertEqual(args[args.index("--daily-profile-exit-win-rate") + 1], "0.57")
        self.assertEqual(args[args.index("--daily-profile-exit-ev") + 1], "0.1")
        self.assertEqual(args[args.index("--daily-profile-degraded-runs") + 1], "3")
        self.assertEqual(args[args.index("--daily-profile-max-active") + 1], "2")
        self.assertEqual(args[args.index("--daily-profile-evaluation-time") + 1], "07:45")
        self.assertEqual(args[args.index("--daily-profile-activation-time") + 1], "08:05")
        self.assertEqual(args.count("--profile-degradation-cooldown-minutes"), 1)
        self.assertEqual(
            args[args.index("--profile-degradation-cooldown-minutes") + 1],
            "75",
        )

    def test_run_script_forwards_profile_degradation_cooldown_default_and_cli_forms(self):
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
            env.pop("PROFILE_DEGRADATION_COOLDOWN_MINUTES", None)
            env.update(
                {
                    "PYTHON_BIN": str(fake_python),
                    "FAKE_PYTHON_LOG": str(log_path),
                }
            )

            cases = (
                ([], "60"),
                (["--profile-degradation-cooldown-minutes", "30"], "30"),
                (["--profile-degradation-cooldown-minutes=45"], "45"),
            )
            captured = []
            for cli_args, expected in cases:
                result = run(
                    ["bash", str(ROOT / "scripts" / "run.sh"), *cli_args],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=5,
                )
                args = log_path.read_text(encoding="utf-8").splitlines()
                captured.append((result, args, expected))

        for result, args, expected in captured:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(args.count("--profile-degradation-cooldown-minutes"), 1)
            self.assertEqual(
                args[args.index("--profile-degradation-cooldown-minutes") + 1],
                expected,
            )

    def test_run_script_forwards_two_stage_defaults_and_accepts_empty_base_only_cli(self):
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
            for name in (
                "MAX_OPEN_ORDERS",
                "TRADE_SCORE_THRESHOLD",
                "RESULT_SEQUENCE_GUARD",
                "STAKE_PROGRESSION",
                "STAKE_PROGRESSION_MAX_ORDERS",
                "STAKE_PROGRESSION_MAX_ACTIVE",
                "STAKE_PROGRESSION_BASE_ONLY_SEGMENTS",
            ):
                env.pop(name, None)
            env.update(
                {
                    "PYTHON_BIN": str(fake_python),
                    "FAKE_PYTHON_LOG": str(log_path),
                }
            )

            default_result = run(
                ["bash", str(ROOT / "scripts" / "run.sh")],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )
            default_args = log_path.read_text(encoding="utf-8").splitlines()

            custom_result = run(
                [
                    "bash",
                    str(ROOT / "scripts" / "run.sh"),
                    "--stake-progression-max-active",
                    "4",
                    "--trade-score-threshold",
                    "0",
                    "--stake-progression-base-only-segments",
                    "",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )
            custom_args = log_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(default_result.returncode, 0, default_result.stderr + default_result.stdout)
        self.assertNotIn("--no-stake-progression", default_args)
        self.assertNotIn("--no-result-sequence-guard", default_args)
        self.assertEqual(default_args[default_args.index("--max-open-orders") + 1], "2")
        self.assertEqual(default_args[default_args.index("--trade-score-threshold") + 1], "auto")
        self.assertEqual(default_args[default_args.index("--stake-progression-max-orders") + 1], "2")
        self.assertEqual(default_args[default_args.index("--stake-progression-max-active") + 1], "1")
        self.assertEqual(default_args[default_args.index("--stake-progression-base-only-segments") + 1], "")

        self.assertEqual(custom_result.returncode, 0, custom_result.stderr + custom_result.stdout)
        self.assertEqual(custom_args[custom_args.index("--stake-progression-max-active") + 1], "4")
        self.assertEqual(custom_args[custom_args.index("--trade-score-threshold") + 1], "0")
        self.assertEqual(custom_args[custom_args.index("--stake-progression-base-only-segments") + 1], "")

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
                self.assertTrue(any(name.endswith("/app/daily_profile_selector.py") for name in names))
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
                packaged_index = (package_root / "app" / "static" / "index.html").read_text(encoding="utf-8")
                help_result = run(
                    [sys.executable, "-m", "app.server", "--help"],
                    cwd=package_root,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(help_result.returncode, 0, help_result.stderr + help_result.stdout)
                self.assertIn('id="stake-progression-badge"', packaged_index)
                self.assertIn("--symbol", help_result.stdout)
                self.assertIn("--stake", help_result.stdout)
                self.assertIn("--stake-progression-max-orders", help_result.stdout)
                self.assertIn("--stake-progression-max-active", help_result.stdout)
                self.assertIn("--stake-progression-base-only-segments", help_result.stdout)
                self.assertIn("两阶段固定为 2 级", help_result.stdout)


if __name__ == "__main__":
    unittest.main()
