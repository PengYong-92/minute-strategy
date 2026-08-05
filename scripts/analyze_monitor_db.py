#!/usr/bin/env python3
import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_DB_PATH = ROOT / "data" / "monitor.sqlite3"
DEFAULT_REPORT_DIR = ROOT / "reports"

from app.order_profile import sample_from_entry_snapshot, summarize_order_samples


def load_order_samples(db_path: Path, symbol: str = "BTCUSDT") -> list[dict]:
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            select *
            from order_entry_snapshots
            where symbol = ?
            order by order_id
            """,
            (symbol.upper(),),
        ).fetchall()
    return [sample_from_entry_snapshot(row) for row in rows]


def analyze_samples(samples: Sequence[dict], *, min_group_size: int = 2) -> dict:
    return summarize_order_samples(samples, min_group_size=min_group_size)


def write_report(report: dict, report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"monitor_db_analysis_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def print_summary(report: dict) -> None:
    total = report["total"]
    print("=== 数据库订单样本画像 ===")
    print(
        f"样本: {report['sample_count']} "
        f"区间: {report['range']['from'] or '-'} -> {report['range']['to'] or '-'}"
    )
    print(
        f"总计: orders={total['orders']} win={total['win_rate']:.2%} "
        f"pnl={total['pnl']:.2f} ev={total['ev']:.2f}"
    )
    print("\n高风险提示:")
    for item in report["risk_hints"]:
        print(
            f"- {item['key']}: orders={item['orders']} win={item['win_rate']:.2%} "
            f"ev={item['ev']:.2f} pnl={item['pnl']:.2f}"
        )
    print("\n按时段:")
    for item in report["by_segment"]:
        print(
            f"- {item['key']}: orders={item['orders']} win={item['win_rate']:.2%} "
            f"ev={item['ev']:.2f} pnl={item['pnl']:.2f}"
        )
    guard = report.get("profile_guard") or {}
    if guard:
        baseline = guard["baseline"]
        print("\n画像守卫反事实回放:")
        print(
            f"- 基准滚单重算: orders={baseline['orders']} win={baseline['win_rate']:.2%} "
            f"pnl={baseline['pnl']:.2f} ev={baseline['ev']:.2f} roi={baseline['roi']:.2%}"
        )
        _print_guard_variant("- 静态组合拦截", guard.get("static_combined") or {})
        _print_guard_variant("- 前视滚动组合拦截", guard.get("walk_forward_combined") or {})
        _print_guard_variant("- 推荐前视滚动参数", guard.get("recommended_walk_forward") or {})
        recommended_subset = guard.get("recommended_key_subset") or {}
        _print_guard_variant("- 稳定候选key子集滚动", recommended_subset)
        policy = recommended_subset.get("selection_policy") or (guard.get("key_subset_sweep") or {}).get("selection_policy") or {}
        training = recommended_subset.get("training") or {}
        validation = recommended_subset.get("validation") or {}
        if validation:
            keys = ",".join(recommended_subset.get("candidate_risk_keys") or [])
            print(
                f"- 稳定候选验证: train_stable={training.get('stable')} tail_stable={validation.get('stable')} keys={keys} "
                f"train_orders={training.get('orders', 0)} train_delta={training.get('delta_pnl', 0.0):+.2f} "
                f"orders={validation.get('orders', 0)} delta={validation.get('delta_pnl', 0.0):+.2f} "
                f"traded_ev={validation.get('traded_ev', 0.0):.2f} "
                f"blocked_ev={validation.get('blocked_ev', 0.0):.2f} "
                f"{validation.get('reason', '')}"
            )
        if policy:
            print(
                f"- 稳定带选择: {policy.get('name', '-')} "
                f"eligible={policy.get('eligible', 0)}/{policy.get('stable_candidates', 0)} "
                f"pnl_band={policy.get('delta_pnl_band', 0.0):.2f} "
                f"validation_band={policy.get('validation_delta_pnl_band', 0.0):.2f} "
                f"{policy.get('reason', '')}"
            )
        replay_upgrade = guard.get("replay_upgrade") or {}
        print(
            f"- 回放估算升级建议: {replay_upgrade.get('action', '-')} "
            f"({replay_upgrade.get('confidence', '-')}) {replay_upgrade.get('reason', '')}"
        )
        contribution = (guard.get("walk_forward_combined") or {}).get("blocked_key_contribution") or []
        if contribution:
            print("\n前视滚动拦截贡献Top:")
            for item in contribution[:6]:
                _print_shadow_stats(f"- {item.get('key')}", item)
        print("\n单项弱点静态拦截Top:")
        for item in (guard.get("static_by_hint") or [])[:6]:
            _print_guard_variant(f"- {item['name'].replace('static_', '')}", item)
        sweep = guard.get("walk_forward_sweep") or {}
        if sweep:
            print("\n前视滚动参数扫描Top:")
            for item in (sweep.get("top") or [])[:8]:
                _print_guard_variant(
                    f"- history={item.get('min_history')} group={item.get('min_group_size')} score={item.get('score')}",
                    item,
                )
        subset_sweep = guard.get("key_subset_sweep") or {}
        if subset_sweep:
            summary = subset_sweep.get("validation") or {}
            print(
                f"\n滚动守卫key子集稳定扫描Top: "
                f"验证段orders={summary.get('orders', 0)} start={summary.get('start_index', 0)}"
            )
            for item in (subset_sweep.get("top") or [])[:8]:
                keys = ",".join(item.get("candidate_risk_keys") or item.get("allowed_risk_keys") or [])
                training = item.get("training") or {}
                validation = item.get("validation") or {}
                _print_guard_variant(
                    f"- stable={training.get('stable')}/{validation.get('stable')} keys={keys} score={item.get('score')} stability={item.get('stability_score')}",
                    item,
                )
    shadow = report.get("profile_guard_shadow") or {}
    if shadow and shadow.get("observed", {}).get("orders", 0):
        print("\n历史影子画像守卫表现:")
        _print_shadow_stats("- 已记录样本", shadow.get("observed") or {})
        _print_shadow_stats("- 影子会拦截", shadow.get("would_block") or {})
        _print_shadow_stats("- 影子放行", shadow.get("pass") or {})
        upgrade = shadow.get("upgrade") or {}
        print(
            f"- 升级建议: {upgrade.get('action', '-')} "
            f"({upgrade.get('confidence', '-')}) {upgrade.get('reason', '')}"
        )
        for item in (shadow.get("hit_keys") or [])[:6]:
            _print_shadow_stats(f"- 命中 {item.get('key')}", item)
    policy = report.get("profile_guard_policy") or {}
    if policy and (policy.get("by_policy") or policy.get("by_selected_key")):
        print("\n历史画像守卫策略版本表现:")
        for item in (policy.get("by_policy") or [])[:6]:
            _print_shadow_stats(f"- 策略 {item.get('key')}", item)
        for item in (policy.get("by_selected_key") or [])[:6]:
            _print_shadow_stats(f"- 选中key {item.get('key')}", item)
    compare = report.get("profile_guard_shadow_compare") or {}
    if compare and compare.get("observed", {}).get("orders", 0):
        print("\n推荐稳定候选 vs 默认滚动守卫:")
        _print_shadow_stats("- 推荐会拦", compare.get("recommended_block") or {})
        _print_shadow_stats("- 默认会拦", compare.get("default_block") or {})
        _print_shadow_stats("- 仅推荐会拦", compare.get("recommended_block_default_pass") or {})
        _print_shadow_stats("- 仅默认会拦", compare.get("recommended_pass_default_block") or {})
        upgrade = compare.get("upgrade") or {}
        print(
            f"- 对照升级建议: {upgrade.get('action', '-')} "
            f"({upgrade.get('confidence', '-')}) {upgrade.get('reason', '')}"
        )
        for item in compare.get("by_bucket") or []:
            _print_shadow_stats(f"- {item.get('key')}", item)


def _print_guard_variant(label: str, item: dict) -> None:
    traded = item.get("traded") or {}
    blocked = item.get("blocked") or {}
    print(
        f"{label}: traded={traded.get('orders', 0)} win={traded.get('win_rate', 0.0):.2%} "
        f"pnl={traded.get('pnl', 0.0):.2f} ev={traded.get('ev', 0.0):.2f} "
        f"blocked={blocked.get('orders', 0)} blocked_actual_pnl={item.get('blocked_actual_pnl', 0.0):.2f} "
        f"delta={item.get('delta_pnl', 0.0):+.2f}"
    )


def _print_shadow_stats(label: str, item: dict) -> None:
    print(
        f"{label}: orders={item.get('orders', 0)} win={item.get('win_rate', 0.0):.2%} "
        f"ev={item.get('ev', 0.0):.2f} pnl={item.get('pnl', 0.0):.2f}"
    )

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze live monitor SQLite order-entry samples.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--min-group-size", type=int, default=2)
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    samples = load_order_samples(args.db_path, args.symbol)
    report = analyze_samples(samples, min_group_size=max(1, args.min_group_size))
    if not args.no_write:
        report["report_path"] = str(write_report(report, args.report_dir))
    print_summary(report)
    if report.get("report_path"):
        print(f"\n报告: {report['report_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
