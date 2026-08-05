#!/usr/bin/env python3
import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.storage import summarize_observations


DEFAULT_DB_PATH = ROOT / "data" / "monitor.sqlite3"


def load_observations(db_path: Path, symbol: str = "BTCUSDT") -> list:
    from app.models import ObservationSignal

    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            select payload
            from observation_signals
            where symbol = ?
            order by opened_at
            """,
            (symbol.upper(),),
        ).fetchall()
    fields = set(ObservationSignal.__dataclass_fields__)
    observations = []
    for row in rows:
        payload = json.loads(row["payload"])
        clean = {key: payload.get(key) for key in fields if key in payload}
        clean.setdefault("level", "")
        clean.setdefault("reason", "")
        clean.setdefault("entry_price", 0.0)
        clean.setdefault("score", 0.0)
        clean.setdefault("threshold", 0.0)
        clean.setdefault("edge", 0.0)
        clean.setdefault("regime", "")
        clean.setdefault("source_decision", "")
        observations.append(ObservationSignal(**clean))
    return observations


def print_summary(summary: dict) -> None:
    total = summary.get("total") or {}
    print("=== 观察信号数据库画像 ===")
    print(
        f"总观察: signals={total.get('signals', 0)} settled={total.get('settled', 0)} "
        f"open={total.get('open', 0)} win={total.get('win_rate', 0.0):.2%} "
        f"ev={total.get('ev', 0.0):.2f} pnl={total.get('pnl', 0.0):.2f}"
    )
    print("\n策略/方向/时段:")
    for item in summary.get("groups") or []:
        print(
            f"- {item.get('strategy_tag')} {item.get('direction')} {item.get('threshold_segment')}: "
            f"signals={item.get('signals', 0)} settled={item.get('settled', 0)} "
            f"win={item.get('win_rate', 0.0):.2%} ev={item.get('ev', 0.0):.2f} "
            f"pnl={item.get('pnl', 0.0):.2f} action={item.get('action')} "
            f"confidence={item.get('confidence')}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze persisted research observation signals.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--symbol", default="BTCUSDT")
    args = parser.parse_args(argv)

    observations = load_observations(args.db_path, args.symbol)
    print_summary(summarize_observations(observations, group_limit=200))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
